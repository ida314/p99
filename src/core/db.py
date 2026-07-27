"""SQLite schema and connection handling (spec §4).

Two layers:

  * `events` — append-only, the source of truth. Nothing else is authoritative.
  * projections — `sessions`, `attempts`, ... rebuildable at any time by
    replaying the log (see `events.replay`).

`problems` is the exception: it is seeded from a checked-in JSON catalog, not
from the log, and survives a replay.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from . import paths

SCHEMA_VERSION = 1

EVENT_LOG_DDL = """
CREATE TABLE IF NOT EXISTS events (
  id           INTEGER PRIMARY KEY,
  uuid         TEXT NOT NULL UNIQUE,      -- for future cross-device sync
  ts           TEXT NOT NULL,             -- ISO8601 UTC
  type         TEXT NOT NULL,
  payload      TEXT NOT NULL,             -- JSON
  schema_ver   INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS events_type_idx ON events(type);
CREATE INDEX IF NOT EXISTS events_ts_idx   ON events(ts);
"""

CATALOG_DDL = """
CREATE TABLE IF NOT EXISTS problems (
  slug         TEXT PRIMARY KEY,          -- 'two-sum'
  title        TEXT NOT NULL,
  url          TEXT NOT NULL,
  difficulty   TEXT NOT NULL,             -- easy|medium|hard
  tags         TEXT NOT NULL,             -- JSON array: ['array','hash-table']
  pattern      TEXT,                      -- neetcode group: 'sliding-window'
  lists        TEXT NOT NULL              -- JSON: ['neetcode150','blind75']
);
CREATE INDEX IF NOT EXISTS problems_pattern_idx ON problems(pattern);
"""

# Projections. Dropped and rebuilt wholesale by `events.replay`.
# `uuid` columns are the join key between the log and the projections; integer
# ids stay stable across replays because events are applied in log order.
PROJECTION_DDL = """
CREATE TABLE IF NOT EXISTS sessions (
  id           INTEGER PRIMARY KEY,
  uuid         TEXT NOT NULL UNIQUE,
  started_at   TEXT NOT NULL,
  ended_at     TEXT,
  planned_n    INTEGER NOT NULL,
  outcome      TEXT,                      -- completed|partial|abandoned
  session_note TEXT                       -- optional end-of-run reflection
);

CREATE TABLE IF NOT EXISTS attempts (
  id                 INTEGER PRIMARY KEY,
  uuid               TEXT NOT NULL UNIQUE,
  session_id         INTEGER NOT NULL REFERENCES sessions(id),
  slug               TEXT NOT NULL REFERENCES problems(slug),
  started_at         TEXT NOT NULL,
  ended_at           TEXT,
  active_seconds     INTEGER,             -- excludes paused time
  wall_seconds       INTEGER,
  paused_seconds     INTEGER DEFAULT 0,
  verdict            TEXT,                -- accepted|wrong_answer|tle|gave_up
  max_hint_tier      INTEGER DEFAULT 0,   -- 0..4
  submissions        INTEGER DEFAULT 0,   -- failed submits before accept
  self_confidence    INTEGER,             -- 1..4, asked at end
  lc_runtime_pct     REAL,                -- optional, hand-entered
  lc_memory_pct      REAL,
  code_path          TEXT,                -- archived source
  language           TEXT,
  note_path          TEXT,                -- reflection note, nullable
  claimed_complexity TEXT,                -- LLM guess, user-confirmable
  confirmed_complexity TEXT,
  is_review          INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS attempts_session_idx ON attempts(session_id);
CREATE INDEX IF NOT EXISTS attempts_slug_idx    ON attempts(slug);

CREATE TABLE IF NOT EXISTS settings (
  key          TEXT PRIMARY KEY,
  value        TEXT NOT NULL,
  updated_at   TEXT NOT NULL
);
"""

# Tables Phase 2/3 fill in. Created now so the schema is stable and a replay
# has somewhere to put things later; nothing in Phase 1 writes to them.
FUTURE_DDL = """
CREATE TABLE IF NOT EXISTS fsrs_cards (
  slug         TEXT PRIMARY KEY REFERENCES problems(slug),
  stability    REAL, difficulty REAL,
  due          TEXT, last_review TEXT,
  reps         INTEGER, lapses INTEGER,
  state        TEXT                       -- new|learning|review|relearning
);

CREATE TABLE IF NOT EXISTS tag_mastery (
  tag          TEXT PRIMARY KEY,
  attempts     INTEGER, solved_clean INTEGER,
  ema_score    REAL, last_seen TEXT
);

CREATE TABLE IF NOT EXISTS coach_memory (
  id           INTEGER PRIMARY KEY,
  updated_at   TEXT NOT NULL,
  profile      TEXT NOT NULL,
  token_est    INTEGER,
  source_range TEXT
);

CREATE TABLE IF NOT EXISTS jobs (
  id           INTEGER PRIMARY KEY,
  kind         TEXT NOT NULL,
  payload      TEXT NOT NULL,
  status       TEXT NOT NULL,
  attempts     INTEGER DEFAULT 0,
  created_at   TEXT, completed_at TEXT,
  result       TEXT, error TEXT
);

CREATE TABLE IF NOT EXISTS queues (
  date         TEXT PRIMARY KEY,          -- YYYY-MM-DD, local
  slugs        TEXT NOT NULL,             -- JSON array, ordered
  rationale    TEXT NOT NULL,
  generated_by TEXT NOT NULL,
  created_at   TEXT NOT NULL
);
"""

META_DDL = """
CREATE TABLE IF NOT EXISTS schema_meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
"""

# Projection tables that `replay` truncates. `problems` is deliberately absent.
PROJECTION_TABLES = ("attempts", "sessions", "settings", "fsrs_cards", "tag_mastery")


def connect(path: Path | None = None) -> sqlite3.Connection:
    """Open the database, creating it and its parent directory if needed."""
    target = path or paths.db_file()
    target.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(target, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init(conn: sqlite3.Connection) -> None:
    """Create every table. Idempotent."""
    for ddl in (META_DDL, EVENT_LOG_DDL, CATALOG_DDL, PROJECTION_DDL, FUTURE_DDL):
        conn.executescript(ddl)
    conn.execute(
        "INSERT INTO schema_meta(key, value) VALUES('schema_version', ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (str(SCHEMA_VERSION),),
    )


def open_db(path: Path | None = None) -> sqlite3.Connection:
    """`connect` + `init` — the normal entry point."""
    conn = connect(path)
    init(conn)
    return conn


def truncate_projections(conn: sqlite3.Connection) -> None:
    """Wipe everything rebuildable from the event log. Leaves `problems` alone.

    Order matters: children before parents, so this stays valid with foreign
    keys on (and inside a transaction, where PRAGMA foreign_keys is a no-op).
    """
    for table in PROJECTION_TABLES:
        conn.execute(f"DELETE FROM {table}")
