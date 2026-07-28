"""The event log is the source of truth; projections must be a pure function of it."""

from __future__ import annotations

import pytest

from core import engine as engine_module, events
from core.engine import RunEngine


def _run_a_session(conn, slugs=("two-sum", "3sum")):
    eng = RunEngine(conn)
    eng.start_session(list(slugs))
    for slug in slugs:
        eng.start_problem(slug)
        eng.reveal_hint()
        n = eng.record_submission("wrong_answer")
        eng.archive_submission(f"/tmp/{slug}-wrong{n}.py", n, "python")
        eng.finish("accepted", self_confidence=3, lc_runtime_pct=91.0, language="python")
        eng.archive_code(f"/tmp/{slug}.py", "python")
        eng.record_note(f"/tmp/{slug}.md")
        eng.advance()
    eng.end_session(session_note="tired but fine")
    return eng


def _snapshot(conn):
    def rows(table, skip=()):
        out = []
        for row in conn.execute(f"SELECT * FROM {table} ORDER BY id"):
            out.append({k: row[k] for k in row.keys() if k not in skip})
        return out

    return rows("sessions"), rows("attempts"), rows("submissions")


def test_projections_are_rebuilt_identically_by_replay(conn):
    _run_a_session(conn)
    before = _snapshot(conn)

    replayed = events.replay(conn)

    assert replayed > 0
    assert _snapshot(conn) == before


def test_replay_is_idempotent(conn):
    _run_a_session(conn)
    events.replay(conn)
    once = _snapshot(conn)
    events.replay(conn)
    assert _snapshot(conn) == once


def test_attempt_ids_are_stable_across_replay(conn):
    """Code and note paths embed the attempt id — a replay must not renumber."""
    _run_a_session(conn)
    ids = [r["id"] for r in conn.execute("SELECT id FROM attempts ORDER BY id")]
    uuids = [r["uuid"] for r in conn.execute("SELECT uuid FROM attempts ORDER BY id")]
    events.replay(conn)
    assert [r["id"] for r in conn.execute("SELECT id FROM attempts ORDER BY id")] == ids
    assert [r["uuid"] for r in conn.execute("SELECT uuid FROM attempts ORDER BY id")] == uuids


def test_hint_tier_is_monotonic(conn):
    eng = RunEngine(conn)
    eng.start_session(["two-sum"])
    eng.start_problem("two-sum")
    assert eng.reveal_hint()[0] == 1
    assert eng.reveal_hint()[0] == 2
    eng.finish("accepted")

    row = conn.execute("SELECT max_hint_tier FROM attempts").fetchone()
    assert row["max_hint_tier"] == 2

    # Out-of-order replay of a lower tier must not lower the recorded tier.
    events.append(
        conn,
        events.HINT_REVEALED,
        {"attempt_uuid": eng.attempt.uuid, "slug": "two-sum", "tier": 1},
    )
    assert conn.execute("SELECT max_hint_tier FROM attempts").fetchone()["max_hint_tier"] == 2


def test_hint_event_is_written_before_the_text_is_returned(conn):
    """A SIGKILL between reveal and render must not erase the hint."""
    eng = RunEngine(conn)
    eng.start_session(["two-sum"])
    eng.start_problem("two-sum")
    eng.reveal_hint()
    types = [r["type"] for r in conn.execute("SELECT type FROM events ORDER BY id")]
    assert types[-1] == events.HINT_REVEALED


def test_accepted_submission_does_not_count_against_you(conn):
    eng = RunEngine(conn)
    eng.start_session(["two-sum"])
    eng.start_problem("two-sum")
    eng.record_submission("wrong_answer")
    eng.record_submission("accepted")
    eng.finish("accepted")
    assert conn.execute("SELECT submissions FROM attempts").fetchone()["submissions"] == 1


def test_every_submission_is_logged_in_order_with_its_code(conn):
    eng = RunEngine(conn)
    eng.start_session(["two-sum"])
    eng.start_problem("two-sum")
    first = eng.record_submission("wrong_answer")
    eng.archive_submission("/tmp/1-wrong1.py", first, "python")
    second = eng.record_submission("wrong_answer")  # skipped the editor
    eng.finish("accepted")

    assert (first, second) == (1, 2)
    rows = conn.execute("SELECT * FROM submissions ORDER BY n").fetchall()
    assert [r["n"] for r in rows] == [1, 2]
    assert [r["code_path"] for r in rows] == ["/tmp/1-wrong1.py", None]
    assert all(r["verdict"] == "wrong_answer" for r in rows)
    assert all(r["attempt_id"] == eng.attempt.id for r in rows)


def test_archived_wrong_answers_never_touch_the_solution_path(conn):
    """`attempts.code_path` is the solution you settled on, not a wrong answer."""
    eng = RunEngine(conn)
    eng.start_session(["two-sum"])
    eng.start_problem("two-sum")
    n = eng.record_submission("wrong_answer")
    eng.archive_submission("/tmp/wrong.py", n, "python")
    eng.finish("accepted")
    eng.archive_code("/tmp/right.py", "python")

    assert conn.execute("SELECT code_path FROM attempts").fetchone()["code_path"] == "/tmp/right.py"
    assert conn.execute("SELECT code_path FROM submissions").fetchone()["code_path"] == "/tmp/wrong.py"


def test_submission_numbers_survive_an_accepted_submit_in_the_middle(conn):
    """`n` numbers submits; `submissions` counts failures. They are not the same."""
    eng = RunEngine(conn)
    eng.start_session(["two-sum"])
    eng.start_problem("two-sum")
    eng.record_submission("wrong_answer")
    eng.record_submission("accepted")
    third = eng.record_submission("wrong_answer")
    eng.finish("accepted")

    assert third == 3  # would collide with the first wrong answer's file at 2
    assert conn.execute("SELECT submissions FROM attempts").fetchone()["submissions"] == 2
    assert [r["n"] for r in conn.execute("SELECT n FROM submissions ORDER BY n")] == [1, 2, 3]


def test_a_submission_logged_before_this_feature_still_projects(conn):
    """Old events carry no `n`; replay has to number them anyway."""
    eng = RunEngine(conn)
    eng.start_session(["two-sum"])
    attempt = eng.start_problem("two-sum")
    for _ in range(2):
        events.append(
            conn,
            events.PROBLEM_SUBMITTED,
            {"attempt_uuid": attempt.uuid, "slug": "two-sum", "verdict": "wrong_answer"},
        )
    assert [r["n"] for r in conn.execute("SELECT n FROM submissions ORDER BY n")] == [1, 2]
    events.replay(conn)
    assert [r["n"] for r in conn.execute("SELECT n FROM submissions ORDER BY n")] == [1, 2]


def test_gave_up_is_still_recorded(conn):
    eng = RunEngine(conn)
    eng.start_session(["two-sum"])
    eng.start_problem("two-sum")
    eng.abandon()
    eng.end_session()
    row = conn.execute("SELECT verdict, ended_at FROM attempts").fetchone()
    assert row["verdict"] == "gave_up"
    assert row["ended_at"] is not None


def test_tier_four_hint_text_says_it_ends_the_attempt(conn):
    assert "gave_up" in engine_module.HINT_STUBS[4]


def test_session_outcome_is_inferred(conn):
    eng = RunEngine(conn)
    eng.start_session(["two-sum", "3sum"], planned_n=2)
    eng.start_problem("two-sum")
    eng.finish("accepted")
    eng.advance()
    eng.end_session()
    assert conn.execute("SELECT outcome FROM sessions").fetchone()["outcome"] == "partial"


def test_unknown_event_type_is_rejected(conn):
    with pytest.raises(ValueError):
        events.append(conn, "not_a_real_event", {})


def test_paused_time_is_excluded_from_active_seconds(conn):
    clock = {"t": 1000.0}

    eng = RunEngine(conn, clock=lambda: clock["t"])
    eng.start_session(["two-sum"])
    eng.start_problem("two-sum")
    clock["t"] += 60
    eng.pause()
    clock["t"] += 300  # five minutes away from the desk
    eng.resume()
    clock["t"] += 30
    eng.finish("accepted")

    row = conn.execute("SELECT active_seconds, wall_seconds, paused_seconds FROM attempts").fetchone()
    assert row["active_seconds"] == 90
    assert row["wall_seconds"] == 390
    assert row["paused_seconds"] == 300


def test_tier_four_hint_ends_the_attempt_as_gave_up(conn):
    """Spec §13: tier 4 ends the attempt regardless of what happens next."""
    eng = RunEngine(conn)
    eng.start_session(["two-sum"])
    eng.start_problem("two-sum")
    for _ in range(4):
        eng.reveal_hint()

    assert eng.attempt.finished
    row = conn.execute("SELECT verdict, max_hint_tier, ended_at FROM attempts").fetchone()
    assert row["verdict"] == "gave_up"
    assert row["max_hint_tier"] == 4
    assert row["ended_at"] is not None


def test_the_clock_stops_when_an_attempt_finishes(conn):
    """Time spent in the verdict prompt is neither solve time nor a pause."""
    clock = {"t": 0.0}
    eng = RunEngine(conn, clock=lambda: clock["t"])
    eng.start_session(["two-sum"])
    attempt = eng.start_problem("two-sum")

    clock["t"] += 600
    timing = attempt.timing()          # the instant `f` is pressed
    clock["t"] += 40                   # filling in the verdict modal
    eng.finish("accepted", timing=timing)
    clock["t"] += 5000                 # still on screen afterwards

    row = conn.execute(
        "SELECT active_seconds, wall_seconds, paused_seconds FROM attempts"
    ).fetchone()
    assert row["active_seconds"] == 600
    assert row["wall_seconds"] == 600
    assert row["paused_seconds"] == 0
    assert attempt.active_seconds == 600  # the displayed timer is frozen too


def test_append_is_atomic_with_its_projection(conn, monkeypatch):
    """A failed projection must not leave a half-applied event in the log."""
    eng = RunEngine(conn)
    eng.start_session(["two-sum"])
    before = conn.execute("SELECT COUNT(*) AS n FROM events").fetchone()["n"]

    boom = RuntimeError("projection exploded")

    def explode(_conn, _event):
        raise boom

    monkeypatch.setattr(events, "apply", explode)
    with pytest.raises(RuntimeError):
        events.append(conn, events.PROBLEM_STARTED, {"session_uuid": "x", "attempt_uuid": "y", "slug": "two-sum"})

    assert conn.execute("SELECT COUNT(*) AS n FROM events").fetchone()["n"] == before
