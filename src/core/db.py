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
# 4: suspend/resume — `sessions` gained the four columns a run needs to be picked
#    up in a later process, and `attempts` gained the away-time counters.
# 5: the cost claim split along its two axes — `attempts` gained a space
#    complexity and a per-axis optimality answer, and `optimality` stopped being
#    written to.
# 6: mastery — `fsrs_cards` gained `rungs_left` and `mastered_at`, the counter
#    that decides when a problem leaves the rotation and the date it did.
# 7: solving strategies — three new projections holding the vocabulary of
#    approaches you name yourself, and its per-problem and per-attempt links.
#    The bump also forces the replay that regrades every card under the rating
#    map's new optimality branch (see `srs.rate`).
# 8: the approach library — `solutions`, one row per problem-and-approach that
#    has code behind it.
# 9: the solutions page — `problem_solutions` absorbs both `problem_strategies`
#    and v8's `solutions`, because they were the same list seen twice: the ways
#    one problem can be solved. It gains the thing neither had, an optimality
#    per way, which is what makes "there is an O(n log n) route and I wrote the
#    O(n²)" a fact the problem holds rather than a role an answer plays.
# 10: solving it again -- `resolves`, one row per pass after the first at the
#    same problem in the same sitting. The attempt row stays the first pass, so
#    nothing it already scored moves.
# 11: methods -- the ways one problem can be solved become their own list,
#    `problem_methods`, keyed by the problem and named in the problem's own
#    terms, with `attempt_methods` saying which of them a solve wrote. They stop
#    being rows in the shared strategy vocabulary, which is what
#    `problem_solutions` made them: a strategy is a pattern that spans problems,
#    a method is one route through one problem, and one table could not be both.
#    `problem_solutions` is dropped and not recreated -- the log keeps every
#    event that filled it, and the methods list starts empty.
# Bumping this is cheap precisely because everything it touches is a projection
# -- see `migrate`.
SCHEMA_VERSION = 11

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
  session_note TEXT,                      -- optional end-of-run reflection
  -- Everything below is what a *suspended* run needs to be picked up by a later
  -- process. A run only ever lived in memory before this; `slugs` and
  -- `speech_mode` come straight off the `session_started` payload, so a replay
  -- refills them for runs that predate the feature too.
  slugs        TEXT,                      -- JSON array, ordered; the run's plan
  speech_mode  INTEGER NOT NULL DEFAULT 0,
  suspended_at TEXT,                      -- set while waiting to be resumed
  resume_index INTEGER                    -- cursor into `slugs`
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
  claimed_complexity       TEXT,          -- time
  claimed_space_complexity TEXT,
  -- reserved for whatever eventually checks the claim against the code.
  confirmed_complexity TEXT,
  -- optimal|suboptimal|unsure, once per axis. They are separate columns and not
  -- one answer because they are separate facts: a hash map that buys O(n) time
  -- with O(n) space is optimal on one axis and beaten on the other, and being
  -- sure about time while having never thought about space is the normal state.
  time_optimality    TEXT,
  space_optimality   TEXT,
  -- legacy, still in the log: the answer to "was it the optimal algorithm?",
  -- asked before the question had axes. Never written to again, and never
  -- reinterpreted as either of the two above -- an unqualified "optimal" is not
  -- a claim about time, it is a claim someone made about a question that did
  -- not distinguish. It renders as it always did.
  optimality         TEXT,
  is_review          INTEGER NOT NULL DEFAULT 0,
  -- Time the app was closed on this attempt, and how many times you walked away
  -- and came back. Deliberately not folded into `paused_seconds`: a pause is
  -- four minutes at the kettle with the run on screen, and calling an overnight
  -- gap the same thing would make both numbers useless.
  suspended_seconds  INTEGER DEFAULT 0,
  suspends           INTEGER DEFAULT 0
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

-- One row per pass after the first, when you solve the same problem again in the
-- same sitting. `n` counts passes over the whole attempt, so the attempt row is
-- pass 1 and the first re-solve is 2 -- the same number that names its file.
--
-- A child table for the reason `submissions` is one: there are many per attempt.
-- But the sharper reason is what stays on `attempts`. The attempt row is the
-- first pass, and it is the row that was scored and the row that graded the
-- card. Solving it a second time tonight is worth recording and is not worth a
-- second review, so nothing here is ever read by `scoring` or by `srs` -- see
-- the `problem_resolved` branch of `events.apply`.
CREATE TABLE IF NOT EXISTS resolves (
  id                 INTEGER PRIMARY KEY,
  attempt_uuid       TEXT NOT NULL,
  attempt_id         INTEGER REFERENCES attempts(id),
  slug               TEXT,
  n                  INTEGER NOT NULL,    -- 1-based over the attempt; re-solves start at 2
  verdict            TEXT,
  ended_at           TEXT NOT NULL,
  active_seconds     INTEGER,
  wall_seconds       INTEGER,
  paused_seconds     INTEGER,
  self_confidence    INTEGER,
  lc_runtime_pct     REAL,
  lc_memory_pct      REAL,
  claimed_complexity TEXT,
  claimed_space_complexity TEXT,
  time_optimality    TEXT,
  space_optimality   TEXT,
  code_path          TEXT,
  language           TEXT,
  note_path          TEXT,
  UNIQUE(attempt_uuid, n)
);
CREATE INDEX IF NOT EXISTS resolves_attempt_idx ON resolves(attempt_id);

CREATE TABLE IF NOT EXISTS settings (
  key          TEXT PRIMARY KEY,
  value        TEXT NOT NULL,
  updated_at   TEXT NOT NULL
);

-- Solving strategies: the approaches you name yourself, shared across problems.
-- See `strategies` for why this is a vocabulary and not a per-problem note.
--
-- `key` is `strategies.normalise(name)`, so "Top-Down DP" and "top down dp" are
-- one row. `name` is the first spelling you typed; nothing ever rewrites it.
CREATE TABLE IF NOT EXISTS strategies (
  key          TEXT PRIMARY KEY,          -- 'bottom-up-tabulation'
  name         TEXT NOT NULL,             -- 'bottom-up tabulation'
  first_seen   TEXT NOT NULL
);

-- The ways one problem can be solved: every method you have recorded for it,
-- optimal or not, written or not.
--
-- This is the problem's list, not any attempt's. A problem admits the methods it
-- admits whether or not you have ever taken them, which is why `optimality`
-- lives here and not on `attempts`: "there is an O(n log n) route" is true
-- before you sit down and true after, and an attempt row can only ever say what
-- *you* wrote on one particular evening.
--
-- Keyed by `(slug, key)` and holding its own `name`, because a method has no
-- meaning away from its problem -- "sort, then two pointers from both ends" is
-- not a technique, it is this problem's route. That is the whole difference from
-- `strategies`, which is one vocabulary shared by every problem, and the reason
-- these two tables never reference each other. See `methods`.
--
-- Accumulates. Rows come from the methods prompt after a solve and from the
-- methods screen months later. Nothing removes one, because a route that worked
-- once did not stop existing.
--
-- `optimality` is null until you say, and null is not `unsure`: an unanswered
-- question is not an answer. Same instinct that left `attempts.optimality` alone
-- rather than reinterpreting it when the cost claim grew a second axis.
--
-- The code columns hold the *latest* file written for this method. Every attempt
-- keeps its own file on disk under its own id; this is a pointer at the newest
-- of them, and replacing a pointer is not rewriting a solution.
CREATE TABLE IF NOT EXISTS problem_methods (
  slug         TEXT NOT NULL REFERENCES problems(slug),
  key          TEXT NOT NULL,            -- methods.normalise(name), per problem
  name         TEXT NOT NULL,            -- 'sort, then two pointers'
  optimality   TEXT,                     -- optimal|suboptimal|unsure|null
  code_path    TEXT,
  language     TEXT,
  attempt_uuid TEXT,                     -- null for one added off an attempt
  attempt_id   INTEGER REFERENCES attempts(id),
  first_seen   TEXT NOT NULL,
  updated_at   TEXT NOT NULL,
  PRIMARY KEY (slug, key)
);
CREATE INDEX IF NOT EXISTS problem_methods_attempt_idx
  ON problem_methods(attempt_uuid);

-- Which method one attempt wrote. Usually one row; two on a night you solved it
-- twice. This is the half of the methods list that is about tonight, and it is
-- what `saw_better` compares the problem's optimal methods against -- an optimal
-- method recorded here that is not in this table for this attempt is you having
-- known there was better.
CREATE TABLE IF NOT EXISTS attempt_methods (
  attempt_uuid TEXT NOT NULL,
  attempt_id   INTEGER REFERENCES attempts(id),
  slug         TEXT NOT NULL,
  key          TEXT NOT NULL,
  PRIMARY KEY (attempt_uuid, key)
);
CREATE INDEX IF NOT EXISTS attempt_methods_slug_idx ON attempt_methods(slug);

-- Which patterns one attempt reached for. `role` is `used` (what you wrote with)
-- or `worth_learning` (the better approach you could see and did not write, a
-- role nothing writes any more) -- the second is what `srs.rate` still reads to
-- tell a suboptimal solve you diagnosed yourself from one you did not.
--
-- Says nothing about the ways the problem can be solved: that is
-- `problem_methods`, and the two are never joined.
CREATE TABLE IF NOT EXISTS attempt_strategies (
  attempt_uuid TEXT NOT NULL,
  attempt_id   INTEGER REFERENCES attempts(id),
  slug         TEXT NOT NULL,
  key          TEXT NOT NULL REFERENCES strategies(key),
  role         TEXT NOT NULL,             -- used|worth_learning
  PRIMARY KEY (attempt_uuid, key)
);
CREATE INDEX IF NOT EXISTS attempt_strategies_key_idx  ON attempt_strategies(key);
CREATE INDEX IF NOT EXISTS attempt_strategies_slug_idx ON attempt_strategies(slug);
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
  step         INTEGER,                   -- learning/relearning step; null in review
  -- Mastery, from the `[mastery]` table in `data/srs/*.toml`. `rungs_left` is
  -- how many more non-failing reviews this card owes before it leaves the
  -- rotation; `mastered_at` is when it did. Both null under v1/v2, which have
  -- no mastery table and master nothing.
  rungs_left   INTEGER,
  mastered_at  TEXT
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

# Where a live run keeps its clock between events, so a process that dies without
# unwinding can still be picked up. Not a projection and not history: the log
# records what *happened*, and a run in progress has not happened yet. Three
# things follow from that, and all three are load-bearing.
#
#   * It is absent from `PROJECTION_TABLES`. A replay must not wipe it -- nothing
#     in the log can put it back, which is the entire point of it existing.
#   * It carries no foreign keys. `sessions` and `attempts` are truncated by a
#     replay while this row is not, so the uuids are plain TEXT.
#   * It gets no `SCHEMA_VERSION` bump. Nothing already on disk changes shape and
#     there is nothing to backfill from the log, so no replay is owed; `init`'s
#     `CREATE TABLE IF NOT EXISTS` is the whole migration.
#
# At most one row, always id 1: only one run can be live at a time.
CHECKPOINT_DDL = """
CREATE TABLE IF NOT EXISTS run_checkpoint (
  id               INTEGER PRIMARY KEY CHECK (id = 1),
  session_uuid     TEXT NOT NULL,
  attempt_uuid     TEXT,                    -- null between problems
  resume_index     INTEGER NOT NULL,        -- cursor into the session's `slugs`
  solves           INTEGER NOT NULL DEFAULT 1,
  attempt_finished INTEGER NOT NULL DEFAULT 0,
  -- The readings `attempts` only receives at finish/abandon/suspend. Without
  -- them a recovered run comes back at 00:00 having lost the whole solve.
  active_seconds   INTEGER,
  wall_seconds     INTEGER,
  paused_seconds   INTEGER,
  updated_at       TEXT NOT NULL            -- how long ago the crash was
);
"""

# Projection tables that `replay` truncates. `problems` is deliberately absent.
#
# `queues` belongs here even though a Phase 3 queue is chosen by an LLM and
# could never be recomputed: the `queue_generated` payload carries the finished
# slug list, so replaying the log reproduces the queue exactly rather than
# regenerating it.
PROJECTION_TABLES = (
    "problem_methods",     # references attempts: children first
    "attempt_methods",     # references attempts: children first
    "attempt_strategies",  # references attempts and strategies: children first
    "strategies",
    "submissions",  # references attempts: children first, see the docstring
    "resolves",     # references attempts: children first, same as above
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
# log. Nothing here is authoritative: `strategies.name` is the one place a
# projection holds a string you typed, and even that is a fold over the
# `problem_finished` payloads that recorded it, so dropping the table loses
# nothing the log cannot say again.
# Order within a version matters: `migrate` drops with foreign keys on, so a
# child table has to go before the parent it references.
SHAPE_CHANGED_IN = {
    2: ("fsrs_cards", "tag_mastery"),
    3: ("submissions", "attempts"),
    4: ("submissions", "attempts", "sessions"),
    5: ("submissions", "attempts"),
    6: ("fsrs_cards",),
    # New tables rather than changed ones, so there is nothing to drop -- but
    # listing them keeps the version honest, and `DROP TABLE IF EXISTS` makes it
    # free. The bump itself is what forces the replay `srs.rate` needs.
    7: ("attempt_strategies", "problem_strategies", "strategies"),
    # Another new table, and again the bump is doing the real work: the replay
    # it forces is what backfills `solutions` from every `code_archived` event
    # already in the log.
    8: ("solutions",),
    # Two tables retired into one. `solutions` and `problem_strategies` are
    # dropped and never recreated -- the DDL above no longer has them -- and the
    # replay this bump forces refills `problem_solutions` from the same log.
    9: ("solutions", "problem_strategies", "problem_solutions"),
    # A new table again, and again the bump is what does the work: the replay it
    # forces is what folds every `problem_resolved` already in the log.
    10: ("resolves",),
    # `problem_solutions` is dropped and never recreated -- the DDL above no
    # longer has it. What replaces it is not a rename: a method is keyed by its
    # problem and carries its own name, so there is nothing in the old rows to
    # migrate that the log does not already hold. The methods list starts empty
    # and the replay this bump forces regrades every card without it.
    11: ("problem_solutions", "problem_methods", "attempt_methods"),
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
    for ddl in (EVENT_LOG_DDL, CATALOG_DDL, PROJECTION_DDL, FUTURE_DDL, CHECKPOINT_DDL):
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
