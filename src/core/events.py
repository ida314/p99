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
SESSION_ENDED = "session_ended"
REVIEW_COMPLETED = "review_completed"   # Phase 3
MEMORY_UPDATED = "memory_updated"       # Phase 3
QUEUE_GENERATED = "queue_generated"     # Phase 2
SETTINGS_CHANGED = "settings_changed"

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
        SESSION_ENDED,
        REVIEW_COMPLETED,
        MEMORY_UPDATED,
        QUEUE_GENERATED,
        SETTINGS_CHANGED,
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


def _update_attempt(conn: sqlite3.Connection, attempt_uuid: str, **fields: Any) -> None:
    fields = {k: v for k, v in fields.items() if v is not None}
    if not fields:
        return
    assignments = ", ".join(f"{k} = ?" for k in fields)
    conn.execute(
        f"UPDATE attempts SET {assignments} WHERE uuid = ?",
        (*fields.values(), attempt_uuid),
    )


def apply(conn: sqlite3.Connection, event: Event) -> None:
    """Fold one event into the projection tables.

    Unknown or not-yet-implemented event types are ignored on purpose: the log
    may legitimately contain events from a later phase (or another device).
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
        )

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


def replay(conn: sqlite3.Connection) -> int:
    """Drop every projection and rebuild it from the log. Returns event count."""
    events = read_all(conn)
    conn.execute("BEGIN")
    try:
        truncate_projections(conn)
        for event in events:
            apply(conn, event)
    except Exception:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")
    return len(events)
