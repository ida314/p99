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

# 2: `fsrs_cards` gained `step` and lost nothing; `tag_mastery` was dropped.
# 3: `attempts` gained `optimality` and `audio_path`.
# Bumping this is cheap precisely because everything it touches is a projection
# -- see `migrate`.
SCHEMA_VERSION = 3

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
  -- how much help you needed, not what the judge said. See scoring.VERDICTS:
  --   solved_unaided|solved_with_hints|solved_after_description
  --   |solved_after_pseudocode|solved_after_implementation|gave_up|ungraded
  -- legacy, still in the log: accepted|wrong_answer|tle|used_editorial
  verdict            TEXT,
  max_hint_tier      INTEGER DEFAULT 0,   -- 0..4
  submissions        INTEGER DEFAULT 0,   -- failed submits before accept
  self_confidence    INTEGER,             -- 1..4, asked at end
  lc_runtime_pct     REAL,                -- optional, hand-entered
  lc_memory_pct      REAL,
  code_path          TEXT,                -- archived source
  language           TEXT,
  note_path          TEXT,                -- reflection note, nullable
  audio_path         TEXT,                -- speech-mode recording, nullable
  -- what you said your solution costs, typed at the finish prompt. Free text:
  -- O(n+m), O(nk) and "amortized O(1)" are the answers worth having, and a
  -- format that rejects them would only be recording the easy cases.
  claimed_complexity TEXT,
  -- reserved for whatever eventually checks the claim against the code.
  confirmed_complexity TEXT,
  optimality         TEXT,                -- optimal|suboptimal|unsure
  is_review          INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS attempts_session_idx ON attempts(session_id);
CREATE INDEX IF NOT EXISTS attempts_slug_idx    ON attempts(slug);

-- One row per submit to LeetCode. `attempts.submissions` is the count; this is
-- the detail, and it is where a failed submit's archived code hangs. Kept off
-- `attempts` because there are many per attempt and because `attempts.code_path`
-- belongs to the solution you settled on, not to a wrong answer along the way.
CREATE TABLE IF NOT EXISTS submissions (
  id           INTEGER PRIMARY KEY,
  attempt_uuid TEXT NOT NULL,
  attempt_id   INTEGER REFERENCES attempts(id),
  slug         TEXT,
  n            INTEGER NOT NULL,    -- 1-based, within the attempt
  verdict      TEXT,                -- as reported at submit time
  submitted_at TEXT NOT NULL,
  code_path    TEXT,                -- archived wrong answer; null if skipped
  language     TEXT,
  UNIQUE(attempt_uuid, n)
);
CREATE INDEX IF NOT EXISTS submissions_attempt_idx ON submissions(attempt_id);

CREATE TABLE IF NOT EXISTS settings (
  key          TEXT PRIMARY KEY,
  value        TEXT NOT NULL,
  updated_at   TEXT NOT NULL
);
"""

# Phase 2 fills `fsrs_cards` and `queues`; the rest waits for Phase 3. All of
# them are created now so the schema is stable and a replay has somewhere to put
# things later.
#
# `tag_mastery` used to live here. It is gone on purpose: its `ema_score` is a
# function of the score, and this project does not store scores (see the note at
# the top of `scoring`). It is computed at read time by `stats.tag_mastery`
# instead -- the third deviation from the spec, documented in the README.
FUTURE_DDL = """
CREATE TABLE IF NOT EXISTS fsrs_cards (
  slug         TEXT PRIMARY KEY REFERENCES problems(slug),
  stability    REAL, difficulty REAL,
  due          TEXT, last_review TEXT,
  reps         INTEGER, lapses INTEGER,
  state        TEXT,                      -- learning|review|relearning
  step         INTEGER                    -- learning/relearning step; null in review
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
#
# `queues` belongs here even though a Phase 3 queue is chosen by an LLM and
# could never be recomputed: the `queue_generated` payload carries the finished
# slug list, so replaying the log reproduces the queue exactly rather than
# regenerating it.
PROJECTION_TABLES = (
    "submissions",  # references attempts: children first, see the docstring
    "attempts",
    "sessions",
    "settings",
    "fsrs_cards",
    "queues",
)

# Projections whose *shape* changed in a given schema version. Bumping
# `SCHEMA_VERSION` and listing the affected tables here is the entire migration
# story, and it is this short only because projections are disposable: drop
# them, recreate them from the DDL, and the next replay refills them from the
# log. Nothing a user typed is ever in one of these tables.
# Order within a version matters: `migrate` drops with foreign keys on, so a
# child table has to go before the parent it references.
SHAPE_CHANGED_IN = {
    2: ("fsrs_cards", "tag_mastery"),
    3: ("submissions", "attempts"),
}


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


def stored_version(conn: sqlite3.Connection) -> int:
    """The schema version on disk. 0 for a database that predates the stamp."""
    try:
        row = conn.execute(
            "SELECT value FROM schema_meta WHERE key = 'schema_version'"
        ).fetchone()
    except sqlite3.OperationalError:  # schema_meta itself not created yet
        return 0
    return int(row["value"]) if row else 0


def migrate(conn: sqlite3.Connection) -> bool:
    """Drop projections whose shape changed since the stored version.

    Returns True if anything was dropped, meaning the caller owes a replay.

    This is the whole migration mechanism, and it can be this small because the
    only tables that ever change shape are projections: there is nothing in one
    of them that is not also in the event log. `ALTER TABLE` gymnastics would be
    strictly more code and strictly more risk.

    `db` cannot call `events.replay` -- `events` imports `db`, not the other way
    round -- so the signal goes back to the caller instead.
    """
    from_version = stored_version(conn)
    if from_version >= SCHEMA_VERSION:
        return False

    dropped = False
    for version, tables in sorted(SHAPE_CHANGED_IN.items()):
        if from_version < version <= SCHEMA_VERSION:
            for table in tables:
                conn.execute(f"DROP TABLE IF EXISTS {table}")
                dropped = True
    return dropped


def init(conn: sqlite3.Connection) -> bool:
    """Create every table, migrating first. Idempotent.

    Returns True if a projection was dropped and the caller owes a replay.
    """
    conn.executescript(META_DDL)
    needs_replay = migrate(conn)
    for ddl in (EVENT_LOG_DDL, CATALOG_DDL, PROJECTION_DDL, FUTURE_DDL):
        conn.executescript(ddl)
    conn.execute(
        "INSERT INTO schema_meta(key, value) VALUES('schema_version', ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (str(SCHEMA_VERSION),),
    )
    return needs_replay


def open_db(path: Path | None = None) -> sqlite3.Connection:
    """`connect` + `init` — the normal entry point.

    Performs the replay a schema bump asks for, so no caller has to remember to.
    """
    conn = connect(path)
    if init(conn):
        from . import events  # deferred: `events` imports this module

        events.replay(conn)
    return conn


def truncate_projections(conn: sqlite3.Connection) -> None:
    """Wipe everything rebuildable from the event log. Leaves `problems` alone.

    Order matters: children before parents, so this stays valid with foreign
    keys on (and inside a transaction, where PRAGMA foreign_keys is a no-op).
    """
    for table in PROJECTION_TABLES:
        conn.execute(f"DELETE FROM {table}")
