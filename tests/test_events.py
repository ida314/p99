"""The event log is the source of truth; projections must be a pure function of it."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from core import engine as engine_module, events, methods, queues, srs, stats, strategies
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
            strategies=strategies.payload(["Two Pointers"]),
            methods=methods.payload(
                [
                    {"name": "two pointers over the sorted array", "used": True},
                    {"name": "prefix sums", "optimality": "optimal"},
                ]
            ),
        )
        eng.archive_code(
            f"/tmp/{slug}.py", "python", "two pointers over the sorted array"
        )
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
        rows("resolves"),
        keyed("strategies", "key"),
        keyed("attempt_strategies", "attempt_uuid, key"),
        keyed("attempt_methods", "attempt_uuid, key"),
        keyed("problem_methods", "slug, key"),
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


# --- crash recovery ----------------------------------------------------------
#
# A run put down by hand writes `session_suspended` on the way out. A run whose
# process is killed writes nothing, and every test below is about the gap that
# leaves: the session row is neither ended nor suspended, so `suspended_run`
# cannot see it and the whole evening reads as if it never happened.


def _crashed_mid_problem(conn, clock, slugs=("two-sum", "3sum")):
    """A run twelve minutes into its second problem when the process died.

    The kill is modelled by walking away from the engine rather than by killing
    anything: no `suspend_session`, no `end_session`, nothing on the way out but
    whatever the checkpoint had already written down.
    """
    eng = RunEngine(conn, clock=lambda: clock["t"])
    eng.start_session(list(slugs), speech_mode=True)
    eng.start_problem(slugs[0])
    eng.finish("accepted")
    eng.advance()

    eng.start_problem(slugs[1])
    eng.reveal_hint()
    eng.record_submission("wrong_answer")
    clock["t"] += 720
    eng.checkpoint()               # the ten-second timer, one last time
    return eng


def test_a_killed_run_is_not_visible_without_recovery(conn):
    """The bug, stated. Delete this and the rest of the section proves nothing."""
    _crashed_mid_problem(conn, {"t": 1000.0})

    session = conn.execute("SELECT * FROM sessions").fetchone()
    assert session["ended_at"] is None and session["suspended_at"] is None
    assert engine_module.suspended_run(conn) is None


def test_a_killed_run_is_offered_back(conn):
    clock = {"t": 1000.0}
    _crashed_mid_problem(conn, clock)

    assert engine_module.recover_crashed_runs(conn) is not None

    run = engine_module.suspended_run(conn)
    assert run.index == 1                  # still on the second problem
    assert run.title == "3Sum"
    assert run.active_seconds == 720       # the clock the checkpoint held
    assert run.speech_mode is True         # the run's, not today's setting


def test_recovery_does_not_grade_the_problem_it_hands_back(conn):
    """A crash is not a verdict, any more than a break is."""
    _crashed_mid_problem(conn, {"t": 1000.0})
    engine_module.recover_crashed_runs(conn)

    live = conn.execute("SELECT * FROM attempts WHERE slug = '3sum'").fetchone()
    assert live["verdict"] is None
    assert live["ended_at"] is None
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM fsrs_cards WHERE slug = '3sum'"
    ).fetchone()["n"] == 0


def test_resuming_a_recovered_run_restores_the_clock_and_the_hint(conn):
    clock = {"t": 1000.0}
    _crashed_mid_problem(conn, clock)
    engine_module.recover_crashed_runs(conn)

    clock["t"] += 5
    later = RunEngine(conn, clock=lambda: clock["t"])
    session = later.resume_session(engine_module.suspended_run(conn).session_uuid)

    assert session.remaining == ["3sum"]
    assert later.attempt.active_seconds == 720
    assert later.attempt.max_hint_tier == 1
    assert later.attempt.submissions == 1
    assert later.attempt.paused          # you were not solving it while it was dead


def test_recovery_clears_the_checkpoint(conn):
    _crashed_mid_problem(conn, {"t": 1000.0})
    engine_module.recover_crashed_runs(conn)

    assert conn.execute("SELECT COUNT(*) AS n FROM run_checkpoint").fetchone()["n"] == 0
    # And so a second launch finds nothing left to recover.
    assert engine_module.recover_crashed_runs(conn) is None


def test_recovery_is_idempotent(conn):
    clock = {"t": 1000.0}
    _crashed_mid_problem(conn, clock)
    engine_module.recover_crashed_runs(conn)
    before = _snapshot(conn)

    engine_module.recover_crashed_runs(conn)

    assert _snapshot(conn) == before


def test_a_crash_during_the_capture_flow_hands_back_the_next_problem(conn):
    """Killed in `$EDITOR`: the problem is graded, so the cursor moves past it."""
    eng = RunEngine(conn)
    eng.start_session(["two-sum", "3sum"])
    eng.start_problem("two-sum")
    eng.finish("accepted")         # checkpointed here, before the handoff
    del eng

    engine_module.recover_crashed_runs(conn)

    run = engine_module.suspended_run(conn)
    assert run.index == 1
    assert run.title is None
    resumed = RunEngine(conn)
    assert resumed.resume_session(run.session_uuid).remaining == ["3sum"]
    assert resumed.attempt is None


def test_a_crash_mid_re_solve_keeps_the_first_pass(conn):
    """The second pass is lost. The first one was sealed when it finished."""
    clock = {"t": 1000.0}
    eng = RunEngine(conn, clock=lambda: clock["t"])
    eng.start_session(["two-sum", "3sum"])
    eng.start_problem("two-sum")
    clock["t"] += 300
    eng.finish("solved_unaided")
    eng.solve_again()
    clock["t"] += 90
    eng.checkpoint()
    del eng

    engine_module.recover_crashed_runs(conn)

    first = conn.execute("SELECT * FROM attempts WHERE slug = 'two-sum'").fetchone()
    assert first["active_seconds"] == 300      # not 90, and not overwritten
    assert first["verdict"] == "solved_unaided"
    assert conn.execute("SELECT COUNT(*) AS n FROM resolves").fetchone()["n"] == 0
    # The run comes back on the problem after, not into a pass that recorded
    # nothing.
    run = engine_module.suspended_run(conn)
    assert run.index == 1
    assert run.title is None


def test_a_run_killed_before_its_first_problem_still_comes_back(conn):
    eng = RunEngine(conn)
    eng.start_session(["two-sum", "3sum"])
    del eng

    engine_module.recover_crashed_runs(conn)

    run = engine_module.suspended_run(conn)
    assert run.index == 0
    assert run.slugs == ["two-sum", "3sum"]
    assert run.title is None


def test_older_crashed_runs_are_closed_rather_than_queued_up(conn):
    """One resume offered, not nine. The rest are sealed where they stopped."""
    for slug in ("two-sum", "3sum", "merge-two-sorted-lists"):
        eng = RunEngine(conn)
        eng.start_session([slug])
        eng.start_problem(slug)
        eng.checkpoint()
        del eng

    engine_module.recover_crashed_runs(conn)

    rows = conn.execute("SELECT * FROM sessions ORDER BY id").fetchall()
    assert [r["ended_at"] is None for r in rows] == [False, False, True]
    assert [r["outcome"] for r in rows[:2]] == ["abandoned", "abandoned"]
    # Sealed, but not judged: nothing was scheduled off any of it.
    assert conn.execute("SELECT COUNT(*) AS n FROM fsrs_cards").fetchone()["n"] == 0
    assert engine_module.suspended_run(conn).slugs == ["merge-two-sorted-lists"]


def test_a_sealed_run_does_not_strand_the_problem_it_was_on(conn):
    """A NULL verdict is `_attempted_slugs`'s word for a problem you have seen.

    Left that way, a problem nobody ever graded is out of the unseen pool while
    having no card to come due either -- gone from the queue for good. `ungraded`
    is what says nothing judged it, and it is the one verdict that pool skips.
    """
    for slug in ("two-sum", "3sum"):
        eng = RunEngine(conn)
        eng.start_session([slug])
        eng.start_problem(slug)
        eng.checkpoint()
        del eng

    engine_module.recover_crashed_runs(conn)

    sealed = conn.execute("SELECT * FROM attempts WHERE slug = 'two-sum'").fetchone()
    assert sealed["verdict"] == "ungraded"
    assert sealed["ended_at"] is not None
    assert sealed["active_seconds"] == 0
    assert conn.execute("SELECT COUNT(*) AS n FROM fsrs_cards").fetchone()["n"] == 0
    assert "two-sum" not in queues._attempted_slugs(conn)
    # The run that was handed back is untouched: it is not over, so its problem
    # keeps the NULL verdict that says so.
    assert conn.execute(
        "SELECT verdict FROM attempts WHERE slug = '3sum'"
    ).fetchone()["verdict"] is None
    assert "3sum" in queues._attempted_slugs(conn)


def test_sealing_does_not_promote_a_run_that_finished_nothing(conn):
    """The outcome is read before the ungraded closes, not after."""
    for slug in ("two-sum", "3sum"):
        eng = RunEngine(conn)
        eng.start_session([slug])
        eng.start_problem(slug)
        eng.checkpoint()
        del eng

    engine_module.recover_crashed_runs(conn)

    assert conn.execute(
        "SELECT outcome FROM sessions ORDER BY id"
    ).fetchone()["outcome"] == "abandoned"


def test_an_older_crashed_run_is_not_dated_to_this_launch(conn):
    """A run from a fortnight ago did not end tonight."""
    for slug in ("two-sum", "3sum"):
        eng = RunEngine(conn)
        eng.start_session([slug])
        eng.start_problem(slug)
        eng.finish("solved_unaided")
        del eng
    old = (datetime.now(timezone.utc) - timedelta(days=14)).isoformat(timespec="seconds")
    conn.execute("UPDATE attempts SET ended_at = ? WHERE slug = 'two-sum'", (old,))

    engine_module.recover_crashed_runs(conn)

    sealed = conn.execute("SELECT * FROM sessions ORDER BY id").fetchone()
    assert sealed["ended_at"] == old
    assert sealed["outcome"] == "completed"


def test_a_legacy_crash_with_no_checkpoint_is_still_offered(conn):
    """Every run killed before this table existed. Recovered, just without a clock."""
    eng = RunEngine(conn)
    eng.start_session(["two-sum", "3sum"])
    eng.start_problem("two-sum")
    eng.finish("accepted")
    eng.advance()
    eng.start_problem("3sum")
    del eng
    conn.execute("DELETE FROM run_checkpoint")

    engine_module.recover_crashed_runs(conn)

    run = engine_module.suspended_run(conn)
    assert run.index == 1
    assert run.title == "3Sum"
    # Nothing was ever written down, so it comes back at zero rather than at a
    # number nobody measured.
    assert run.active_seconds == 0
    resumed = RunEngine(conn)
    assert resumed.resume_session(run.session_uuid).remaining == ["3sum"]
    assert resumed.attempt.problem.slug == "3sum"


def test_a_checkpoint_belonging_to_another_run_is_not_believed(conn):
    """Two crashes deep: the checkpoint describes the newest and only that one."""
    clock = {"t": 1000.0}
    first = RunEngine(conn, clock=lambda: clock["t"])
    first.start_session(["two-sum"])
    first.start_problem("two-sum")
    clock["t"] += 600
    first.checkpoint()
    del first
    second = RunEngine(conn, clock=lambda: clock["t"])
    second.start_session(["3sum"])
    second.start_problem("3sum")
    del second
    # The newest run's own checkpoint, replaced by hand with the older run's.
    conn.execute("UPDATE run_checkpoint SET session_uuid = 'not-this-run'")

    engine_module.recover_crashed_runs(conn)

    run = engine_module.suspended_run(conn)
    assert run.slugs == ["3sum"]
    assert run.active_seconds == 0      # rather than the other run's ten minutes


def test_recovery_survives_replay(conn):
    clock = {"t": 1000.0}
    _crashed_mid_problem(conn, clock)
    engine_module.recover_crashed_runs(conn)
    resumed = RunEngine(conn, clock=lambda: clock["t"])
    resumed.resume_session(engine_module.suspended_run(conn).session_uuid)
    resumed.finish("solved_with_hints")
    resumed.advance()
    resumed.end_session()
    before = _snapshot(conn)

    events.replay(conn)

    assert _snapshot(conn) == before


def test_a_replay_does_not_wipe_the_checkpoint(conn):
    """It is not a projection: nothing in the log could put it back."""
    _crashed_mid_problem(conn, {"t": 1000.0})
    before = dict(conn.execute("SELECT * FROM run_checkpoint").fetchone())

    events.replay(conn)

    assert dict(conn.execute("SELECT * FROM run_checkpoint").fetchone()) == before


def test_ending_a_run_normally_leaves_nothing_to_recover(conn):
    eng = RunEngine(conn)
    eng.start_session(["two-sum"])
    eng.start_problem("two-sum")
    eng.finish("solved_unaided")
    eng.advance()
    eng.end_session()

    assert conn.execute("SELECT COUNT(*) AS n FROM run_checkpoint").fetchone()["n"] == 0
    assert engine_module.recover_crashed_runs(conn) is None
    assert engine_module.suspended_run(conn) is None


def test_suspending_by_hand_leaves_nothing_to_recover(conn):
    """`z` and a kill must not both offer the same run back."""
    _suspended_mid_problem(conn, {"t": 1000.0})

    assert conn.execute("SELECT COUNT(*) AS n FROM run_checkpoint").fetchone()["n"] == 0
    assert engine_module.recover_crashed_runs(conn) is None
    assert engine_module.suspended_run(conn) is not None


# --- the approach you wrote --------------------------------------------------


def _solve(conn, slug="two-sum", verdict="solved_unaided", **finish):
    """One finished solve, with the engine left holding the attempt."""
    eng = RunEngine(conn)
    eng.start_session([slug])
    eng.start_problem(slug)
    eng.finish(verdict, **finish)
    return eng


def test_the_patterns_you_named_stay_out_of_the_problems_methods(conn):
    """Two places, and deliberately not a third: the vocabulary and the attempt.

    Naming a pattern is not recording a way to solve the problem. `two pointers`
    means the same thing on every problem; the route you took through this one is
    a sentence you write on the methods prompt, and nothing derives one from the
    other.
    """
    _solve(conn, strategies=strategies.payload(["Two Pointers"]))

    vocab = {r["key"]: r["name"] for r in conn.execute("SELECT key, name FROM strategies")}
    # The key is normalised; the name is the spelling that was typed.
    assert vocab == {"two-pointers": "Two Pointers"}
    answered = {
        r["key"]: r["role"] for r in conn.execute("SELECT key, role FROM attempt_strategies")
    }
    assert answered == {"two-pointers": "used"}
    assert conn.execute("SELECT COUNT(*) AS n FROM problem_methods").fetchone()["n"] == 0


def test_the_vocabulary_keeps_the_first_spelling_you_used(conn):
    """"Top-Down DP" and "top down dp" are one strategy, named once."""
    eng = RunEngine(conn)
    for spelling in ("Top-Down DP", "top down dp"):
        eng.start_session(["two-sum"])
        eng.start_problem("two-sum")
        eng.finish("accepted", strategies=strategies.payload([spelling]))
        eng.advance()
        eng.end_session()

    rows = conn.execute("SELECT key, name FROM strategies").fetchall()
    assert len(rows) == 1
    assert rows[0]["key"] == "top-down-dp"
    assert rows[0]["name"] == "Top-Down DP"


def test_a_legacy_worth_learning_answer_still_folds(conn):
    """The role is retired from the prompt, not from the log.

    An attempt recorded before methods existed keeps its answer, keeps rendering
    it, and keeps grading off it — `srs.grade_attempt` reads both doors.
    Nothing writes this role any more; everything still reads it.
    """
    eng = RunEngine(conn)
    eng.start_session(["two-sum"])
    eng.start_problem("two-sum")
    uuid = eng.attempt.uuid
    events.append(
        conn,
        events.PROBLEM_FINISHED,
        {
            "attempt_uuid": uuid,
            "slug": "two-sum",
            "verdict": "solved_unaided",
            "time_optimality": "suboptimal",
            "strategies": {"used": ["brute force"], "worth_learning": ["hash map"]},
        },
    )
    roles = {
        r["key"]: r["role"] for r in conn.execute("SELECT key, role FROM attempt_strategies")
    }
    assert roles == {"brute-force": "used", "hash-map": "worth_learning"}
    # The legacy door, and it is the only one open here: the answer never named a
    # method, so the problem's list of ways is still empty.
    assert conn.execute("SELECT COUNT(*) AS n FROM problem_methods").fetchone()["n"] == 0
    assert stats.load_attempts(conn)[0]["saw_better"] is True


def test_discarding_an_attempt_forgets_its_strategy_answer(conn):
    """The answer goes with the attempt; the vocabulary is not a casualty."""
    eng = RunEngine(conn)
    eng.start_session(["two-sum"])
    eng.start_problem("two-sum")
    uuid = eng.attempt.uuid
    eng.discard()

    events.append(conn, events.ATTEMPT_DISCARDED, {"attempt_uuid": uuid, "slug": "two-sum"})
    assert (
        conn.execute("SELECT COUNT(*) AS n FROM attempt_strategies").fetchone()["n"] == 0
    )


def test_a_discarded_run_takes_its_strategy_answers_down_with_it(conn):
    """A tombstoned session leaves no answer behind, before or after a replay."""
    _run_a_session(conn, slugs=("two-sum",))
    assert conn.execute("SELECT COUNT(*) AS n FROM attempt_strategies").fetchone()["n"] == 1

    session_uuid = conn.execute("SELECT uuid FROM sessions").fetchone()["uuid"]
    events.append(conn, events.RUN_DELETED, {"session_uuid": session_uuid})
    assert conn.execute("SELECT COUNT(*) AS n FROM attempt_strategies").fetchone()["n"] == 0

    events.replay(conn)
    assert conn.execute("SELECT COUNT(*) AS n FROM attempt_strategies").fetchone()["n"] == 0
    # The replay skips the whole event, so a strategy only that attempt ever
    # named is never created rather than created and then deleted.
    assert conn.execute("SELECT COUNT(*) AS n FROM strategies").fetchone()["n"] == 0


# --- the ways this problem can be solved -------------------------------------


def test_the_methods_block_records_optimal_and_not(conn):
    """The whole point of the page: both kinds are kept, and both are the record."""
    _solve(
        conn,
        time_optimality="suboptimal",
        strategies=strategies.payload(["bottom-up tabulation"]),
        methods=methods.payload(
            [
                {
                    "name": "tabulate over the coin axis",
                    "optimality": "suboptimal",
                    "used": True,
                },
                {"name": "patience sorting on the tails", "optimality": "optimal"},
                {"name": "try every pair", "optimality": None},
            ]
        ),
    )
    ways = {
        r["key"]: r["optimality"]
        for r in conn.execute("SELECT key, optimality FROM problem_methods")
    }
    assert ways == {
        "tabulate-over-the-coin-axis": "suboptimal",
        "patience-sorting-on-the-tails": "optimal",
        # Recorded with no claim at all, which is not the same as `unsure`:
        # one is a question nobody answered, the other is an answer.
        "try-every-pair": None,
    }
    # Only the one you wrote is this attempt's answer. The other two are facts
    # about the problem and belong to no solve.
    assert {r["key"] for r in conn.execute("SELECT key FROM attempt_methods")} == {
        "tabulate-over-the-coin-axis"
    }
    # And the name is the problem's own sentence, kept as typed.
    assert conn.execute(
        "SELECT name FROM problem_methods WHERE key = 'try-every-pair'"
    ).fetchone()[0] == "try every pair"


def test_an_optimal_method_you_did_not_write_is_the_new_saw_better(conn):
    """The replacement for the retired `worth_learning` role, on the schedule.

    Two identical beaten solves. One records that an optimal route exists which
    is not the one it wrote; the other records nothing. The first found the
    pattern late, the second missed it — so only the second comes back soon.
    """
    beaten = dict(
        time_optimality="suboptimal",
        claimed_complexity="O(n^2)",
        claimed_space_complexity="O(1)",
    )
    _solve(
        conn,
        slug="two-sum",
        **beaten,
        strategies=strategies.payload(["brute force"]),
        methods=methods.payload(
            [
                {"name": "try every pair", "used": True},
                {"name": "one pass with a complement map", "optimality": "optimal"},
            ]
        ),
    )
    _solve(conn, slug="3sum", **beaten, strategies=strategies.payload(["brute force"]))

    cards = {r["slug"]: r for r in conn.execute("SELECT * FROM fsrs_cards")}
    assert srs.parse_ts(cards["two-sum"]["due"]) > srs.parse_ts(cards["3sum"]["due"])
    rows = {r["slug"]: r for r in stats.load_attempts(conn)}
    assert rows["two-sum"]["saw_better"] is True
    assert rows["3sum"]["saw_better"] is False


def test_marking_the_method_you_wrote_optimal_is_not_seeing_better(conn):
    """`saw_better` asks about a route that is *not* yours.

    Without the exclusion, recording your own optimal solve would read as having
    spotted something you missed, and the demote it cancels would never fire.
    """
    _solve(
        conn,
        time_optimality="suboptimal",
        strategies=strategies.payload(["brute force"]),
        methods=methods.payload(
            [{"name": "try every pair", "optimality": "optimal", "used": True}]
        ),
    )
    assert stats.load_attempts(conn)[0]["saw_better"] is False


def test_the_methods_page_can_be_edited_long_after_the_solve(conn):
    """`method_updated` is why the page beats a role on the finish prompt.

    A route you notice two months later gets recorded when you notice it. The
    event folds through exactly the same code the prompt does.
    """
    _solve(
        conn,
        strategies=strategies.payload(["brute force"]),
        methods=methods.payload([{"name": "try every pair", "used": True}]),
    )
    events.append(
        conn,
        events.METHOD_UPDATED,
        {
            "slug": "two-sum",
            "methods": [
                {"name": "one pass with a complement map", "optimality": "optimal"}
            ],
        },
    )
    ways = {
        r["key"]: r["optimality"]
        for r in conn.execute("SELECT key, optimality FROM problem_methods")
    }
    assert ways == {"try-every-pair": None, "one-pass-with-a-complement-map": "optimal"}
    # Read months later and never a solve's answer: nothing links it to an attempt.
    assert conn.execute("SELECT COUNT(*) AS n FROM attempt_methods").fetchone()["n"] == 1
    # Not retroactive on the card: that attempt was graded when it happened, and
    # a replay grades it in the same order and reaches the same place.
    before = [dict(r) for r in conn.execute("SELECT * FROM fsrs_cards")]
    events.replay(conn)
    assert [dict(r) for r in conn.execute("SELECT * FROM fsrs_cards")] == before


def test_the_archived_file_is_tagged_with_the_method(conn):
    """One solve, one file, and the method it took as a tag on it."""
    eng = _solve(
        conn,
        strategies=strategies.payload(["hash map"]),
        methods=methods.payload(
            [{"name": "one pass with a complement map", "used": True}]
        ),
    )
    eng.archive_code("/tmp/two-sum.py", "python", "one pass with a complement map")

    row = conn.execute("SELECT * FROM problem_methods").fetchone()
    assert (row["slug"], row["key"]) == ("two-sum", "one-pass-with-a-complement-map")
    assert row["code_path"] == "/tmp/two-sum.py"


def test_an_untagged_file_is_still_archived_against_the_attempt(conn):
    """No method named is a normal night, not an error.

    The file is the solve's either way. What an untagged solve does not do is
    invent a row on the problem's list to hang it from.
    """
    eng = _solve(conn, strategies=strategies.payload(["hash map"]))
    eng.archive_code("/tmp/two-sum.py", "python")

    assert conn.execute("SELECT code_path FROM attempts").fetchone()[0] == "/tmp/two-sum.py"
    assert conn.execute("SELECT COUNT(*) AS n FROM problem_methods").fetchone()["n"] == 0


def test_the_attempts_headline_file_is_the_first_one_archived(conn):
    """One `attempts.code_path` — and a replay picks the same one.

    A solve archives one file now; the log still holds nights that archived
    several, one per approach named, and the guard is what keeps those reading
    the way they were written.
    """
    eng = _solve(conn, strategies=strategies.payload(["hash map", "sorting"]))
    eng.archive_code("/tmp/1-hash-map.py", "python", "one pass with a map")
    eng.archive_code("/tmp/1-sorting.py", "python", "sort then scan")

    assert conn.execute("SELECT code_path FROM attempts").fetchone()[0] == "/tmp/1-hash-map.py"
    events.replay(conn)
    assert conn.execute("SELECT code_path FROM attempts").fetchone()[0] == "/tmp/1-hash-map.py"


def test_archiving_code_claims_nothing_about_the_cost(conn):
    """Writing a route up says nothing about whether it is the best one."""
    eng = _solve(
        conn,
        time_optimality="optimal",
        strategies=strategies.payload(["hash map"]),
    )
    eng.archive_code("/tmp/two-sum.py", "python", "one pass with a complement map")
    row = conn.execute("SELECT * FROM problem_methods").fetchone()
    assert row["code_path"] and row["optimality"] is None


def test_code_written_from_the_methods_screen_belongs_to_no_attempt(conn):
    """`method_archived` fills a gap without inventing a solve that closed it."""
    events.append(
        conn,
        events.METHOD_ARCHIVED,
        {
            "slug": "two-sum",
            "method": "Monotonic Stack Over The Heights",
            "code_path": "/tmp/method-monotonic-stack-over-the-heights.py",
            "language": "python",
        },
    )
    row = conn.execute("SELECT * FROM problem_methods").fetchone()
    assert row["key"] == "monotonic-stack-over-the-heights"
    assert row["name"] == "Monotonic Stack Over The Heights"
    assert row["attempt_uuid"] is None and row["attempt_id"] is None
    # It joins the problem's list and nothing else. Not the strategy vocabulary:
    # a method is not a pattern, and writing one down does not name one.
    assert conn.execute("SELECT COUNT(*) AS n FROM strategies").fetchone()["n"] == 0
    assert conn.execute("SELECT COUNT(*) AS n FROM attempt_methods").fetchone()["n"] == 0
    assert conn.execute("SELECT COUNT(*) AS n FROM attempts").fetchone()["n"] == 0


def test_the_retired_solution_events_fold_nowhere(conn):
    """They named a row in the strategy vocabulary, which no method can be.

    They stay in the log and stay valid; a replay simply no longer projects them,
    exactly as `worth_learning` is no longer prompted for.
    """
    events.append(
        conn,
        events.SOLUTION_UPDATED,
        {"slug": "two-sum", "solutions": [{"name": "hash map", "optimality": "optimal"}]},
    )
    events.append(
        conn,
        events.SOLUTION_ARCHIVED,
        {"slug": "two-sum", "approach": "hash map", "code_path": "/tmp/x.py"},
    )
    events.replay(conn)
    assert conn.execute("SELECT COUNT(*) AS n FROM problem_methods").fetchone()["n"] == 0


def test_a_discarded_attempt_loses_its_file_but_not_the_method(conn):
    """The method survives the discard; the pointer at its file does not.

    The row stays because a route that worked once did not stop existing. The
    pointer goes because `code_archived` carries a top-level `attempt_uuid`, so a
    replay skips it and never attaches the file at all.
    """
    eng = _solve(
        conn,
        strategies=strategies.payload(["hash map"]),
        methods=methods.payload(
            [{"name": "one pass with a complement map", "used": True}]
        ),
    )
    uuid = eng.attempt.uuid
    eng.archive_code("/tmp/two-sum.py", "python", "one pass with a complement map")
    assert conn.execute("SELECT code_path FROM problem_methods").fetchone()[0]

    events.append(conn, events.ATTEMPT_DISCARDED, {"attempt_uuid": uuid, "slug": "two-sum"})
    row = conn.execute("SELECT * FROM problem_methods").fetchone()
    assert row["key"] == "one-pass-with-a-complement-map"
    assert row["code_path"] is None and row["attempt_id"] is None
    # And the attempt's own answer goes with the attempt.
    assert conn.execute("SELECT COUNT(*) AS n FROM attempt_methods").fetchone()["n"] == 0


def test_the_page_lists_what_you_recorded_as_well_as_what_you_wrote(conn):
    """The empty row is the point: it is the gap you would open the page to close."""
    eng = _solve(
        conn,
        time_optimality="suboptimal",
        claimed_complexity="O(n^2)",
        strategies=strategies.payload(["brute force"]),
        methods=methods.payload(
            [
                {"name": "try every pair", "optimality": "suboptimal", "used": True},
                {
                    "name": "one pass with a complement map",
                    "optimality": "optimal",
                },
            ]
        ),
    )
    eng.archive_code("/tmp/two-sum.py", "python", "try every pair")

    rows = {m.key: m for m in methods.for_problem(conn, "two-sum")}
    assert set(rows) == {"try-every-pair", "one-pass-with-a-complement-map"}
    assert rows["try-every-pair"].written
    assert rows["try-every-pair"].optimality == "suboptimal"
    # The complexity is read off the attempt that wrote it, never asked twice.
    assert rows["try-every-pair"].complexity == "O(n^2)"
    assert not rows["one-pass-with-a-complement-map"].written
    assert rows["one-pass-with-a-complement-map"].optimality == "optimal"


def test_a_method_and_a_strategy_may_share_a_name_without_touching(conn):
    """Two lists, two namespaces. The whole point of the split, in one solve."""
    _solve(
        conn,
        strategies=strategies.payload(["two pointers"]),
        methods=methods.payload([{"name": "two pointers", "used": True}]),
    )
    assert [r["key"] for r in conn.execute("SELECT key FROM strategies")] == [
        "two-pointers"
    ]
    assert [r["key"] for r in conn.execute("SELECT key FROM problem_methods")] == [
        "two-pointers"
    ]
    # Same word, two rows in two tables, and no join anywhere between them: the
    # method is this problem's, the strategy is every problem's.
    assert [s.key for s in strategies.for_problem(conn, "two-sum")] == ["two-pointers"]
    assert [m.key for m in methods.for_problem(conn, "3sum")] == []


# --- solving it again --------------------------------------------------------


def _solve_it_twice(conn, slug="two-sum"):
    """One attempt, two passes: the second by a different route."""
    eng = RunEngine(conn)
    eng.start_session([slug])
    eng.start_problem(slug)
    eng.finish(
        "solved_unaided",
        self_confidence=3,
        language="python",
        claimed_complexity="O(n log n)",
        time_optimality="suboptimal",
        strategies=strategies.payload(["sorting"]),
        methods=methods.payload([{"name": "sort then scan", "used": True}]),
    )
    eng.archive_code(f"/tmp/{slug}.py", "python", "sort then scan")
    eng.record_note(f"/tmp/{slug}.md")

    eng.solve_again()
    eng.finish(
        "solved_unaided",
        self_confidence=4,
        language="python",
        claimed_complexity="O(n)",
        time_optimality="optimal",
        strategies=strategies.payload(["hash map"]),
        methods=methods.payload(
            [{"name": "one pass with a complement map", "used": True}]
        ),
    )
    eng.archive_code(
        f"/tmp/{slug}-again2.py", "python", "one pass with a complement map"
    )
    eng.record_note(f"/tmp/{slug}-again2.md")
    return eng


def test_a_second_pass_is_recorded_beside_the_attempt_not_over_it(conn):
    _solve_it_twice(conn)

    attempt = conn.execute("SELECT * FROM attempts").fetchone()
    assert attempt["verdict"] == "solved_unaided"
    # The attempt keeps the first pass's answers, files and all.
    assert attempt["claimed_complexity"] == "O(n log n)"
    assert attempt["time_optimality"] == "suboptimal"
    assert attempt["code_path"] == "/tmp/two-sum.py"
    assert attempt["note_path"] == "/tmp/two-sum.md"

    resolve = conn.execute("SELECT * FROM resolves").fetchone()
    assert resolve["n"] == 2
    assert resolve["attempt_uuid"] == attempt["uuid"]
    assert resolve["attempt_id"] == attempt["id"]
    assert resolve["claimed_complexity"] == "O(n)"
    assert resolve["time_optimality"] == "optimal"
    assert resolve["code_path"] == "/tmp/two-sum-again2.py"
    assert resolve["note_path"] == "/tmp/two-sum-again2.md"


def test_a_second_pass_does_not_grade_a_second_time(conn):
    """One attempt is one review, however many times you sat it."""
    _solve_it_twice(conn)
    card = conn.execute("SELECT * FROM fsrs_cards WHERE slug = 'two-sum'").fetchone()
    assert card["reps"] == 1


def test_both_routes_reach_the_problems_list(conn):
    """Solving it a second way in one sitting is what the methods list wants to hear."""
    _solve_it_twice(conn)
    assert {r["key"] for r in conn.execute("SELECT key FROM problem_methods")} == {
        "sort-then-scan",
        "one-pass-with-a-complement-map",
    }
    # Both passes were this attempt's, so both methods are its answer.
    assert {r["key"] for r in conn.execute("SELECT key FROM attempt_methods")} == {
        "sort-then-scan",
        "one-pass-with-a-complement-map",
    }
    assert {r["key"] for r in conn.execute("SELECT key FROM attempt_strategies")} == {
        "sorting",
        "hash-map",
    }


def test_a_second_pass_survives_replay(conn):
    _solve_it_twice(conn)
    before = _snapshot(conn)
    events.replay(conn)
    assert _snapshot(conn) == before


def test_discarding_the_attempt_takes_its_later_passes_with_it(conn):
    """Otherwise the live projection and the replayed one disagree."""
    eng = _solve_it_twice(conn)
    attempt_uuid = eng.attempt.uuid
    events.append(
        conn, events.ATTEMPT_DISCARDED, {"attempt_uuid": attempt_uuid, "slug": "two-sum"}
    )
    events.replay(conn)

    assert conn.execute("SELECT COUNT(*) AS n FROM attempts").fetchone()["n"] == 0
    assert conn.execute("SELECT COUNT(*) AS n FROM resolves").fetchone()["n"] == 0


def test_giving_up_on_a_later_pass_leaves_the_verdict_alone(conn):
    eng = RunEngine(conn)
    eng.start_session(["two-sum"])
    eng.start_problem("two-sum")
    eng.finish("solved_unaided")
    eng.solve_again()
    eng.abandon()

    attempt = conn.execute("SELECT * FROM attempts").fetchone()
    assert attempt["verdict"] == "solved_unaided"
    assert conn.execute("SELECT verdict FROM resolves").fetchone()["verdict"] == "gave_up"
    # No `problem_abandoned`, so nothing regraded.
    types = [e.type for e in events.read_all(conn)]
    assert events.PROBLEM_ABANDONED not in types
    assert conn.execute("SELECT reps FROM fsrs_cards").fetchone()["reps"] == 1


def test_throwing_a_later_pass_away_writes_nothing(conn):
    eng = RunEngine(conn)
    eng.start_session(["two-sum"])
    eng.start_problem("two-sum")
    eng.finish("solved_unaided")
    eng.solve_again()
    eng.discard()

    assert conn.execute("SELECT COUNT(*) AS n FROM attempts").fetchone()["n"] == 1
    assert conn.execute("SELECT COUNT(*) AS n FROM resolves").fetchone()["n"] == 0
    types = [e.type for e in events.read_all(conn)]
    assert events.ATTEMPT_DISCARDED not in types
    # And the attempt is sealed again, so the run can move on.
    assert eng.attempt.finished
    eng.advance()


def test_a_run_cannot_be_suspended_mid_re_solve(conn):
    """The clock a suspend records would land on the first pass's row."""
    eng = RunEngine(conn)
    eng.start_session(["two-sum"])
    eng.start_problem("two-sum")
    eng.finish("solved_unaided")
    eng.solve_again()
    with pytest.raises(engine_module.SessionError):
        eng.suspend_session()
