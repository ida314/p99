"""The run engine: timer, hint tiers, attempt state machine (spec §15.3).

All state changes go through here, and every one of them is an event append —
the engine never writes a projection row directly. The TUI is a view over this.

Timing uses a monotonic clock, so a clock adjustment mid-attempt cannot produce
a negative solve time.
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from typing import Any, Callable

from . import catalog, events, srs
from .catalog import Problem

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


class RunEngine:
    """Owns one run. Append-only: every method below writes an event."""

    def __init__(self, conn: sqlite3.Connection, clock: Clock = time.monotonic):
        self.conn = conn
        self.clock = clock
        self.session: Session | None = None
        self.attempt: Attempt | None = None

    # --- session ----------------------------------------------------------

    def start_session(self, slugs: list[str], planned_n: int | None = None) -> Session:
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
            },
        )
        row = self.conn.execute("SELECT id FROM sessions WHERE uuid = ?", (session_uuid,)).fetchone()
        self.session = Session(
            uuid=session_uuid,
            id=int(row["id"]),
            planned_n=planned_n if planned_n is not None else len(slugs),
            slugs=list(slugs),
        )
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

    def _infer_outcome(self) -> str:
        assert self.session is not None
        row = self.conn.execute(
            "SELECT COUNT(*) AS n FROM attempts WHERE session_id = ? AND ended_at IS NOT NULL",
            (self.session.id,),
        ).fetchone()
        done = int(row["n"])
        if done == 0:
            return "abandoned"
        return "completed" if done >= self.session.planned_n else "partial"

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
        if tier == MAX_HINT_TIER:
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
        optimality: str | None = None,
    ) -> Attempt:
        a = self._require_attempt()
        if verdict == "gave_up":
            # Nothing you claim about a solution survives not having reached
            # one, so the complexity and the optimality answer are dropped
            # rather than carried onto an abandonment.
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
                "optimality": optimality,
            },
        )
        a.finished = True
        return a

    def abandon(self, timing: dict[str, int] | None = None) -> Attempt:
        """Give up. Scores 0, but the attempt is still logged (spec §5)."""
        a = self._require_attempt()
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
        if a.finished:
            raise RuntimeError("cannot discard a finished attempt")
        events.append(
            self.conn,
            events.ATTEMPT_DISCARDED,
            {"attempt_uuid": a.uuid, "slug": a.problem.slug},
        )
        self.attempt = None

    def archive_code(self, path: str, language: str | None = None) -> None:
        a = self._require_attempt()
        events.append(
            self.conn,
            events.CODE_ARCHIVED,
            {
                "attempt_uuid": a.uuid,
                "slug": a.problem.slug,
                "code_path": path,
                "language": language,
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
            {"attempt_uuid": a.uuid, "slug": a.problem.slug, "note_path": path},
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
