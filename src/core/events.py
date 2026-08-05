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

from . import scoring, srs
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
NOTE_WRITTEN = "note_written"
AUDIO_RECORDED = "audio_recorded"
SESSION_ENDED = "session_ended"
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
        NOTE_WRITTEN,
        AUDIO_RECORDED,
        SESSION_ENDED,
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

    Deliberately not via `config`: `config.set_option` appends events, so this
    module cannot import it. Only the two keys the card projection depends on
    are read this way, and both are plain strings.
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
    """
    return (
        srs.load_params(_setting(conn, "srs.params", srs.DEFAULT_PARAMS)),
        scoring.load_weights(_setting(conn, "scoring.weights", scoring.DEFAULT_WEIGHTS)),
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


def _forget_attempts(conn: sqlite3.Connection, attempt_uuids: list[str]) -> None:
    """Drop attempts and their submissions from the projections.

    Archived code and notes on disk are deliberately left alone. They are your
    writing, and the point of a discard is that the attempt should not count --
    not that the afternoon should be destroyed.
    """
    for attempt_uuid in attempt_uuids:
        conn.execute("DELETE FROM submissions WHERE attempt_uuid = ?", (attempt_uuid,))
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
        conn.execute(
            "INSERT OR IGNORE INTO sessions(uuid, started_at, planned_n) VALUES(?,?,?)",
            (p["session_uuid"], p.get("started_at", event.ts), int(p.get("planned_n", 0))),
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
            optimality=p.get("optimality"),
        )
        # Every rating input is on the row by now: verdict and timing from this
        # event, the hint tier from earlier `hint_revealed` events.
        _grade(conn, p["attempt_uuid"], event, context)

    elif event.type == CODE_ARCHIVED:
        _update_attempt(
            conn,
            p["attempt_uuid"],
            code_path=p.get("code_path"),
            language=p.get("language"),
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
