"""Percentiles, tail drivers, and run ranking (spec §6)."""

from __future__ import annotations

from dataclasses import fields
from datetime import datetime, timedelta, timezone

from core import events, stats
from core.engine import RunEngine


def _log_attempt(conn, session_uuid, slug, active_seconds, *, tier=0, verdict="accepted", days_ago=0):
    ts = (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat(timespec="seconds")
    attempt_uuid = events.new_uuid()
    events.append(
        conn,
        events.PROBLEM_STARTED,
        {"session_uuid": session_uuid, "attempt_uuid": attempt_uuid, "slug": slug},
        ts=ts,
    )
    if tier:
        events.append(conn, events.HINT_REVEALED, {"attempt_uuid": attempt_uuid, "tier": tier}, ts=ts)
    events.append(
        conn,
        events.PROBLEM_FINISHED,
        {
            "attempt_uuid": attempt_uuid,
            "verdict": verdict,
            "active_seconds": active_seconds,
            "wall_seconds": active_seconds,
        },
        ts=ts,
    )
    return attempt_uuid


def _session(conn, days_ago=0, planned_n=3):
    ts = (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat(timespec="seconds")
    session_uuid = events.new_uuid()
    events.append(
        conn, events.SESSION_STARTED, {"session_uuid": session_uuid, "planned_n": planned_n}, ts=ts
    )
    return session_uuid


def test_percentile_is_nearest_rank():
    values = list(range(1, 101))
    assert stats.percentile(values, 50) == 50
    assert stats.percentile(values, 90) == 90
    assert stats.percentile(values, 99) == 99
    assert stats.percentile([], 50) is None
    assert stats.percentile([7], 99) == 7


def test_percentile_never_invents_a_value():
    """Every reported percentile must be a time some attempt actually took."""
    values = [100, 200, 900]
    for p in (50, 90, 99):
        assert stats.percentile(values, p) in values


def test_distribution_names_its_tail_drivers(conn):
    s = _session(conn)
    # sliding-window problems, one of them a disaster
    _log_attempt(conn, s, "longest-substring-without-repeating-characters", 600)
    _log_attempt(conn, s, "permutation-in-string", 700)
    _log_attempt(conn, s, "minimum-window-substring", 2470, tier=2)

    dist = stats.distribution(conn, pattern="sliding-window", days=60)

    assert dist.n == 3
    assert dist.p50 == 700
    assert dist.tail_drivers
    worst = dist.tail_drivers[0]
    assert worst.slug == "minimum-window-substring"
    assert "tier-2" in worst.reason


def test_thin_slices_are_marked_unreliable(conn):
    s = _session(conn)
    _log_attempt(conn, s, "two-sum", 300)
    dist = stats.distribution(conn, days=60, min_samples=20)
    assert dist.n == 1
    assert not dist.reliable


def test_window_excludes_older_attempts(conn):
    s = _session(conn)
    _log_attempt(conn, s, "two-sum", 300, days_ago=1)
    _log_attempt(conn, s, "3sum", 400, days_ago=200)
    assert stats.distribution(conn, days=60).n == 1
    assert stats.distribution(conn, days=None).n == 2


def test_clean_rate_trend_compares_to_the_prior_window(conn):
    s = _session(conn)
    # prior 60d window: one hinted solve. current window: one clean solve.
    _log_attempt(conn, s, "two-sum", 300, days_ago=90, tier=2)
    _log_attempt(conn, s, "3sum", 300, days_ago=5)
    dist = stats.distribution(conn, days=60)
    assert dist.clean_rate == 1.0
    assert dist.prior_clean_rate == 0.0
    assert dist.clean_rate_delta == 1.0


def test_run_ranking_and_standing(conn):
    eng = RunEngine(conn)
    for i, seconds in enumerate((300, 1500)):
        eng.start_session(["two-sum"])
        eng.start_problem("two-sum")
        events.append(
            conn,
            events.PROBLEM_FINISHED,
            {
                "attempt_uuid": eng.attempt.uuid,
                "verdict": "accepted",
                "active_seconds": seconds,
                "wall_seconds": seconds,
            },
        )
        eng.attempt.finished = True
        eng.advance()
        eng.end_session()

    runs = stats.load_runs(conn)
    assert len(runs) == 2
    assert runs[0].score > runs[1].score  # the faster run scores higher

    standing = stats.standing(runs, runs[0].session_id)
    assert standing.rank == 1
    assert standing.run_number == 1
    assert standing.best_score == runs[0].score
    assert standing.percentile == 100.0


def test_overview_counts_only_finished_attempts(conn):
    s = _session(conn)
    _log_attempt(conn, s, "two-sum", 300)
    events.append(
        conn,
        events.PROBLEM_STARTED,
        {"session_uuid": s, "attempt_uuid": events.new_uuid(), "slug": "3sum"},
    )
    ov = stats.overview(conn)
    assert ov.total_attempts == 1
    assert ov.distinct_slugs == 1
    assert ov.catalog_size == 150


def test_distributions_by_pattern(conn):
    s = _session(conn)
    _log_attempt(conn, s, "two-sum", 300)
    _log_attempt(conn, s, "minimum-window-substring", 2000)
    labels = {d.label for d in stats.distributions_by(conn, "pattern", days=60)}
    assert "ARRAYS HASHING" in labels
    assert "SLIDING WINDOW" in labels


def test_fmt_ago_uses_the_coarsest_unit_that_still_says_something():
    now = datetime(2026, 8, 3, 12, 0, tzinfo=timezone.utc)

    def ago(**kwargs):
        return stats.fmt_ago((now - timedelta(**kwargs)).isoformat(), now=now)

    assert ago(seconds=20) == "just now"
    assert ago(minutes=40) == "40m ago"
    assert ago(hours=5) == "5h ago"
    assert ago(days=12) == "12d ago"
    assert ago(days=59) == "59d ago"
    assert ago(days=90) == "2mo ago"
    assert ago(days=800) == "2y ago"


def test_problem_history_is_newest_first_and_only_this_problem(conn):
    s = _session(conn)
    _log_attempt(conn, s, "two-sum", 1800, verdict="gave_up", days_ago=90)
    _log_attempt(conn, s, "two-sum", 552, verdict="solved_with_hints", tier=1, days_ago=12)
    _log_attempt(conn, s, "3sum", 900, days_ago=1)
    # Still on screen: started, never finished. It is not part of your record.
    events.append(
        conn,
        events.PROBLEM_STARTED,
        {"session_uuid": s, "attempt_uuid": events.new_uuid(), "slug": "two-sum"},
    )

    past = stats.problem_history(conn, "two-sum")
    assert len(past) == 2
    assert past[0].ago == "12d ago"
    assert past[0].result == "SOLVED WITH HINTS"
    assert past[0].active_seconds == 552
    assert past[0].score > 0
    assert past[1].result == "GAVE UP"
    assert past[1].score == 0

    # The one thing this read model must never carry: the answer.
    assert not any(f.name.endswith("_path") for f in fields(stats.PastAttempt))


def test_a_past_attempt_reports_the_help_it_took_not_the_help_it_claimed(conn):
    """Hints revealed outrank the verdict, and the row fades to match."""
    s = _session(conn)
    _log_attempt(conn, s, "two-sum", 400, verdict="solved_unaided", tier=2)

    past = stats.problem_history(conn, "two-sum")[0]
    assert past.result == "SOLVED, NO HELP (tier 2)"
    assert past.style_label == "SOLVED AFTER DESCRIPTION"


def test_problem_history_limit_keeps_the_most_recent(conn):
    s = _session(conn)
    for days in (60, 30, 2):
        _log_attempt(conn, s, "two-sum", 600, days_ago=days)
    past = stats.problem_history(conn, "two-sum", limit=2)
    assert [p.ago for p in past] == ["2d ago", "30d ago"]
