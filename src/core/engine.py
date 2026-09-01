"""The run engine: timer, hint tiers, attempt state machine (spec §15.3).

All state changes go through here, and every one of them is an event append —
the engine never writes a projection row directly. The TUI is a view over this.

The one table written by hand is `run_checkpoint`, and it is not a projection:
it is where the run's clock lives *between* events, so a process that dies
without unwinding can still be picked back up. Nothing there is history — a run
in progress has not happened yet — and `recover_crashed_runs` spends it on the
next launch by appending the ordinary `session_suspended` the crash never wrote.

Timing uses a monotonic clock, so a clock adjustment mid-attempt cannot produce
a negative solve time.
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

from . import catalog, events, srs, stats
from .catalog import Problem
from .scoring import fmt_duration

MAX_HINT_TIER = 4

# Phase 1 ships the hint *mechanism* — monotonic tiers, irreversible within an
# attempt, an event written before the text is rendered so a SIGKILL can't
# erase it — with canned text instead of a model. The tier contracts are §13's;
# Phase 3 swaps the text source and nothing else. Logging tiers from day one is
# what keeps `max_hint_tier` honest in scoring history.
HINT_STUBS = {
    1: (
        "Nudge — what category of insight does this need? Name it out loud "
        "before you write anything. (LLM hints arrive in Phase 3.)"
    ),
    2: (
        "Approach — which technique and which data structure? Say the pair "
        "aloud, then reason about why the obvious one fails. (Phase 3.)"
    ),
    3: (
        "Pseudocode — write the ten-line skeleton yourself before you look "
        "anything up. If you can't, that's the gap. (Phase 3.)"
    ),
    4: (
        "Solution — this tier ends the attempt as `gave_up` regardless of what "
        "happens next. Go read it properly, then write the reflection note."
    ),
}


Clock = Callable[[], float]


@dataclass
class Attempt:
    """Live state for the problem currently on screen."""

    uuid: str
    id: int
    problem: Problem
    is_review: bool = False
    clock: Clock = time.monotonic
    started_monotonic: float = 0.0
    paused_at: float | None = None
    paused_seconds: float = 0.0
    max_hint_tier: int = 0
    submissions: int = 0
    #: Every submit logged, pass or fail — `submissions` counts only the fails.
    #: This is the one that numbers the archived wrong answers, so it can never
    #: reuse a number and overwrite a file.
    submits_logged: int = 0
    #: Which pass at this problem is on screen. 1 until you choose to solve it
    #: again, and the number that names both the `resolves` row and its file.
    solves: int = 1
    finished: bool = False
    final_timing: dict[str, int] | None = None

    def __post_init__(self) -> None:
        if not self.started_monotonic:
            self.started_monotonic = self.clock()

    @property
    def paused(self) -> bool:
        return self.paused_at is not None

    @property
    def wall_seconds(self) -> int:
        if self.final_timing:
            return self.final_timing["wall_seconds"]
        return max(0, int(self.clock() - self.started_monotonic))

    @property
    def active_seconds(self) -> int:
        """Wall time minus everything spent paused (spec §16.1)."""
        if self.final_timing:
            return self.final_timing["active_seconds"]
        return max(0, int(self.clock() - self.started_monotonic - self._paused_now()))

    @property
    def total_paused_seconds(self) -> int:
        if self.final_timing:
            return self.final_timing["paused_seconds"]
        return int(self._paused_now())

    def _paused_now(self) -> float:
        paused = self.paused_seconds
        if self.paused_at is not None:
            paused += self.clock() - self.paused_at
        return paused

    def timing(self) -> dict[str, int]:
        """Freeze the clock readings for this instant.

        Taken the moment you press finish/give-up, so the time spent in the
        verdict prompt afterwards is neither billed as solve time nor recorded
        as a pause you never took. Once stored on `final_timing` the attempt's
        clock is stopped for good.
        """
        if self.final_timing:
            return dict(self.final_timing)
        return {
            "active_seconds": self.active_seconds,
            "wall_seconds": self.wall_seconds,
            "paused_seconds": self.total_paused_seconds,
        }


@dataclass
class Session:
    uuid: str
    id: int
    planned_n: int
    slugs: list[str]
    index: int = 0

    @property
    def remaining(self) -> list[str]:
        return self.slugs[self.index :]


class SessionError(RuntimeError):
    pass


@dataclass(frozen=True)
class SuspendedRun:
    """A run waiting to be picked up, as the home screen needs to describe it.

    Read straight off the `sessions` projection rather than reconstructed by the
    engine, because the home screen has to answer "is there one?" on every mount
    without starting anything.
    """

    session_uuid: str
    slugs: list[str] = field(default_factory=list)
    index: int = 0
    speech_mode: bool = False
    suspended_at: str = ""
    #: The problem left on screen, if the break was taken mid-attempt.
    title: str | None = None
    active_seconds: int = 0

    @property
    def summary(self) -> str:
        """One line: where you were, and how long ago that was."""
        where = f"problem {min(self.index + 1, len(self.slugs))} of {len(self.slugs)}"
        bits = [where]
        if self.title:
            bits.append(f"{self.title}, {fmt_duration(self.active_seconds)} in")
        else:
            bits.append("not started")
        if self.suspended_at:
            bits.append(stats.fmt_ago(self.suspended_at))
        return "  ·  ".join(bits)


def suspended_run(conn: sqlite3.Connection) -> SuspendedRun | None:
    """The run waiting to be resumed, if there is one.

    At most one: starting or resuming a run refuses while a session is live, so
    two suspended runs cannot both be reachable. The newest wins regardless, so
    a database that somehow holds two is still openable.
    """
    row = conn.execute(
        "SELECT * FROM sessions WHERE suspended_at IS NOT NULL AND ended_at IS NULL "
        "ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if row is None:
        return None
    attempt = conn.execute(
        "SELECT a.slug, a.active_seconds, p.title FROM attempts a "
        "JOIN problems p ON p.slug = a.slug "
        "WHERE a.session_id = ? AND a.ended_at IS NULL ORDER BY a.id DESC LIMIT 1",
        (row["id"],),
    ).fetchone()
    return SuspendedRun(
        session_uuid=row["uuid"],
        slugs=json.loads(row["slugs"] or "[]"),
        index=int(row["resume_index"] or 0),
        speech_mode=bool(row["speech_mode"]),
        suspended_at=row["suspended_at"],
        title=attempt["title"] if attempt else None,
        active_seconds=int(attempt["active_seconds"] or 0) if attempt else 0,
    )


def infer_outcome(conn: sqlite3.Connection, session_id: int, planned_n: int) -> str:
    """How a run ended, counted off the attempts it actually finished.

    Module-level because `recover_crashed_runs` has to answer it for a session
    no engine is holding — a run whose process died a fortnight ago.
    """
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM attempts WHERE session_id = ? AND ended_at IS NOT NULL",
        (session_id,),
    ).fetchone()
    done = int(row["n"])
    if done == 0:
        return "abandoned"
    return "completed" if done >= planned_n else "partial"


def recover_crashed_runs(conn: sqlite3.Connection) -> str | None:
    """Turn runs the app never closed into ones it can offer back.

    A run that was put down by hand wrote `session_suspended` on the way out; one
    whose process was killed — the terminal window closed, `kill -9`, a machine
    that lost power — wrote nothing, and its session row is left neither ended
    nor suspended. `suspended_run` cannot see that, so the run reads as if it
    never happened, and every problem in it stays out of the queue for good
    (`queues._attempted_slugs` counts a NULL verdict as attempted).

    So the crash is converted here into the suspend it should have been, and the
    whole resume path downstream — the `c` row on home, `resume_session`,
    `_rehydrate`, `Recorder.adopt` — carries on not knowing the difference. What
    the run was doing when it died comes from `run_checkpoint`; a run that
    predates that table is still recovered, just without its clock.

    Only the newest is handed back. The older ones are sealed with the outcome
    their own attempts imply, because a run you crashed out of three weeks ago is
    not one you are coming back to — and being asked about it nine times, once
    per launch, is worse than not being asked. Nothing is graded and no verdict
    is written either way: those attempts keep their NULL verdicts, exactly as
    they have them now.

    Called once, from `CoreApp.on_mount`, before anything reads `sessions`.
    Returns the uuid of the run left resumable, if there is one.
    """
    orphans = conn.execute(
        "SELECT * FROM sessions WHERE ended_at IS NULL AND suspended_at IS NULL "
        "ORDER BY id DESC"
    ).fetchall()
    check = conn.execute("SELECT * FROM run_checkpoint WHERE id = 1").fetchone()

    resumable: str | None = None
    for n, row in enumerate(orphans):
        if n == 0:
            _suspend_crashed(conn, row, check)
            resumable = row["uuid"]
        else:
            _seal_crashed(conn, row)

    # Unconditionally, even when nothing was found: `CoreApp.on_unmount` swallows
    # its own failures, so a cleanly ended run can still leave a row behind, and
    # a stale one would put the wrong clock on the next crash.
    conn.execute("DELETE FROM run_checkpoint")
    return resumable


def _suspend_crashed(
    conn: sqlite3.Connection, row: sqlite3.Row, check: sqlite3.Row | None
) -> None:
    """Write the `session_suspended` the crash never got to write."""
    if check is None or check["session_uuid"] != row["uuid"]:
        # No checkpoint, or one belonging to some other run: everything below has
        # to come off the projections instead. This is the path every session
        # that predates the checkpoint table takes.
        check = None

    if check is not None:
        index = int(check["resume_index"])
        when = check["updated_at"]
    else:
        done = conn.execute(
            "SELECT COUNT(*) AS n FROM attempts "
            "WHERE session_id = ? AND ended_at IS NOT NULL",
            (row["id"],),
        ).fetchone()
        index = int(done["n"])
        last = conn.execute(
            "SELECT started_at FROM attempts WHERE session_id = ? ORDER BY id DESC LIMIT 1",
            (row["id"],),
        ).fetchone()
        # The last thing known to have happened, rather than now: `fmt_ago` on the
        # home row and the `away_seconds` a resume measures are both read off
        # this, and dating the crash to this launch would claim you were sitting
        # in front of it the whole time.
        when = (last["started_at"] if last else None) or row["started_at"]

    payload: dict[str, Any] = {"session_uuid": row["uuid"], "suspended_at": when}
    attempt = conn.execute(
        "SELECT * FROM attempts WHERE session_id = ? AND ended_at IS NULL "
        "ORDER BY id DESC LIMIT 1",
        (row["id"],),
    ).fetchone()

    live = (
        attempt is not None
        and check is not None
        and check["attempt_uuid"] == attempt["uuid"]
        and not check["attempt_finished"]
        and int(check["solves"]) == 1
    )
    if live:
        assert check is not None
        # Same nesting as `suspend_session`, for the reason given in
        # `events.apply`: the cursor above outlives this attempt being discarded.
        payload["attempt"] = {
            "uuid": attempt["uuid"],
            "active_seconds": int(check["active_seconds"] or 0),
            "wall_seconds": int(check["wall_seconds"] or 0),
            "paused_seconds": int(check["paused_seconds"] or 0),
        }
    elif check is not None and (check["attempt_finished"] or int(check["solves"]) > 1):
        # Either the process died between `finish` and `advance` — in the editor,
        # most likely — or it died on a second pass at the same problem. Both end
        # the same way: the problem is graded, so the cursor moves past it rather
        # than handing it back. A re-solve loses the re-solve and nothing else,
        # which is what `solve_again` already promises; writing its clock would
        # land on the first pass's row, which is why `suspend_session` refuses to
        # do it at all.
        index += 1
    # Anything else — an attempt with no matching checkpoint — falls through with
    # no timing block. The row keeps its NULL seconds and `_rehydrate` reads them
    # as zero, so the problem comes back at 00:00: nothing was ever written down,
    # and inventing a number would be worse than admitting that.

    payload["index"] = index
    events.append(conn, events.SESSION_SUSPENDED, payload)


def _seal_crashed(conn: sqlite3.Connection, row: sqlite3.Row) -> None:
    """Close out an older crashed run, without judging anything inside it."""
    # Read before anything below closes an attempt, so a run that finished
    # nothing is still recorded as having finished nothing.
    outcome = infer_outcome(conn, int(row["id"]), int(row["planned_n"] or 0))

    # The problem that was on screen when the process died is closed as
    # `ungraded` -- the verdict that exists for exactly this, and the one thing
    # here that is not merely bookkeeping. Left with a NULL verdict it counts as
    # attempted forever (`queues._attempted_slugs`) while never being graded, so
    # it schedules no card and never comes due: the problem falls out of the
    # unseen pool and out of the queue, permanently, with no way back short of
    # picking it by hand. `ungraded` is the honest way to say nothing judged it,
    # and it is the one verdict that pool skips. It schedules nothing either --
    # `srs.grade_attempt` returns on `UNSCHEDULED_VERDICTS` before it touches a
    # card -- so this closes the attempt without inventing a score for it.
    for open_attempt in conn.execute(
        "SELECT uuid, slug, started_at FROM attempts "
        "WHERE session_id = ? AND ended_at IS NULL ORDER BY id",
        (row["id"],),
    ).fetchall():
        events.append(
            conn,
            events.PROBLEM_FINISHED,
            {
                "attempt_uuid": open_attempt["uuid"],
                "slug": open_attempt["slug"],
                "verdict": "ungraded",
                # Nothing was ever measured, so the answer is zero rather than a
                # span invented out of two timestamps that mean nothing.
                "active_seconds": 0,
                "wall_seconds": 0,
                "paused_seconds": 0,
                "ended_at": open_attempt["started_at"],
            },
        )

    last = conn.execute(
        "SELECT ended_at FROM attempts WHERE session_id = ? AND ended_at IS NOT NULL "
        "ORDER BY id DESC LIMIT 1",
        (row["id"],),
    ).fetchone()
    events.append(
        conn,
        events.SESSION_ENDED,
        {
            "session_uuid": row["uuid"],
            "outcome": outcome,
            "session_note": None,
            # Dated to the run, not to this launch. `stats.load_runs` reads it,
            # and a run from a fortnight ago did not end tonight.
            "ended_at": (last["ended_at"] if last else None) or row["started_at"],
        },
    )


class RunEngine:
    """Owns one run. Append-only: every method below writes an event."""

    def __init__(self, conn: sqlite3.Connection, clock: Clock = time.monotonic):
        self.conn = conn
        self.clock = clock
        self.session: Session | None = None
        self.attempt: Attempt | None = None

    # --- crash recovery ---------------------------------------------------

    def checkpoint(self) -> None:
        """Write down where the run is, outside the log, in case this is the end.

        The one direct row write in this class, and the module docstring says
        why. Cheap by construction: a single-row UPSERT against a table nothing
        else touches, called on every cursor move and on a timer in between.

        Silent when there is no session -- callers reach this from a timer and
        from teardown paths, and neither should have to check first.
        """
        session = self.session
        if session is None:
            return
        a = self.attempt
        timing = a.timing() if a is not None else {}
        self.conn.execute(
            "INSERT INTO run_checkpoint("
            "  id, session_uuid, attempt_uuid, resume_index, solves,"
            "  attempt_finished, active_seconds, wall_seconds, paused_seconds, updated_at"
            ") VALUES(1,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET "
            "  session_uuid = excluded.session_uuid,"
            "  attempt_uuid = excluded.attempt_uuid,"
            "  resume_index = excluded.resume_index,"
            "  solves = excluded.solves,"
            "  attempt_finished = excluded.attempt_finished,"
            "  active_seconds = excluded.active_seconds,"
            "  wall_seconds = excluded.wall_seconds,"
            "  paused_seconds = excluded.paused_seconds,"
            "  updated_at = excluded.updated_at",
            (
                session.uuid,
                a.uuid if a is not None else None,
                session.index,
                a.solves if a is not None else 1,
                int(a.finished) if a is not None else 0,
                timing.get("active_seconds"),
                timing.get("wall_seconds"),
                timing.get("paused_seconds"),
                events.utc_now(),
            ),
        )

    def clear_checkpoint(self) -> None:
        """The run is accounted for in the log. There is nothing to come back to."""
        self.conn.execute("DELETE FROM run_checkpoint")

    # --- session ----------------------------------------------------------

    def start_session(
        self, slugs: list[str], planned_n: int | None = None, *, speech_mode: bool = False
    ) -> Session:
        if self.session is not None:
            raise SessionError("a session is already in progress")
        session_uuid = events.new_uuid()
        events.append(
            self.conn,
            events.SESSION_STARTED,
            {
                "session_uuid": session_uuid,
                "planned_n": planned_n if planned_n is not None else len(slugs),
                "slugs": slugs,
                # Carried so a resume in a later process turns the microphone
                # back on exactly when this run did, rather than asking today's
                # setting what last week's run was doing.
                "speech_mode": int(speech_mode),
            },
        )
        row = self.conn.execute("SELECT id FROM sessions WHERE uuid = ?", (session_uuid,)).fetchone()
        self.session = Session(
            uuid=session_uuid,
            id=int(row["id"]),
            planned_n=planned_n if planned_n is not None else len(slugs),
            slugs=list(slugs),
        )
        # Immediately: a run killed before its first problem is still a run you
        # picked, and it comes back rather than vanishing.
        self.checkpoint()
        return self.session

    def end_session(self, outcome: str | None = None, session_note: str | None = None) -> None:
        if self.session is None:
            return
        if self.attempt is not None and not self.attempt.finished:
            self.abandon()
        if outcome is None:
            outcome = self._infer_outcome()
        events.append(
            self.conn,
            events.SESSION_ENDED,
            {
                "session_uuid": self.session.uuid,
                "outcome": outcome,
                "session_note": session_note,
            },
        )
        self.session = None
        self.clear_checkpoint()

    def suspend_session(self) -> None:
        """Put the run down. `resume_session` picks it up, in any later process.

        Deliberately not an ending: the attempt on screen keeps its NULL verdict,
        so nothing is graded, no card moves, and the problem stays out of the
        next queue. All that is written down is where the cursor was and what
        the clock read -- enough for `_rehydrate` to rebuild the attempt, and
        nothing that pretends the problem is over.
        """
        if self.session is None:
            return
        a = self.attempt
        if a is not None and a.solves > 1:
            # A suspend writes the attempt's clock readings back over
            # `attempts`, which on a later pass would overwrite the first pass's
            # timing with this one's. Finish or drop the re-solve first; the
            # solve screen says so rather than letting this be reached.
            raise SessionError("finish or drop this re-solve first")
        payload: dict[str, Any] = {
            "session_uuid": self.session.uuid,
            "index": self.session.index,
        }
        if a is not None and not a.finished:
            # Nested, not a top-level `attempt_uuid`: see the note in
            # `events.apply`. The cursor above has to survive this attempt being
            # thrown away later.
            payload["attempt"] = {"uuid": a.uuid, **a.timing()}
        elif a is not None:
            # Finished, but the capture flow was interrupted before `advance`.
            # There is nothing to resume into and the cursor has not moved yet,
            # so move it here rather than handing back a solved problem.
            payload["index"] = self.session.index + 1
        events.append(self.conn, events.SESSION_SUSPENDED, payload)
        self.session = None
        self.attempt = None
        # The log says everything the checkpoint did, and says it properly.
        self.clear_checkpoint()

    def resume_session(self, session_uuid: str) -> Session:
        """Reopen a suspended run, live attempt and all.

        The clock comes back **paused**. Reading yourself back into a problem you
        last saw eight hours ago is not solve time, and starting the timer the
        instant the screen draws would bill it as if it were.
        """
        if self.session is not None:
            raise SessionError("a session is already in progress")
        row = self.conn.execute(
            "SELECT * FROM sessions WHERE uuid = ? AND ended_at IS NULL", (session_uuid,)
        ).fetchone()
        if row is None:
            raise SessionError("no suspended run to resume")

        session = Session(
            uuid=session_uuid,
            id=int(row["id"]),
            planned_n=int(row["planned_n"] or 0),
            slugs=json.loads(row["slugs"] or "[]"),
            index=int(row["resume_index"] or 0),
        )
        attempt_row = self.conn.execute(
            "SELECT * FROM attempts WHERE session_id = ? AND ended_at IS NULL "
            "ORDER BY id DESC LIMIT 1",
            (session.id,),
        ).fetchone()
        attempt = None
        if attempt_row is not None:
            problem = catalog.get(self.conn, attempt_row["slug"])
            if problem is not None:
                attempt = self._rehydrate(attempt_row, problem)

        away = 0
        if row["suspended_at"]:
            gap = datetime.now(timezone.utc) - srs.parse_ts(row["suspended_at"])
            away = max(0, int(gap.total_seconds()))
        events.append(
            self.conn,
            events.SESSION_RESUMED,
            {
                "session_uuid": session_uuid,
                "index": session.index,
                "attempt": (
                    {"uuid": attempt.uuid, "away_seconds": away} if attempt else None
                ),
            },
        )
        self.session = session
        self.attempt = attempt
        # Re-armed straight away: a run resumed and then killed has to come back
        # a second time, not fall through the gap the first crash left.
        self.checkpoint()
        return session

    def _rehydrate(self, row: sqlite3.Row, problem: Problem) -> Attempt:
        """Rebuild the live attempt from its projection row.

        The monotonic clock died with the last process, so it is reconstructed
        from the two readings the suspend froze: put `started_monotonic` far
        enough back that `wall_seconds` reads `active + paused` again, and the
        properties take care of themselves.
        """
        active = int(row["active_seconds"] or 0)
        paused = int(row["paused_seconds"] or 0)
        now = self.clock()
        logged = self.conn.execute(
            "SELECT COUNT(*) AS n FROM submissions WHERE attempt_uuid = ?", (row["uuid"],)
        ).fetchone()
        return Attempt(
            uuid=row["uuid"],
            id=int(row["id"]),
            problem=problem,
            is_review=bool(row["is_review"]),
            clock=self.clock,
            started_monotonic=now - active - paused,
            paused_at=now,
            paused_seconds=paused,
            max_hint_tier=int(row["max_hint_tier"] or 0),
            submissions=int(row["submissions"] or 0),
            # Every submit, not just the failures -- this is what names the next
            # archived wrong answer, so it must not restart at 1 and overwrite
            # a file the first half of the attempt already wrote.
            submits_logged=int(logged["n"]),
        )

    def _infer_outcome(self) -> str:
        assert self.session is not None
        return infer_outcome(self.conn, self.session.id, self.session.planned_n)

    # --- attempt ----------------------------------------------------------

    def start_problem(self, slug: str, is_review: bool | None = None) -> Attempt:
        """Begin an attempt. `is_review=None` asks the scheduler (spec §8).

        A problem with an FSRS card in a review state is a review, and reviews
        are worth `review_mult` because retention is the thing being trained.
        Passing an explicit True/False still wins, which is what keeps the
        callers that already knew the answer — and the tests — unaffected.

        The answer is resolved here, at write time, and travels in the
        `problem_started` payload. `events.apply` reads it back out rather than
        recomputing it, so replaying an old log cannot relabel history with
        today's cards.
        """
        if self.session is None:
            raise SessionError("no session in progress")
        if self.attempt is not None and not self.attempt.finished:
            raise SessionError("finish the current problem first")
        problem = catalog.get(self.conn, slug)
        if problem is None:
            raise SessionError(f"unknown problem: {slug}")
        if is_review is None:
            is_review = srs.is_due_review(self.conn, slug)

        attempt_uuid = events.new_uuid()
        events.append(
            self.conn,
            events.PROBLEM_STARTED,
            {
                "session_uuid": self.session.uuid,
                "attempt_uuid": attempt_uuid,
                "slug": slug,
                "is_review": int(is_review),
            },
        )
        row = self.conn.execute("SELECT id FROM attempts WHERE uuid = ?", (attempt_uuid,)).fetchone()
        self.attempt = Attempt(
            uuid=attempt_uuid,
            id=int(row["id"]),
            problem=problem,
            is_review=is_review,
            clock=self.clock,
        )
        self.checkpoint()
        return self.attempt

    def pause(self) -> None:
        a = self._require_attempt()
        if a.paused_at is None:
            a.paused_at = self.clock()

    def resume(self) -> None:
        a = self._require_attempt()
        if a.paused_at is not None:
            a.paused_seconds += self.clock() - a.paused_at
            a.paused_at = None

    def toggle_pause(self) -> bool:
        a = self._require_attempt()
        if a.paused:
            self.resume()
        else:
            self.pause()
        return a.paused

    def reveal_hint(self) -> tuple[int, str]:
        """Reveal the next tier. Monotonic and irreversible within an attempt.

        The event is written *before* the text is returned, so the hint can
        never be seen without being recorded.
        """
        a = self._require_attempt()
        if a.max_hint_tier >= MAX_HINT_TIER:
            return a.max_hint_tier, HINT_STUBS[MAX_HINT_TIER]
        tier = a.max_hint_tier + 1
        events.append(
            self.conn,
            events.HINT_REVEALED,
            {"attempt_uuid": a.uuid, "slug": a.problem.slug, "tier": tier},
        )
        a.max_hint_tier = tier
        # Tier 4 is the full solution: it ends the attempt as `gave_up`
        # regardless of what happens next (spec §13). Recorded before the text
        # is returned, so reading it can never go unlogged.
        #
        # Not on a later pass. The rule exists to guarantee the tier scores
        # zero, and a re-solve is not scored at all -- ending it here would only
        # take away the verdict prompt for no gain.
        if tier == MAX_HINT_TIER and a.solves == 1:
            self.abandon()
        return tier, HINT_STUBS[tier]

    def record_submission(self, verdict: str) -> int:
        """Log a submission to LeetCode. Only failures count against you.

        Returns the submission's number within the attempt, which is also what
        names its archived code — so the caller can hand it to `capture` and to
        `archive_submission` without counting anything itself.
        """
        a = self._require_attempt()
        a.submits_logged += 1
        events.append(
            self.conn,
            events.PROBLEM_SUBMITTED,
            {
                "attempt_uuid": a.uuid,
                "slug": a.problem.slug,
                "verdict": verdict,
                "n": a.submits_logged,
            },
        )
        # The submission verdict is the judge's word on one submit — `accepted`
        # or `wrong_answer`. It is not the attempt verdict (`scoring.VERDICTS`),
        # which records how much help you needed to get there.
        if verdict != "accepted":
            a.submissions += 1
        return a.submits_logged

    def finish(
        self,
        verdict: str,
        *,
        timing: dict[str, int] | None = None,
        self_confidence: int | None = None,
        lc_runtime_pct: float | None = None,
        lc_memory_pct: float | None = None,
        language: str | None = None,
        claimed_complexity: str | None = None,
        claimed_space_complexity: str | None = None,
        time_optimality: str | None = None,
        space_optimality: str | None = None,
        strategies: dict[str, list[str]] | None = None,
        methods: list[dict] | None = None,
    ) -> Attempt:
        a = self._require_attempt()
        if a.solves > 1:
            # A later pass at the same problem. Same prompts, same answers, a
            # different event -- and `gave_up` is not routed to `abandon` here,
            # because abandoning would re-end an attempt that already ended and
            # grade a card that is already scheduled.
            return self._finish_resolve(
                a,
                verdict,
                timing=timing,
                self_confidence=self_confidence,
                lc_runtime_pct=lc_runtime_pct,
                lc_memory_pct=lc_memory_pct,
                language=language,
                claimed_complexity=claimed_complexity,
                claimed_space_complexity=claimed_space_complexity,
                time_optimality=time_optimality,
                space_optimality=space_optimality,
                strategies=strategies,
                methods=methods,
            )
        if verdict == "gave_up":
            # Nothing you claim about a solution survives not having reached
            # one, so the complexities, the optimality answers and both post-
            # solve blocks are dropped rather than carried onto an abandonment.
            return self.abandon(timing=timing)
        a.final_timing = timing or a.timing()
        events.append(
            self.conn,
            events.PROBLEM_FINISHED,
            {
                "attempt_uuid": a.uuid,
                "slug": a.problem.slug,
                "verdict": verdict,
                **a.final_timing,
                "self_confidence": self_confidence,
                "lc_runtime_pct": lc_runtime_pct,
                "lc_memory_pct": lc_memory_pct,
                "language": language,
                "claimed_complexity": claimed_complexity,
                "claimed_space_complexity": claimed_space_complexity,
                "time_optimality": time_optimality,
                "space_optimality": space_optimality,
                # The patterns you reached for, as you typed them. Rides on this
                # payload rather than a later event so that `events.apply` can
                # write the rows before it grades the card -- `srs.rate` reads
                # them. Omitted entirely when you skipped the prompt, so an old
                # event and a skipped one are the same shape.
                **({"strategies": strategies} if strategies else {}),
                # The problem's own list of methods, with what each one costs and
                # which one you wrote. Same payload for the same reason, and a
                # sharper one: `saw_better` asks whether an optimal method is
                # recorded here that is not the one you wrote, so these rows have
                # to exist before the fold reaches `_grade`.
                **({"methods": methods} if methods else {}),
            },
        )
        a.finished = True
        # Before the capture flow, not after. What follows is two `$EDITOR`
        # handoffs through `App.suspend()`, and the message pump — and with it
        # the checkpoint timer — is parked for the whole of it.
        self.checkpoint()
        return a

    def solve_again(self) -> Attempt:
        """Reopen the finished attempt for another pass at the same problem.

        The clock restarts from zero rather than picking the old one back up:
        this is a fresh solve, and how long the second one took is the whole
        point of recording it. The hint tier and the failed submits stay --
        those happened, and no second pass makes them not have.

        Writes no event. Nothing is recorded until the pass finishes, exactly as
        nothing is recorded before the first `finish`, so dropping out of a
        re-solve costs the re-solve and nothing else: the attempt behind it was
        sealed the moment `problem_finished` was appended.
        """
        a = self._require_attempt()
        if not a.finished:
            raise SessionError("finish this solve first")
        a.solves += 1
        a.finished = False
        a.final_timing = None
        a.started_monotonic = self.clock()
        a.paused_at = None
        a.paused_seconds = 0.0
        self.checkpoint()
        return a

    def _finish_resolve(
        self,
        a: Attempt,
        verdict: str,
        *,
        timing: dict[str, int] | None = None,
        self_confidence: int | None = None,
        lc_runtime_pct: float | None = None,
        lc_memory_pct: float | None = None,
        language: str | None = None,
        claimed_complexity: str | None = None,
        claimed_space_complexity: str | None = None,
        time_optimality: str | None = None,
        space_optimality: str | None = None,
        strategies: dict[str, list[str]] | None = None,
        methods: list[dict] | None = None,
    ) -> Attempt:
        """End a later pass. Recorded beside the attempt, never over it.

        Carries the same answers a finish does, including the two post-solve
        blocks -- naming a second method tonight is exactly the thing the
        methods list wants to hear about. What it does not carry is a
        grading: see the `problem_resolved` branch of `events.apply`.

        A `gave_up` pass is a `resolves` row that says so, not a
        `problem_abandoned`. The attempt's own verdict is the first pass's and
        stays the first pass's.
        """
        a.final_timing = timing or a.timing()
        events.append(
            self.conn,
            events.PROBLEM_RESOLVED,
            {
                "attempt_uuid": a.uuid,
                "slug": a.problem.slug,
                "n": a.solves,
                "verdict": verdict,
                **a.final_timing,
                "self_confidence": self_confidence,
                "lc_runtime_pct": lc_runtime_pct,
                "lc_memory_pct": lc_memory_pct,
                "language": language,
                "claimed_complexity": claimed_complexity,
                "claimed_space_complexity": claimed_space_complexity,
                "time_optimality": time_optimality,
                "space_optimality": space_optimality,
                **({"strategies": strategies} if strategies else {}),
                **({"methods": methods} if methods else {}),
            },
        )
        a.finished = True
        self.checkpoint()
        return a

    def abandon(self, timing: dict[str, int] | None = None) -> Attempt:
        """Give up. Scores 0, but the attempt is still logged (spec §5)."""
        a = self._require_attempt()
        if a.solves > 1:
            # Giving up on a second pass ends the pass, not the attempt. The
            # attempt's verdict was settled by the first one and history is not
            # rewritten by how tonight's rerun went.
            return self._finish_resolve(a, "gave_up", timing=timing)
        a.final_timing = timing or a.timing()
        events.append(
            self.conn,
            events.PROBLEM_ABANDONED,
            {
                "attempt_uuid": a.uuid,
                "slug": a.problem.slug,
                **a.final_timing,
            },
        )
        a.finished = True
        self.checkpoint()
        return a

    def discard(self) -> None:
        """Throw the attempt away: it is not recorded at all.

        For the misfire — the wrong problem opened, the timer left running over
        lunch, the `f` you did not mean. The events stay in the log, because the
        log is not a lie; the projections forget the attempt, so it never
        reaches your history, your score, or your review schedule.

        Refuses an attempt that is already finished. A finished attempt has been
        graded, and its card would need unwinding -- which `discard` cannot do.
        Nothing can reach this with a finished attempt today: the finish modal
        is the only caller, and it only opens while the attempt is live.
        """
        a = self._require_attempt()
        if a.solves > 1:
            # Throwing away a re-solve throws away the re-solve. Nothing has
            # been written for this pass yet, so there is nothing to tombstone
            # -- the attempt goes back to being the sealed thing it was.
            a.solves -= 1
            a.finished = True
            self.checkpoint()
            return
        if a.finished:
            raise RuntimeError("cannot discard a finished attempt")
        events.append(
            self.conn,
            events.ATTEMPT_DISCARDED,
            {"attempt_uuid": a.uuid, "slug": a.problem.slug},
        )
        self.attempt = None
        self.checkpoint()

    def archive_code(
        self, path: str, language: str | None = None, method: str | None = None
    ) -> None:
        """Attach the archived file to the current attempt, tagged with a method.

        `method` is the name as you typed it, not a key: the key is derived in
        `events._record_method_code`, on the same bargain as every other
        projection -- a change to `methods.normalise` is one replay from applying
        to everything already logged. None is normal: a solve where you named no
        method still archives its file.
        """
        a = self._require_attempt()
        events.append(
            self.conn,
            events.CODE_ARCHIVED,
            {
                "attempt_uuid": a.uuid,
                "slug": a.problem.slug,
                "code_path": path,
                "language": language,
                "method": method,
                # Which pass wrote it. Omitted on the first one, so the payload
                # of a plain solve is the shape it has always been.
                **({"resolve_n": a.solves} if a.solves > 1 else {}),
            },
        )

    def archive_submission(self, path: str, n: int, language: str | None = None) -> None:
        """Attach archived code to submission `n` of the current attempt."""
        a = self._require_attempt()
        events.append(
            self.conn,
            events.SUBMISSION_ARCHIVED,
            {
                "attempt_uuid": a.uuid,
                "slug": a.problem.slug,
                "n": n,
                "code_path": path,
                "language": language,
            },
        )

    def record_note(self, path: str) -> None:
        a = self._require_attempt()
        events.append(
            self.conn,
            events.NOTE_WRITTEN,
            {
                "attempt_uuid": a.uuid,
                "slug": a.problem.slug,
                "note_path": path,
                **({"resolve_n": a.solves} if a.solves > 1 else {}),
            },
        )

    def record_audio(self, path: str) -> None:
        """Attach a speech-mode recording to the attempt it belongs to."""
        a = self._require_attempt()
        events.append(
            self.conn,
            events.AUDIO_RECORDED,
            {"attempt_uuid": a.uuid, "slug": a.problem.slug, "audio_path": path},
        )

    def advance(self) -> None:
        """Move the cursor past the problem just finished."""
        if self.session is not None:
            self.session.index += 1
        self.attempt = None
        self.checkpoint()

    # --- reads ------------------------------------------------------------

    def attempt_row(self, attempt_id: int | None = None) -> dict[str, Any] | None:
        target = attempt_id if attempt_id is not None else (self.attempt.id if self.attempt else None)
        if target is None:
            return None
        row = self.conn.execute(
            "SELECT a.*, p.difficulty, p.title FROM attempts a "
            "JOIN problems p ON p.slug = a.slug WHERE a.id = ?",
            (target,),
        ).fetchone()
        return dict(row) if row else None

    def _require_attempt(self) -> Attempt:
        if self.attempt is None:
            raise SessionError("no attempt in progress")
        return self.attempt
