"""Append-only event log and the replay that builds projections (spec §4).

The rule the whole design rests on: **the app only ever appends.** Every row in
`sessions` and `attempts` is a function of the log, produced by `apply()`.
`append()` writes the event and then calls the same `apply()` the replay uses,
so the live projections and a from-scratch rebuild cannot diverge.

That is what makes projection bugs retroactively fixable: fix `apply`, run the
`replay` command, and all history is corrected.
"""

from __future__ import annotations

import json
import sqlite3
import uuid as uuidlib
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from . import scoring, srs, strategies
from .db import SCHEMA_VERSION, truncate_projections

# --- event types (spec §4) -------------------------------------------------

SESSION_STARTED = "session_started"
PROBLEM_STARTED = "problem_started"
HINT_REVEALED = "hint_revealed"
PROBLEM_SUBMITTED = "problem_submitted"
PROBLEM_ABANDONED = "problem_abandoned"
PROBLEM_FINISHED = "problem_finished"
CODE_ARCHIVED = "code_archived"
SUBMISSION_ARCHIVED = "submission_archived"
#: Code for one approach to a problem, written outside any attempt -- the
#: library screen filling in a route you named and never sat down and wrote.
SOLUTION_ARCHIVED = "solution_archived"
NOTE_WRITTEN = "note_written"
AUDIO_RECORDED = "audio_recorded"
SESSION_ENDED = "session_ended"
# Put the run down and pick it up in a later process. Not an ending: the attempt
# on screen keeps its NULL verdict, which is what stops a break from being
# scored as a `gave_up` it never was.
SESSION_SUSPENDED = "session_suspended"
SESSION_RESUMED = "session_resumed"
REVIEW_COMPLETED = "review_completed"   # Phase 3
MEMORY_UPDATED = "memory_updated"       # Phase 3
QUEUE_GENERATED = "queue_generated"     # Phase 2
SETTINGS_CHANGED = "settings_changed"

# Tombstones. The log stays append-only -- these do not erase anything that was
# written, they record a decision that what was written should not count. The
# projections then forget it, and `replay` skips every event addressed to it.
ATTEMPT_DISCARDED = "attempt_discarded"
RUN_DELETED = "run_deleted"

EVENT_TYPES = frozenset(
    {
        SESSION_STARTED,
        PROBLEM_STARTED,
        HINT_REVEALED,
        PROBLEM_SUBMITTED,
        PROBLEM_ABANDONED,
        PROBLEM_FINISHED,
        CODE_ARCHIVED,
        SUBMISSION_ARCHIVED,
        SOLUTION_ARCHIVED,
        NOTE_WRITTEN,
        AUDIO_RECORDED,
        SESSION_ENDED,
        SESSION_SUSPENDED,
        SESSION_RESUMED,
        REVIEW_COMPLETED,
        MEMORY_UPDATED,
        QUEUE_GENERATED,
        SETTINGS_CHANGED,
        ATTEMPT_DISCARDED,
        RUN_DELETED,
    }
)


@dataclass(frozen=True)
class Event:
    id: int
    uuid: str
    ts: str
    type: str
    payload: dict[str, Any]
    schema_ver: int


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_uuid() -> str:
    return str(uuidlib.uuid4())


# --- writing ---------------------------------------------------------------


def append(
    conn: sqlite3.Connection,
    type: str,
    payload: dict[str, Any],
    *,
    ts: str | None = None,
    event_uuid: str | None = None,
) -> Event:
    """Append an event and fold it into the projections."""
    if type not in EVENT_TYPES:
        raise ValueError(f"unknown event type: {type!r}")
    event_uuid = event_uuid or new_uuid()
    ts = ts or utc_now()

    # The write and its projection are one transaction. If they were not, a
    # crash between them would leave an event whose effect is missing from the
    # projections forever — and the next `replay` would renumber every attempt
    # after it, orphaning the `code/<slug>/<attempt_id>` files already on disk.
    conn.execute("BEGIN IMMEDIATE")
    try:
        cur = conn.execute(
            "INSERT INTO events(uuid, ts, type, payload, schema_ver) VALUES(?,?,?,?,?)",
            (event_uuid, ts, type, json.dumps(payload, sort_keys=True), SCHEMA_VERSION),
        )
        stored = Event(
            id=int(cur.lastrowid),
            uuid=event_uuid,
            ts=ts,
            type=type,
            payload=payload,
            schema_ver=SCHEMA_VERSION,
        )
        apply(conn, stored)
    except Exception:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")
    return stored


def read_all(conn: sqlite3.Connection) -> list[Event]:
    rows = conn.execute(
        "SELECT id, uuid, ts, type, payload, schema_ver FROM events ORDER BY id"
    ).fetchall()
    return [
        Event(
            id=r["id"],
            uuid=r["uuid"],
            ts=r["ts"],
            type=r["type"],
            payload=json.loads(r["payload"]),
            schema_ver=r["schema_ver"],
        )
        for r in rows
    ]


# --- projection ------------------------------------------------------------


def _session_id(conn: sqlite3.Connection, session_uuid: str) -> int | None:
    row = conn.execute("SELECT id FROM sessions WHERE uuid = ?", (session_uuid,)).fetchone()
    return row["id"] if row else None


def _attempt_id(conn: sqlite3.Connection, attempt_uuid: str) -> int | None:
    row = conn.execute("SELECT id FROM attempts WHERE uuid = ?", (attempt_uuid,)).fetchone()
    return row["id"] if row else None


def _setting(conn: sqlite3.Connection, key: str, default: str) -> str:
    """Read one in-app override straight from the projection.

    The settings-table layer only. `srs_context` puts the config *file* under
    it; this stays a bare projection read so it cannot fail on a broken file.
    """
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    if row is None:
        return default
    try:
        value = json.loads(row["value"])
    except (TypeError, ValueError):
        return default
    return value if isinstance(value, str) else default


def srs_context(conn: sqlite3.Connection) -> tuple[srs.Params, scoring.Weights]:
    """The parameters the card projection grades with.

    `replay` resolves this *before* truncating, so a rebuild grades all of
    history with the settings in force now rather than replaying each
    `settings_changed` event as it goes. Switching `srs.params` from v1 to v2
    and replaying is meant to reschedule everything, not to leave the first half
    of your history on the old model.

    Both layers, in `config`'s own order: the file, then the settings screen on
    top of it. This used to read the settings table alone, which meant a
    `params` line in `config.toml` was honoured by every screen that reports the
    schedule and by nothing that computes it -- `p99 doctor` could name one
    parameter file while the cards were graded under another. The import is
    function-local because `config` imports this module; resolving it at call
    time rather than at import time is what makes that legal, and it is the same
    trick `config.options` already uses to reach `scoring` and `srs`.
    """
    from . import config

    cfg = config.load(conn)
    return (
        srs.load_params(cfg.srs.params),
        scoring.load_weights(cfg.scoring.weights),
    )


def _update_attempt(conn: sqlite3.Connection, attempt_uuid: str, **fields: Any) -> None:
    fields = {k: v for k, v in fields.items() if v is not None}
    if not fields:
        return
    assignments = ", ".join(f"{k} = ?" for k in fields)
    conn.execute(
        f"UPDATE attempts SET {assignments} WHERE uuid = ?",
        (*fields.values(), attempt_uuid),
    )


def _grade(
    conn: sqlite3.Connection,
    attempt_uuid: str,
    event: Event,
    context: tuple[srs.Params, scoring.Weights] | None,
) -> None:
    """Fold a finished attempt into its FSRS card (spec §8).

    No new event type carries this: the rating is a pure function of facts the
    attempt already records, so deriving it here is what lets `replay` rebuild
    every card from a log written before any of this existed. `review_completed`
    stays reserved for the Phase 3 review pipeline, which is a different thing
    that happens to share a word.
    """
    params, weights = context or srs_context(conn)
    srs.grade_attempt(conn, attempt_uuid, at=event.ts, params=params, weights=weights)


def _record_strategies(
    conn: sqlite3.Connection,
    event: Event,
    attempt_uuid: str,
    slug: str,
    block: Any,
) -> None:
    """Fold a `problem_finished` payload's `strategies` block into three tables.

    Names arrive as you typed them and the key is derived here rather than at the
    finish prompt, so a change to `strategies.normalise` is one replay away from
    applying to everything you ever wrote -- the same bargain the score and the
    rating already make.

    `INSERT OR IGNORE` throughout, because this runs under `replay` as well as
    live: the first spelling of a strategy is the one that sticks, and the date a
    problem first saw it is the date of the attempt that named it, not of the
    most recent one.
    """
    if not isinstance(block, dict):
        return
    for role in strategies.ROLES:
        for entry in strategies.clean(block.get(role) or []):
            conn.execute(
                "INSERT OR IGNORE INTO strategies(key, name, first_seen) VALUES(?,?,?)",
                (entry.key, entry.name, event.ts),
            )
            conn.execute(
                "INSERT OR IGNORE INTO problem_strategies(slug, key, first_seen) VALUES(?,?,?)",
                (slug, entry.key, event.ts),
            )
            conn.execute(
                "INSERT OR IGNORE INTO attempt_strategies"
                "(attempt_uuid, attempt_id, slug, key, role) VALUES(?,?,?,?,?)",
                (attempt_uuid, _attempt_id(conn, attempt_uuid), slug, entry.key, role),
            )


def _sole_used_key(conn: sqlite3.Connection, attempt_uuid: str) -> str | None:
    """The one approach this attempt wrote, or None if it wrote none or several.

    "None if several" is the whole point rather than a shortcut. An attempt that
    named two approaches archived two files, and nothing in a payload written
    before the approach column existed says which file is which -- so the honest
    answer is to attribute neither.
    """
    rows = conn.execute(
        "SELECT key FROM attempt_strategies WHERE attempt_uuid = ? AND role = ?",
        (attempt_uuid, strategies.USED),
    ).fetchall()
    return rows[0]["key"] if len(rows) == 1 else None


def _record_solution(
    conn: sqlite3.Connection,
    event: Event,
    slug: str,
    key: str,
    name: str,
    code_path: str | None,
    language: str | None,
    attempt_uuid: str | None = None,
) -> None:
    """Put one written approach in the library.

    Newest write wins, and that is not history being rewritten: every attempt
    keeps its own file on disk under its own id, and this row is a pointer at
    the most recent of them. What it replaces is a pointer, not a solution.

    The cost claim is inherited from the attempt, and only when that attempt
    wrote exactly one approach -- otherwise one answer would be copied onto two
    solutions, and at most one of them could be the code it describes. An
    approach added from the library has no attempt and so has no claim at all,
    which reads the same way on screen: an empty cell, not a guess.
    """
    if not slug or not key or not code_path:
        return
    conn.execute(
        "INSERT OR IGNORE INTO strategies(key, name, first_seen) VALUES(?,?,?)",
        (key, name, event.ts),
    )
    conn.execute(
        "INSERT OR IGNORE INTO problem_strategies(slug, key, first_seen) VALUES(?,?,?)",
        (slug, key, event.ts),
    )
    claim: sqlite3.Row | None = None
    if attempt_uuid and _sole_used_key(conn, attempt_uuid) == key:
        claim = conn.execute(
            "SELECT time_optimality, space_optimality FROM attempts WHERE uuid = ?",
            (attempt_uuid,),
        ).fetchone()
    conn.execute(
        "INSERT INTO solutions"
        "(slug, key, code_path, language, attempt_uuid, attempt_id,"
        " time_optimality, space_optimality, written_at) VALUES(?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(slug, key) DO UPDATE SET "
        "code_path = excluded.code_path, language = excluded.language, "
        "attempt_uuid = excluded.attempt_uuid, attempt_id = excluded.attempt_id, "
        "time_optimality = excluded.time_optimality, "
        "space_optimality = excluded.space_optimality, "
        "written_at = excluded.written_at",
        (
            slug,
            key,
            code_path,
            language,
            attempt_uuid,
            _attempt_id(conn, attempt_uuid) if attempt_uuid else None,
            claim["time_optimality"] if claim else None,
            claim["space_optimality"] if claim else None,
            event.ts,
        ),
    )


def _forget_attempts(conn: sqlite3.Connection, attempt_uuids: list[str]) -> None:
    """Drop attempts and their submissions from the projections.

    Archived code and notes on disk are deliberately left alone. They are your
    writing, and the point of a discard is that the attempt should not count --
    not that the afternoon should be destroyed.
    """
    for attempt_uuid in attempt_uuids:
        conn.execute("DELETE FROM submissions WHERE attempt_uuid = ?", (attempt_uuid,))
        # The attempt's answer goes; the vocabulary and the problem's list of
        # approaches stay. A strategy you named is a thing you learned about the
        # problem, and it did not stop being true because the attempt that
        # taught it to you should not have counted. A replay reaches the same
        # place from the other direction: it skips the event entirely, so a
        # strategy that *only* this attempt ever named is simply never created.
        conn.execute("DELETE FROM attempt_strategies WHERE attempt_uuid = ?", (attempt_uuid,))
        # The library row goes with it, and this one is not a judgement call:
        # `code_archived` carries a top-level `attempt_uuid`, so `replay` skips
        # the event outright and never creates the row at all. Keeping it here
        # would make the live projection disagree with the replayed one, which
        # is the one invariant this whole design rests on. The file on disk
        # stays, like every other piece of archived writing.
        conn.execute("DELETE FROM solutions WHERE attempt_uuid = ?", (attempt_uuid,))
        conn.execute("DELETE FROM attempts WHERE uuid = ?", (attempt_uuid,))


def apply(
    conn: sqlite3.Connection,
    event: Event,
    *,
    context: tuple[srs.Params, scoring.Weights] | None = None,
) -> None:
    """Fold one event into the projection tables.

    Unknown or not-yet-implemented event types are ignored on purpose: the log
    may legitimately contain events from a later phase (or another device).

    `context` is the (params, weights) the card projection grades with; it is
    resolved from the settings overlay when not supplied, so a caller applying
    one event in isolation still gets the right answer.
    """
    p = event.payload

    if event.type == SESSION_STARTED:
        # `slugs` is stored rather than left in the payload because resuming has
        # to know the plan, and `speech_mode` because the setup screen's ctrl+a
        # override is a decision about this run that a restart must not lose.
        conn.execute(
            "INSERT OR IGNORE INTO sessions"
            "(uuid, started_at, planned_n, slugs, speech_mode) VALUES(?,?,?,?,?)",
            (
                p["session_uuid"],
                p.get("started_at", event.ts),
                int(p.get("planned_n", 0)),
                json.dumps(p.get("slugs", [])),
                int(p.get("speech_mode", 0)),
            ),
        )

    elif event.type == PROBLEM_STARTED:
        session_id = _session_id(conn, p["session_uuid"])
        if session_id is None:
            return
        conn.execute(
            "INSERT OR IGNORE INTO attempts"
            "(uuid, session_id, slug, started_at, is_review, language) VALUES(?,?,?,?,?,?)",
            (
                p["attempt_uuid"],
                session_id,
                p["slug"],
                p.get("started_at", event.ts),
                int(p.get("is_review", 0)),
                p.get("language"),
            ),
        )

    elif event.type == HINT_REVEALED:
        # Monotonic: a hint tier can only ever go up.
        conn.execute(
            "UPDATE attempts SET max_hint_tier = MAX(COALESCE(max_hint_tier, 0), ?) WHERE uuid = ?",
            (int(p["tier"]), p["attempt_uuid"]),
        )

    elif event.type == PROBLEM_SUBMITTED:
        # `submissions` counts failed submits, so an accepted one does not add.
        # This is the *submission* verdict — what the judge said about one
        # submit — not the attempt verdict in `scoring.VERDICTS`. Different
        # column, different vocabulary; the ladder does not reach here.
        if p.get("verdict") != "accepted":
            conn.execute(
                "UPDATE attempts SET submissions = COALESCE(submissions, 0) + 1 WHERE uuid = ?",
                (p["attempt_uuid"],),
            )
        # `n` is carried in the payload because it is what named the archived
        # file. Events written before submissions existed have none, so fall
        # back to position within the attempt — deterministic under replay.
        n = p.get("n")
        if n is None:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM submissions WHERE attempt_uuid = ?",
                (p["attempt_uuid"],),
            ).fetchone()
            n = int(row["n"]) + 1
        conn.execute(
            "INSERT OR IGNORE INTO submissions"
            "(attempt_uuid, attempt_id, slug, n, verdict, submitted_at) VALUES(?,?,?,?,?,?)",
            (
                p["attempt_uuid"],
                _attempt_id(conn, p["attempt_uuid"]),
                p.get("slug"),
                int(n),
                p.get("verdict"),
                p.get("submitted_at", event.ts),
            ),
        )

    elif event.type == PROBLEM_ABANDONED:
        _update_attempt(
            conn,
            p["attempt_uuid"],
            ended_at=p.get("ended_at", event.ts),
            verdict="gave_up",
            active_seconds=p.get("active_seconds"),
            wall_seconds=p.get("wall_seconds"),
            paused_seconds=p.get("paused_seconds"),
        )
        # Giving up scores zero but still schedules a review (spec §5). Zero is
        # the point: it costs you the run, not the record.
        _grade(conn, p["attempt_uuid"], event, context)

    elif event.type == PROBLEM_FINISHED:
        _update_attempt(
            conn,
            p["attempt_uuid"],
            ended_at=p.get("ended_at", event.ts),
            verdict=p.get("verdict"),
            active_seconds=p.get("active_seconds"),
            wall_seconds=p.get("wall_seconds"),
            paused_seconds=p.get("paused_seconds"),
            self_confidence=p.get("self_confidence"),
            lc_runtime_pct=p.get("lc_runtime_pct"),
            lc_memory_pct=p.get("lc_memory_pct"),
            language=p.get("language"),
            claimed_complexity=p.get("claimed_complexity"),
            claimed_space_complexity=p.get("claimed_space_complexity"),
            time_optimality=p.get("time_optimality"),
            space_optimality=p.get("space_optimality"),
            # Only events written before the question had axes carry this, and
            # they land in the column of the same name. Nothing maps it onto
            # either axis: see the `attempts.optimality` comment.
            optimality=p.get("optimality"),
        )
        # Before `_grade`, not after. The rating reads `worth_learning` to tell a
        # suboptimal solve you diagnosed yourself from one you did not, so the
        # rows have to exist by the time the card is folded. This ordering is the
        # reason the answer rides on this payload instead of a later event.
        _record_strategies(
            conn, event, p["attempt_uuid"], p.get("slug", ""), p.get("strategies")
        )
        # Every rating input is on the row by now: verdict and timing from this
        # event, the hint tier from earlier `hint_revealed` events, the strategy
        # answer from the block just above.
        _grade(conn, p["attempt_uuid"], event, context)

    elif event.type == CODE_ARCHIVED:
        # `attempts.code_path` is the attempt's headline file, and with several
        # approaches archived off one solve there are several to choose from.
        # First one saved wins, guarded on the column rather than on a counter
        # so a replay makes the same choice in the same order.
        conn.execute(
            "UPDATE attempts SET code_path = ? WHERE uuid = ? AND code_path IS NULL",
            (p.get("code_path"), p["attempt_uuid"]),
        )
        _update_attempt(conn, p["attempt_uuid"], language=p.get("language"))
        # The approach this file is for. Named on the payload since the library
        # existed; before that, inferred -- an attempt that wrote exactly one
        # approach wrote it in this file, and there is nothing to be ambiguous
        # about. That inference is what gives the library its back catalogue
        # without a migration and without touching a single logged payload.
        named = strategies.clean([p["approach"]]) if p.get("approach") else []
        entry = named[0] if named else None
        if entry is None:
            key = _sole_used_key(conn, p["attempt_uuid"])
            row = (
                conn.execute("SELECT name FROM strategies WHERE key = ?", (key,)).fetchone()
                if key
                else None
            )
            entry = strategies.Strategy(key=key, name=row["name"]) if row else None
        if entry is not None:
            _record_solution(
                conn,
                event,
                p.get("slug", ""),
                entry.key,
                entry.name,
                p.get("code_path"),
                p.get("language"),
                attempt_uuid=p["attempt_uuid"],
            )

    elif event.type == SOLUTION_ARCHIVED:
        # An approach filled in from the library, with no attempt behind it.
        # It joins the problem's list and the vocabulary exactly as one named at
        # a finish prompt does -- what it does not get is an `attempt_strategies`
        # row, because there was no attempt for it to be an answer about.
        named = strategies.clean([p.get("approach") or ""])
        if named:
            _record_solution(
                conn,
                event,
                p.get("slug", ""),
                named[0].key,
                named[0].name,
                p.get("code_path"),
                p.get("language"),
            )

    elif event.type == SUBMISSION_ARCHIVED:
        conn.execute(
            "UPDATE submissions SET code_path = ?, language = ? WHERE attempt_uuid = ? AND n = ?",
            (p.get("code_path"), p.get("language"), p["attempt_uuid"], int(p["n"])),
        )

    elif event.type == NOTE_WRITTEN:
        _update_attempt(conn, p["attempt_uuid"], note_path=p.get("note_path"))

    elif event.type == AUDIO_RECORDED:
        _update_attempt(conn, p["attempt_uuid"], audio_path=p.get("audio_path"))

    elif event.type == SESSION_ENDED:
        conn.execute(
            "UPDATE sessions SET ended_at = ?, outcome = ?, session_note = ? WHERE uuid = ?",
            (
                p.get("ended_at", event.ts),
                p.get("outcome"),
                p.get("session_note"),
                p["session_uuid"],
            ),
        )

    # Both of these are addressed to the *session*, and the attempt readings they
    # carry are nested under `attempt` rather than sitting at the top level as an
    # `attempt_uuid`. That placement is load-bearing: `replay` skips any event
    # whose top-level `attempt_uuid` was discarded, and the cursor a suspend
    # records is a fact about the run that stays true whatever later became of
    # the problem that was on screen. Nested, the tombstone still does its job --
    # the UPDATEs below simply match no row once the attempt is forgotten.

    elif event.type == SESSION_SUSPENDED:
        conn.execute(
            "UPDATE sessions SET suspended_at = ?, resume_index = ? WHERE uuid = ?",
            (p.get("suspended_at", event.ts), int(p.get("index", 0)), p["session_uuid"]),
        )
        # The clock readings land on the attempt with `ended_at` and `verdict`
        # left NULL — which is already this schema's word for "in progress", so
        # `queues._attempted_slugs` keeps the problem out of tomorrow's queue and
        # nothing grades it. They are what `RunEngine.resume_session` rebuilds
        # the monotonic clock from in the next process.
        attempt = p.get("attempt") or {}
        if attempt.get("uuid"):
            _update_attempt(
                conn,
                attempt["uuid"],
                active_seconds=attempt.get("active_seconds"),
                wall_seconds=attempt.get("wall_seconds"),
                paused_seconds=attempt.get("paused_seconds"),
            )

    elif event.type == SESSION_RESUMED:
        conn.execute(
            "UPDATE sessions SET suspended_at = NULL WHERE uuid = ?", (p["session_uuid"],)
        )
        # `away_seconds` is measured once, when you come back, and carried here
        # rather than recomputed from the two events' timestamps — the same rule
        # `is_review` follows, and what keeps a replay from quietly rewriting how
        # long you were gone.
        attempt = p.get("attempt") or {}
        if attempt.get("uuid"):
            conn.execute(
                "UPDATE attempts SET "
                "  suspended_seconds = COALESCE(suspended_seconds, 0) + ?, "
                "  suspends = COALESCE(suspends, 0) + 1 "
                "WHERE uuid = ?",
                (int(attempt.get("away_seconds", 0)), attempt["uuid"]),
            )

    elif event.type == QUEUE_GENERATED:
        # The payload carries the finished list, so this replays rather than
        # regenerates — which is what keeps a Phase 3 LLM-chosen queue
        # reproducible even though the model that chose it is long gone.
        conn.execute(
            "INSERT INTO queues(date, slugs, rationale, generated_by, created_at) "
            "VALUES(?,?,?,?,?) "
            "ON CONFLICT(date) DO UPDATE SET "
            "  slugs = excluded.slugs, rationale = excluded.rationale, "
            "  generated_by = excluded.generated_by, created_at = excluded.created_at",
            (
                p["date"],
                json.dumps(p["slugs"]),
                p.get("rationale", ""),
                p.get("generated_by", "unknown"),
                p.get("created_at", event.ts),
            ),
        )

    elif event.type == ATTEMPT_DISCARDED:
        _forget_attempts(conn, [p["attempt_uuid"]])

    elif event.type == RUN_DELETED:
        session_id = _session_id(conn, p["session_uuid"])
        if session_id is not None:
            rows = conn.execute(
                "SELECT uuid FROM attempts WHERE session_id = ?", (session_id,)
            ).fetchall()
            _forget_attempts(conn, [r["uuid"] for r in rows])
            conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
        # `fsrs_cards` is not repaired here: this event cannot know which
        # reviews the deleted attempts caused. The caller runs `replay`, which
        # skips them from the start and rebuilds the cards without them.

    elif event.type == SETTINGS_CHANGED:
        # A null value clears the override rather than storing one, which is
        # how the settings screen hands a knob back to config.toml. No setting
        # is allowed to mean null, so the encoding costs nothing.
        if p.get("value") is None:
            conn.execute("DELETE FROM settings WHERE key = ?", (p["key"],))
        else:
            conn.execute(
                "INSERT INTO settings(key, value, updated_at) VALUES(?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
                (p["key"], json.dumps(p["value"]), event.ts),
            )


def tombstoned(events: list[Event]) -> tuple[set[str], set[str]]:
    """The session and attempt uuids a replay must pretend it never saw.

    Two passes, because a tombstone is written *after* the thing it kills: the
    first collects the deleted sessions and discarded attempts, the second walks
    `problem_started` to find the attempts belonging to a deleted session. An
    attempt is addressed by uuid in every later event, so those two sets are
    enough to skip the whole history of a run.
    """
    sessions = {
        e.payload["session_uuid"]
        for e in events
        if e.type == RUN_DELETED and e.payload.get("session_uuid")
    }
    attempts = {
        e.payload["attempt_uuid"]
        for e in events
        if e.type == ATTEMPT_DISCARDED and e.payload.get("attempt_uuid")
    }
    attempts |= {
        e.payload["attempt_uuid"]
        for e in events
        if e.type == PROBLEM_STARTED and e.payload.get("session_uuid") in sessions
    }
    return sessions, attempts


def replay(conn: sqlite3.Connection) -> int:
    """Drop every projection and rebuild it from the log. Returns event count.

    Tombstoned events are skipped rather than applied-then-deleted. That
    distinction is the whole reason `fsrs_cards` survives a run deletion: a
    `run_deleted` sits at the end of the log, so applying its run's attempts
    first would grade their cards, and deleting the rows afterwards would leave
    the schedule shaped by a run that no longer exists.
    """
    events = read_all(conn)
    dead_sessions, dead_attempts = tombstoned(events)
    # Resolved before the truncate, while the settings projection still holds
    # the current overrides — see `srs_context`.
    context = srs_context(conn)
    conn.execute("BEGIN")
    try:
        truncate_projections(conn)
        for event in events:
            p = event.payload
            if p.get("session_uuid") in dead_sessions or p.get("attempt_uuid") in dead_attempts:
                continue
            apply(conn, event, context=context)
    except Exception:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")
    return len(events)
