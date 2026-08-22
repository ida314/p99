"""The event log is the source of truth; projections must be a pure function of it."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from core import engine as engine_module, events, strategies
from core.engine import RunEngine


def _run_a_session(conn, slugs=("two-sum", "3sum")):
    eng = RunEngine(conn)
    eng.start_session(list(slugs))
    for slug in slugs:
        eng.start_problem(slug)
        eng.reveal_hint()
        n = eng.record_submission("wrong_answer")
        eng.archive_submission(f"/tmp/{slug}-wrong{n}.py", n, "python")
        eng.finish(
            "accepted",
            self_confidence=3,
            lc_runtime_pct=91.0,
            language="python",
            claimed_complexity="O(n log n)",
            claimed_space_complexity="O(n)",
            time_optimality="optimal",
            space_optimality="suboptimal",
            strategies=strategies.payload(["Two Pointers"], ["prefix sums"]),
        )
        eng.archive_code(f"/tmp/{slug}.py", "python")
        eng.record_note(f"/tmp/{slug}.md")
        eng.record_audio(f"/tmp/{slug}.opus")
        eng.advance()
    eng.end_session(session_note="tired but fine")
    return eng


def _snapshot(conn):
    def rows(table, skip=()):
        out = []
        for row in conn.execute(f"SELECT * FROM {table} ORDER BY id"):
            out.append({k: row[k] for k in row.keys() if k not in skip})
        return out

    def keyed(table, order):
        return [
            {k: row[k] for k in row.keys()}
            for row in conn.execute(f"SELECT * FROM {table} ORDER BY {order}")
        ]

    return (
        rows("sessions"),
        rows("attempts"),
        rows("submissions"),
        keyed("strategies", "key"),
        keyed("problem_strategies", "slug, key"),
        keyed("attempt_strategies", "attempt_uuid, key"),
    )


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


def test_what_you_claimed_about_the_solution_reaches_the_row(conn):
    eng = RunEngine(conn)
    eng.start_session(["two-sum"])
    eng.start_problem("two-sum")
    eng.finish(
        "solved_unaided",
        claimed_complexity="O(n)",
        claimed_space_complexity="O(1)",
        time_optimality="suboptimal",
        space_optimality="optimal",
    )
    eng.record_audio("/tmp/two-sum.opus")

    row = conn.execute("SELECT * FROM attempts").fetchone()
    assert row["claimed_complexity"] == "O(n)"
    assert row["claimed_space_complexity"] == "O(1)"
    assert row["time_optimality"] == "suboptimal"
    assert row["space_optimality"] == "optimal"
    assert row["audio_path"] == "/tmp/two-sum.opus"


def test_the_recording_and_the_claim_survive_a_replay(conn):
    _run_a_session(conn, slugs=("two-sum",))
    events.replay(conn)

    row = conn.execute("SELECT * FROM attempts").fetchone()
    assert row["claimed_complexity"] == "O(n log n)"
    assert row["claimed_space_complexity"] == "O(n)"
    assert row["time_optimality"] == "optimal"
    assert row["space_optimality"] == "suboptimal"
    assert row["audio_path"] == "/tmp/two-sum.opus"


def test_an_old_optimality_answer_stays_the_answer_it_was(conn):
    """A claim made before the question had axes is not read as either axis.

    "Was it the optimal algorithm?" did not ask about time, so replaying an
    event that answers it must not fill `time_optimality` — that would put an
    axis on an answer nobody gave. It lands in the column of its own name and
    renders there, unqualified, forever.
    """
    eng = RunEngine(conn)
    eng.start_session(["two-sum"])
    attempt = eng.start_problem("two-sum")
    events.append(
        conn,
        events.PROBLEM_FINISHED,
        {
            "attempt_uuid": attempt.uuid,
            "slug": "two-sum",
            "verdict": "solved_unaided",
            "claimed_complexity": "O(n log n)",
            "optimality": "optimal",
        },
    )
    events.replay(conn)

    row = conn.execute("SELECT * FROM attempts").fetchone()
    assert row["optimality"] == "optimal"
    assert row["time_optimality"] is None
    assert row["space_optimality"] is None
    assert row["claimed_space_complexity"] is None


def test_giving_up_carries_no_claim_about_a_solution(conn):
    """`finish` routes `gave_up` to `abandon`, which has nothing to claim."""
    eng = RunEngine(conn)
    eng.start_session(["two-sum"])
    eng.start_problem("two-sum")
    eng.finish(
        "gave_up",
        claimed_complexity="O(n)",
        claimed_space_complexity="O(1)",
        time_optimality="optimal",
        space_optimality="optimal",
    )

    row = conn.execute("SELECT * FROM attempts").fetchone()
    assert row["verdict"] == "gave_up"
    assert row["claimed_complexity"] is None
    assert row["claimed_space_complexity"] is None
    assert row["time_optimality"] is None
    assert row["space_optimality"] is None


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


# --- tombstones ------------------------------------------------------------


def test_a_discarded_attempt_leaves_no_trace_in_the_projections(conn):
    """Throwing an attempt away must remove it, not merely mark it."""
    eng = RunEngine(conn)
    eng.start_session(["two-sum", "3sum"])
    eng.start_problem("two-sum")
    eng.reveal_hint()
    eng.record_submission("wrong_answer")
    attempt_uuid = eng.attempt.uuid

    eng.discard()

    assert eng.attempt is None
    assert conn.execute("SELECT COUNT(*) AS n FROM attempts").fetchone()["n"] == 0
    assert conn.execute("SELECT COUNT(*) AS n FROM submissions").fetchone()["n"] == 0
    assert conn.execute("SELECT COUNT(*) AS n FROM fsrs_cards").fetchone()["n"] == 0
    # The log still remembers everything that happened. Only the projection forgets.
    types = [e.type for e in events.read_all(conn) if e.payload.get("attempt_uuid") == attempt_uuid]
    assert events.HINT_REVEALED in types
    assert events.ATTEMPT_DISCARDED in types


def test_a_discard_survives_replay(conn):
    _run_a_session(conn, slugs=("two-sum",))
    eng = RunEngine(conn)
    eng.start_session(["3sum"])
    eng.start_problem("3sum")
    eng.discard()

    before = _snapshot(conn)
    events.replay(conn)

    assert _snapshot(conn) == before
    assert [r["slug"] for r in conn.execute("SELECT slug FROM attempts")] == ["two-sum"]


def test_a_finished_attempt_cannot_be_discarded(conn):
    """It has already been graded, and `discard` cannot unwind a card."""
    eng = RunEngine(conn)
    eng.start_session(["two-sum"])
    eng.start_problem("two-sum")
    eng.finish("solved_unaided")
    with pytest.raises(RuntimeError):
        eng.discard()
    assert conn.execute("SELECT COUNT(*) AS n FROM attempts").fetchone()["n"] == 1


def test_deleting_a_run_removes_it_and_unwinds_its_reviews(conn):
    """The card must not survive the run that created it.

    This is the whole reason `replay` skips tombstoned events instead of
    applying and then deleting them: `run_deleted` sits at the end of the log,
    so an apply-then-delete would grade the card on the way past.
    """
    _run_a_session(conn, slugs=("two-sum",))
    doomed = RunEngine(conn)
    doomed.start_session(["3sum"])
    doomed.start_problem("3sum")
    doomed.finish("solved_unaided", self_confidence=4)
    doomed.advance()
    session_uuid = doomed.session.uuid
    doomed.end_session()

    assert {r["slug"] for r in conn.execute("SELECT slug FROM fsrs_cards")} == {"two-sum", "3sum"}

    events.append(conn, events.RUN_DELETED, {"session_uuid": session_uuid})
    events.replay(conn)

    assert conn.execute("SELECT COUNT(*) AS n FROM sessions").fetchone()["n"] == 1
    assert [r["slug"] for r in conn.execute("SELECT slug FROM attempts")] == ["two-sum"]
    assert conn.execute("SELECT COUNT(*) AS n FROM submissions").fetchone()["n"] == 1
    assert {r["slug"] for r in conn.execute("SELECT slug FROM fsrs_cards")} == {"two-sum"}


def test_deleting_a_run_leaves_the_other_runs_byte_identical(conn):
    """A delete is not an excuse to renumber or rescore what is left."""
    _run_a_session(conn, slugs=("two-sum",))
    keep = _snapshot(conn)

    doomed = RunEngine(conn)
    doomed.start_session(["3sum"])
    doomed.start_problem("3sum")
    doomed.finish("solved_with_hints")
    doomed.advance()
    session_uuid = doomed.session.uuid
    doomed.end_session()

    events.append(conn, events.RUN_DELETED, {"session_uuid": session_uuid})
    events.replay(conn)

    assert _snapshot(conn) == keep


def test_tombstoned_collects_attempts_of_a_deleted_session(conn):
    _run_a_session(conn, slugs=("two-sum",))
    uuid = conn.execute("SELECT uuid FROM sessions").fetchone()["uuid"]
    attempt_uuid = conn.execute("SELECT uuid FROM attempts").fetchone()["uuid"]
    events.append(conn, events.RUN_DELETED, {"session_uuid": uuid})

    sessions, attempts = events.tombstoned(events.read_all(conn))

    assert sessions == {uuid}
    assert attempt_uuid in attempts


# --- suspend and resume ----------------------------------------------------


def _suspended_mid_problem(conn, clock):
    """A run put down on the second problem, twelve minutes in, after a hint."""
    eng = RunEngine(conn, clock=lambda: clock["t"])
    eng.start_session(["two-sum", "3sum"], speech_mode=True)
    eng.start_problem("two-sum")
    eng.finish("accepted")
    eng.advance()

    eng.start_problem("3sum")
    eng.reveal_hint()
    eng.record_submission("wrong_answer")
    clock["t"] += 720           # twelve minutes on the problem
    eng.pause()
    clock["t"] += 60            # one minute at the kettle, logged as a pause
    eng.resume()
    eng.suspend_session()
    return eng


def _walk_away(conn, hours):
    """Backdate the suspend, so the away time is a real span to measure.

    The fake clock in these tests is monotonic-only; how long you were gone is
    wall-clock, and the log is where that is written down.
    """
    when = datetime.now(timezone.utc) - timedelta(hours=hours)
    conn.execute(
        "UPDATE sessions SET suspended_at = ? WHERE suspended_at IS NOT NULL",
        (when.isoformat(timespec="seconds"),),
    )


def test_suspending_leaves_the_attempt_ungraded(conn):
    clock = {"t": 1000.0}
    eng = _suspended_mid_problem(conn, clock)

    assert eng.session is None and eng.attempt is None
    live = conn.execute(
        "SELECT * FROM attempts WHERE slug = '3sum'"
    ).fetchone()
    # The whole point: a break is not a verdict.
    assert live["verdict"] is None
    assert live["ended_at"] is None
    assert live["active_seconds"] == 720
    assert live["paused_seconds"] == 60
    # Nothing was graded, so nothing was scheduled off it.
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM fsrs_cards WHERE slug = '3sum'"
    ).fetchone()["n"] == 0

    session = conn.execute("SELECT * FROM sessions").fetchone()
    assert session["ended_at"] is None
    assert session["suspended_at"] is not None
    assert session["resume_index"] == 1
    assert session["speech_mode"] == 1


def test_a_suspended_run_is_found_and_described(conn):
    clock = {"t": 1000.0}
    _suspended_mid_problem(conn, clock)

    run = engine_module.suspended_run(conn)

    assert run is not None
    assert run.index == 1
    assert run.slugs == ["two-sum", "3sum"]
    assert run.speech_mode is True
    assert run.title == "3Sum"
    assert "problem 2 of 2" in run.summary
    assert "12:00 in" in run.summary


def test_resuming_restores_the_clock_the_hint_and_the_submits(conn):
    clock = {"t": 1000.0}
    _suspended_mid_problem(conn, clock)
    _walk_away(conn, hours=6)
    clock["t"] += 30_000  # a different process, hours later

    eng = RunEngine(conn, clock=lambda: clock["t"])
    run = engine_module.suspended_run(conn)
    session = eng.resume_session(run.session_uuid)

    assert session.index == 1
    assert session.remaining == ["3sum"]
    a = eng.attempt
    assert a is not None and a.problem.slug == "3sum"
    assert a.max_hint_tier == 1
    assert a.submissions == 1
    assert a.submits_logged == 1
    # Comes back paused, holding exactly the readings it was put down with.
    assert a.paused
    assert a.active_seconds == 720
    assert a.total_paused_seconds == 60
    assert a.wall_seconds == 780

    # The away time is its own fact, not folded into the pause.
    row = conn.execute("SELECT * FROM attempts WHERE slug = '3sum'").fetchone()
    assert row["suspends"] == 1
    assert row["suspended_seconds"] == pytest.approx(6 * 3600, abs=5)
    assert row["paused_seconds"] == 60


def test_the_clock_runs_again_once_the_resumed_attempt_is_unpaused(conn):
    clock = {"t": 1000.0}
    _suspended_mid_problem(conn, clock)
    _walk_away(conn, hours=6)

    eng = RunEngine(conn, clock=lambda: clock["t"])
    eng.resume_session(engine_module.suspended_run(conn).session_uuid)
    clock["t"] += 90              # ninety seconds reading yourself back in
    assert eng.attempt.active_seconds == 720   # free, because it is a pause
    eng.resume()
    clock["t"] += 60
    assert eng.attempt.active_seconds == 780

    eng.finish("accepted")
    row = conn.execute("SELECT * FROM attempts WHERE slug = '3sum'").fetchone()
    assert row["verdict"] == "accepted"
    assert row["active_seconds"] == 780
    assert row["paused_seconds"] == 150        # 60 at the kettle + 90 on return
    # Six hours away, and not one second of it in `paused_seconds`.
    assert row["suspended_seconds"] == pytest.approx(6 * 3600, abs=5)


def test_a_resumed_submit_does_not_reuse_an_archive_number(conn):
    """`submits_logged` is what names the file — restarting it would overwrite."""
    clock = {"t": 1000.0}
    _suspended_mid_problem(conn, clock)

    eng = RunEngine(conn, clock=lambda: clock["t"])
    eng.resume_session(engine_module.suspended_run(conn).session_uuid)

    assert eng.record_submission("wrong_answer") == 2


def test_suspend_and_resume_survive_replay(conn):
    clock = {"t": 1000.0}
    _suspended_mid_problem(conn, clock)
    eng = RunEngine(conn, clock=lambda: clock["t"])
    eng.resume_session(engine_module.suspended_run(conn).session_uuid)
    eng.resume()
    eng.finish("accepted")
    eng.advance()
    eng.end_session()
    before = _snapshot(conn)

    events.replay(conn)

    assert _snapshot(conn) == before


def test_resuming_refuses_while_a_run_is_live(conn):
    clock = {"t": 1000.0}
    _suspended_mid_problem(conn, clock)
    eng = RunEngine(conn)
    eng.start_session(["merge-two-sorted-lists"])

    with pytest.raises(engine_module.SessionError):
        eng.resume_session(engine_module.suspended_run(conn).session_uuid)


def test_a_suspend_after_finishing_does_not_hand_back_the_solved_problem(conn):
    """Quit during the capture step: the cursor moves, so the resume goes on."""
    eng = RunEngine(conn)
    eng.start_session(["two-sum", "3sum"])
    eng.start_problem("two-sum")
    eng.finish("accepted")
    eng.suspend_session()          # before `advance` — the editor handoff died

    run = engine_module.suspended_run(conn)
    assert run.index == 1
    assert run.title is None       # nothing left in progress

    resumed = RunEngine(conn)
    session = resumed.resume_session(run.session_uuid)
    assert resumed.attempt is None
    assert session.remaining == ["3sum"]


def test_throwing_away_a_resumed_attempt_still_replays_identically(conn):
    """The tombstone skips events naming the attempt — including its suspend."""
    clock = {"t": 1000.0}
    _suspended_mid_problem(conn, clock)
    _walk_away(conn, hours=2)

    eng = RunEngine(conn, clock=lambda: clock["t"])
    eng.resume_session(engine_module.suspended_run(conn).session_uuid)
    eng.discard()                  # the wrong problem, or a timer left running
    eng.advance()
    eng.end_session()
    before = _snapshot(conn)

    events.replay(conn)

    assert _snapshot(conn) == before
    # The run is over and says so, rather than being stuck offering a resume.
    session = conn.execute("SELECT * FROM sessions").fetchone()
    assert session["ended_at"] is not None
    assert session["suspended_at"] is None
    assert engine_module.suspended_run(conn) is None


# --- solving strategies ------------------------------------------------------


def test_a_named_strategy_lands_in_all_three_tables(conn):
    """One payload block, three projections: vocabulary, problem link, answer."""
    _run_a_session(conn, slugs=("two-sum",))

    vocab = {r["key"]: r["name"] for r in conn.execute("SELECT key, name FROM strategies")}
    # The key is normalised; the name is the spelling that was typed.
    assert vocab == {"two-pointers": "Two Pointers", "prefix-sums": "prefix sums"}
    assert {r["key"] for r in conn.execute("SELECT key FROM problem_strategies")} == {
        "two-pointers",
        "prefix-sums",
    }
    answered = {
        r["key"]: r["role"] for r in conn.execute("SELECT key, role FROM attempt_strategies")
    }
    assert answered == {"two-pointers": "used", "prefix-sums": "worth_learning"}


def test_the_vocabulary_keeps_the_first_spelling_you_used(conn):
    """"Top-Down DP" and "top down dp" are one strategy, named once."""
    eng = RunEngine(conn)
    for spelling in ("Top-Down DP", "top down dp"):
        eng.start_session(["two-sum"])
        eng.start_problem("two-sum")
        eng.finish("accepted", strategies=strategies.payload([spelling], []))
        eng.advance()
        eng.end_session()

    rows = conn.execute("SELECT key, name FROM strategies").fetchall()
    assert len(rows) == 1
    assert rows[0]["key"] == "top-down-dp"
    assert rows[0]["name"] == "Top-Down DP"


def test_a_strategy_cannot_be_both_written_and_wished_for(conn):
    """A name in both roles is `used`. You cannot have written it and not have."""
    eng = RunEngine(conn)
    eng.start_session(["two-sum"])
    eng.start_problem("two-sum")
    eng.finish("accepted", strategies=strategies.payload(["heap"], ["heap", "quickselect"]))
    eng.advance()
    eng.end_session()

    roles = {
        r["key"]: r["role"] for r in conn.execute("SELECT key, role FROM attempt_strategies")
    }
    assert roles == {"heap": "used", "quickselect": "worth_learning"}


def test_discarding_an_attempt_forgets_its_strategy_answer(conn):
    """The answer goes with the attempt; the vocabulary is not a casualty."""
    eng = RunEngine(conn)
    eng.start_session(["two-sum"])
    eng.start_problem("two-sum")
    uuid = eng.attempt.uuid
    eng.discard()

    # Nothing was finished, so there is nothing to forget yet -- but a discard
    # after a finish is the real case, and it is what `history` does on `d`.
    events.append(conn, events.ATTEMPT_DISCARDED, {"attempt_uuid": uuid, "slug": "two-sum"})
    assert (
        conn.execute("SELECT COUNT(*) AS n FROM attempt_strategies").fetchone()["n"] == 0
    )


def test_a_discarded_run_takes_its_strategy_answers_down_with_it(conn):
    """A tombstoned session leaves no answer behind, before or after a replay."""
    _run_a_session(conn, slugs=("two-sum",))
    assert conn.execute("SELECT COUNT(*) AS n FROM attempt_strategies").fetchone()["n"] == 2

    session_uuid = conn.execute("SELECT uuid FROM sessions").fetchone()["uuid"]
    events.append(conn, events.RUN_DELETED, {"session_uuid": session_uuid})
    assert conn.execute("SELECT COUNT(*) AS n FROM attempt_strategies").fetchone()["n"] == 0

    events.replay(conn)
    assert conn.execute("SELECT COUNT(*) AS n FROM attempt_strategies").fetchone()["n"] == 0
    # The replay skips the whole event, so a strategy only that attempt ever
    # named is never created rather than created and then deleted.
    assert conn.execute("SELECT COUNT(*) AS n FROM strategies").fetchone()["n"] == 0
