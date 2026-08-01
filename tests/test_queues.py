"""Queue generation: the constraints live in code so they can be tested."""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

from core import catalog, events, queues, scoring, srs
from core.engine import RunEngine

WEIGHTS = scoring.load_weights()
NOW = datetime(2026, 7, 31, 9, 0, tzinfo=timezone.utc)


def _build(conn, n=6, now=NOW, regenerate=False):
    return queues.ensure(
        conn,
        n=n,
        active_list="neetcode150",
        weights=WEIGHTS,
        now=now,
        regenerate=regenerate,
    )


def _solve(conn, slug, verdict="accepted", **kw):
    eng = RunEngine(conn)
    eng.start_session([slug])
    eng.start_problem(slug)
    if verdict == "gave_up":
        eng.abandon()
    else:
        eng.finish(verdict, **kw)
    eng.advance()
    eng.end_session()


# --- shape -----------------------------------------------------------------


def test_a_fresh_catalog_still_fills_a_queue(conn):
    """The morning queue is never empty (spec §10)."""
    queue = _build(conn, n=5)
    assert len(queue.items) == 5
    assert queue.rationale
    assert queue.due_count == 0  # nothing has a card yet


def test_the_queue_is_recorded_as_an_event(conn):
    queue = _build(conn)
    row = conn.execute("SELECT * FROM events WHERE type = 'queue_generated'").fetchone()
    assert row is not None
    stored = conn.execute("SELECT * FROM queues WHERE date = ?", (queue.date,)).fetchone()
    assert stored["generated_by"] == queues.GENERATED_BY


def test_the_queue_survives_replay(conn):
    """`queues` is a projection: the payload carries the finished list."""
    queue = _build(conn)
    events.replay(conn)
    assert queues.load(conn, queue.date, NOW).slugs == queue.slugs


def test_ensure_does_not_regenerate(conn):
    first = _build(conn)
    second = _build(conn)
    assert first.slugs == second.slugs
    n = conn.execute("SELECT COUNT(*) AS n FROM events WHERE type='queue_generated'").fetchone()
    assert n["n"] == 1


def test_regenerate_replaces_the_same_day(conn):
    first = _build(conn)
    _build(conn, regenerate=True)
    rows = conn.execute("SELECT COUNT(*) AS n FROM queues").fetchone()
    assert rows["n"] == 1  # one row per date, not one per generation
    assert queues.load(conn, first.date, NOW) is not None


def test_generation_is_deterministic(conn):
    """Same database, same `now`, same queue — there is no rng left in here."""
    first = _build(conn, regenerate=True)
    second = _build(conn, regenerate=True)
    assert first.slugs == second.slugs


# --- the constraints -------------------------------------------------------


def test_no_pattern_runs_three_deep(conn):
    """Interleaving beats blocking for transfer (spec §10 stage 1)."""
    for n in range(2, 11):
        queue = _build(conn, n=n, regenerate=True)
        patterns = [i.pattern for i in queue.items]
        runs = [
            patterns[k]
            for k in range(len(patterns) - 2)
            if patterns[k] == patterns[k + 1] == patterns[k + 2]
        ]
        assert not runs, f"n={n} blocked on {runs}"


def test_reviews_never_take_over_the_queue(conn):
    """Spec §8: due reviews lead, but never consume more than ~40% of it."""
    slugs = [p.slug for p in catalog.all_problems(conn, "neetcode150")[:20]]
    for slug in slugs:
        _solve(conn, slug, self_confidence=3)

    later = NOW + timedelta(days=400)  # everything is long overdue
    for n in (3, 5, 6, 10):
        queue = _build(conn, n=n, now=later, regenerate=True)
        assert queue.due_count <= math.ceil(queues.DUE_SHARE * n), (
            f"n={n}: {queue.due_count} reviews"
        )
        assert queue.new_count > 0


def test_the_difficulty_mix_is_targeted(conn):
    queue = _build(conn, n=10)
    counts = {}
    for item in queue.items:
        counts[item.difficulty.lower()] = counts.get(item.difficulty.lower(), 0) + 1
    # 20/60/20 of 10. Exact rather than approximate: a fresh catalog has plenty
    # of every difficulty, so nothing forces the targets to be relaxed.
    assert counts == {"easy": 2, "medium": 6, "hard": 2}


def test_mix_targets_always_sum_to_n():
    for n in range(1, 31):
        assert sum(queues._mix_targets(n).values()) == n


def test_a_recent_attempt_is_off_the_table(conn):
    """Nothing attempted in the last 3 days, unless FSRS says it is due."""
    _solve(conn, "two-sum", self_confidence=3)
    # `two-sum` now has a card due ~2 days out, so it is neither recent-and-free
    # nor due: it should not appear.
    queue = _build(conn, n=6, now=NOW)
    assert "two-sum" not in queue.slugs


def test_a_due_review_beats_the_cooldown(conn):
    """The three-day rule yields to the model, not the other way round."""
    _solve(conn, "two-sum", self_confidence=3)
    card = srs.card_row(conn, "two-sum")
    assert card is not None
    # Ask for a queue long after the card came due; the cooldown is irrelevant.
    later = srs.parse_ts(card["due"]) + timedelta(days=30)
    queue = _build(conn, n=6, now=later, regenerate=True)
    assert "two-sum" in queue.slugs
    assert queue.due_count >= 1


def test_reviews_are_ordered_most_overdue_first(conn):
    for slug in ("two-sum", "3sum", "valid-anagram"):
        _solve(conn, slug, self_confidence=3)
    later = NOW + timedelta(days=365)
    queue = _build(conn, n=10, now=later, regenerate=True)
    overdue = [i.overdue_days for i in queue.items if i.is_review]
    assert overdue == sorted(overdue, reverse=True)


# --- the rationale ---------------------------------------------------------


def test_the_rationale_says_what_it_did(conn):
    queue = _build(conn, n=6)
    assert "new" in queue.rationale
    assert "pattern" in queue.rationale


def test_a_relaxed_constraint_is_reported_not_swallowed(conn):
    """A rule that silently stops applying is worse than one that never was."""
    pool = [
        queues.Item(slug=f"p{i}", title=f"P{i}", difficulty="medium", pattern="same", source="unseen")
        for i in range(5)
    ]
    items, relaxed = queues._select(pool, 5)
    assert len(items) == 5
    assert relaxed
    assert "interleave" in queues._rationale(items, relaxed, [])


def test_hydrate_marks_reviews_from_live_cards(conn):
    """A queued problem you have since solved reads as a review, not as new."""
    queue = _build(conn, n=5)
    target = queue.slugs[0]
    assert not queue.items[0].is_review

    _solve(conn, target, self_confidence=3)
    reloaded = queues.load(conn, queue.date, NOW)
    assert reloaded.slugs == queue.slugs  # the list itself is fixed by the event
    assert next(i for i in reloaded.items if i.slug == target).is_review
