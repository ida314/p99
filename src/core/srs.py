"""Spaced repetition (spec §8): FSRS cards, derived from the event log.

`fsrs_cards` is a projection. `p99 replay` truncates it and rebuilds it by
folding the log, exactly like `attempts` -- so **card state has to be a pure,
deterministic function of the events in order**. FSRS satisfies that; it is a
fold over (rating, review_datetime) pairs. Three things in `py-fsrs` do not,
and all three are defaults, so all three are overridden here:

1. `Scheduler(enable_fuzzing=True)` randomizes review-state intervals through
   `random()`. Left on, two replays of the same log produce different due dates
   and the guarantee this whole design rests on becomes a lie. Measured: eight
   replays of one six-review fold gave eight different answers.
2. `Card(card_id=None)` seeds the id from the wall clock -- and sleeps 1ms per
   card to dodge collisions, which a replay pays once per attempt.
3. `Card(due=None)` seeds `due` from the wall clock, i.e. from replay time
   rather than from when the attempt actually happened.

`new_card` passes all three explicitly. Nothing in this module may read the
clock: every timestamp arrives from the event being applied.

Parameters live in `data/srs/*.toml`, versioned the way scoring weights are, so
editing them and replaying reschedules all of history. `docs/spaced-repetition.md`
explains what each one is set to and why, and what v2 changed about v1.
"""

from __future__ import annotations

import functools
import sqlite3
import tomllib
import zlib
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from importlib import resources
from typing import Any, Mapping

from fsrs import Card, Rating, Scheduler, State

from . import scoring
# A module object rather than a dotted name, for the reason given in `catalog`:
# a package rename cannot leave a stale string behind to fail at runtime. Note
# this is `core.data.srs`, not the third-party `fsrs` imported above.
from .data import srs as _params_pkg

DEFAULT_PARAMS = "v3"

#: `fsrs.State` has no "new" -- a problem you have never finished simply has no
#: row in `fsrs_cards`. Absence is the new state.
STATE_NAMES = {State.Learning: "learning", State.Review: "review", State.Relearning: "relearning"}
STATE_VALUES = {v: k for k, v in STATE_NAMES.items()}

#: States that mean "you have seen this and it is scheduled to come back".
#: Starting one of these is what makes an attempt a review (`attempts.is_review`).
REVIEW_STATES = ("review", "relearning")


@dataclass(frozen=True)
class Params:
    name: str
    version: int
    weights: tuple[float, ...]
    #: The retention aimed for when a difficulty has nothing more specific to say.
    desired_retention: float
    #: Per-difficulty overrides, as pairs rather than a mapping: `Params` is an
    #: `lru_cache` key on `scheduler` below, so every field has to stay hashable.
    #: Three entries at most, so the scan in `retention_for` is free.
    retention: tuple[tuple[str, float], ...]
    learning_steps: tuple[int, ...]  # minutes
    relearning_steps: tuple[int, ...]  # minutes
    maximum_interval: int
    hard_interval_mult: float
    #: How many non-failing reviews a card entering at each rating must survive
    #: before it is mastered, as pairs for the same hashability reason as
    #: `retention`. Empty means mastery is off, which is what v1 and v2 get.
    #:
    #: Unrelated to `stats.Mastery`, which scores how well a *tag or pattern* is
    #: going. This one is per problem and is a countdown, not a score.
    mastery: tuple[tuple[str, int], ...] = ()

    def retention_for(self, difficulty: str) -> float:
        """The recall probability to aim for on a problem of this difficulty.

        Mirrors `scoring.Weights.par_for`: the difficulty is advisory, and an
        unrecognised one falls back rather than raising in the middle of a replay.
        """
        wanted = (difficulty or "medium").lower()
        for name, value in self.retention:
            if name == wanted:
                return value
        return self.desired_retention

    def rungs_for(self, rating: Rating) -> int | None:
        """How many more successful reviews master a card entering at `rating`.

        None when this parameter set masters nothing, which is the answer for
        every version before v3 and the reason mastery could be added without
        rescheduling their history.
        """
        if not self.mastery:
            return None
        for name, value in self.mastery:
            if name == MASTERY_KEYS[rating]:
                return value
        return None


#: `[mastery]` is keyed by rating name because a TOML table cannot be keyed by
#: an enum. `failed` rather than `again`: the run vocabulary is Failed / Hard /
#: Okay / Easy, and the file is the one place the two namings have to meet.
MASTERY_KEYS = {
    Rating.Again: "failed",
    Rating.Hard: "hard",
    Rating.Good: "good",
    Rating.Easy: "easy",
}


def _parse(raw: dict[str, Any]) -> Params:
    # `desired_retention` is either a scalar for everything (v1) or a table
    # keyed by difficulty (v2). The scalar is kept in both cases as the fallback
    # for a difficulty the table does not name.
    raw_retention = raw.get("desired_retention", 0.9)
    if isinstance(raw_retention, dict):
        retention = tuple((k.lower(), float(v)) for k, v in raw_retention.items())
        # A table with no `medium` still needs something to fall back to.
        default = float(raw_retention.get("medium", next(iter(raw_retention.values()), 0.9)))
    else:
        retention = ()
        default = float(raw_retention)

    return Params(
        name=raw.get("name", "?"),
        version=int(raw.get("version", 0)),
        weights=tuple(float(x) for x in raw["weights"]),
        desired_retention=default,
        retention=retention,
        learning_steps=tuple(int(x) for x in raw.get("learning_steps", [])),
        relearning_steps=tuple(int(x) for x in raw.get("relearning_steps", [])),
        maximum_interval=int(raw.get("maximum_interval", 36500)),
        hard_interval_mult=float(raw.get("hard_interval_mult", 1.0)),
        mastery=tuple((k.lower(), int(v)) for k, v in raw.get("mastery", {}).items()),
    )


@functools.lru_cache(maxsize=8)
def load_params(name: str = DEFAULT_PARAMS) -> Params:
    with resources.files(_params_pkg).joinpath(f"{name}.toml").open("rb") as fh:
        return _parse(tomllib.load(fh))


def available_params() -> list[str]:
    return sorted(
        p.name.removesuffix(".toml")
        for p in resources.files(_params_pkg).iterdir()
        if p.name.endswith(".toml")
    )


@functools.lru_cache(maxsize=16)
def scheduler(params: Params, retention: float | None = None) -> Scheduler:
    """The one construction site for a `Scheduler`.

    `enable_fuzzing=False` is not a preference. See the module docstring: it is
    what makes `p99 replay` reproducible, and `tests/test_srs.py` asserts it.

    `retention` overrides the file's default, which is how a hard problem gets a
    tighter schedule than an easy one -- a scheduler per difficulty, all of them
    cached. `None` means "whatever the file says", so a bare `scheduler(params)`
    still answers questions about the parameters themselves.
    """
    return Scheduler(
        parameters=params.weights,
        desired_retention=params.desired_retention if retention is None else retention,
        learning_steps=[timedelta(minutes=m) for m in params.learning_steps],
        relearning_steps=[timedelta(minutes=m) for m in params.relearning_steps],
        maximum_interval=params.maximum_interval,
        enable_fuzzing=False,
    )


# --- time ------------------------------------------------------------------


def parse_ts(ts: str) -> datetime:
    """Event timestamp -> the tz-aware UTC datetime `review_card` demands.

    `events.utc_now` writes ISO8601 with a `+00:00` offset, but the log may also
    hold naive timestamps written by hand or by an older version; treat those as
    UTC rather than raising in the middle of a replay.
    """
    dt = datetime.fromisoformat(ts)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _card_id(slug: str) -> int:
    """A stable id for a slug.

    `Card()` would derive one from the clock (and sleep 1ms doing it). We key
    cards by slug and never read this, but it has to be *some* deterministic
    value or replay stops reproducing. `hash()` is salted per process; crc32 is
    not.
    """
    return zlib.crc32(slug.encode())


def new_card(slug: str, at: datetime) -> Card:
    """A card for a problem seen for the first time, with nothing left to the clock."""
    return Card(card_id=_card_id(slug), due=at)


# --- the rating map (spec §8) ----------------------------------------------


def rate(attempt: Mapping[str, Any], difficulty: str, weights: scoring.Weights) -> Rating:
    """Grade one finished attempt as an FSRS rating.

    Spec §8, with performance owning the decision and `self_confidence` able to
    lower the result but never to raise it. The self-report is the thing FSRS
    actually models, but it is also the thing you can flatter yourself with; the
    clock and the hint tier cannot be argued with. See the branch below for why
    that asymmetry is the whole of how the self-report is read.

    This map is code, not parameters, so it applies whichever `data/srs/*.toml`
    is selected -- the same way `scoring.help_tier` applies to every weights
    file. Switching to v1 changes the model's constants, not this.

    The verdict ladder needs no branch of its own: it lands in `scoring.help_tier`
    alongside the hints, and the tier thresholds below already say what to do with
    it. Solving after reading the pseudocode or the implementation is tier 3+ and
    rates Again; after the description, or with hints, rates Hard.

    `used_editorial` postdates the spec, which only names `gave_up`. It sits in
    `ZERO_VERDICTS` beside it, and commit 100efb0's bargain was that reading the
    editorial still schedules a review -- so it rates the same: Again.

    `ungraded` never reaches here: `grade_attempt` returns before calling this,
    because the one thing every branch below assumes is that the verdict says
    something about whether you were right.
    """
    verdict = attempt.get("verdict")
    tier = scoring.help_tier(attempt)
    active = int(attempt.get("active_seconds") or 0)
    confidence = attempt.get("self_confidence")
    par = weights.par_for(difficulty)

    # Anything you did not solve at all, or solved only after most of the answer
    # was in front of you. Legacy `wrong_answer`/`tle` land here too -- you left
    # without an answer, whatever the reason.
    if verdict not in scoring.CLEAN_VERDICTS or tier >= 3:
        return Rating.Again

    if tier >= 1 or active > 1.5 * par:
        return Rating.Hard

    # The self-report is used in one direction only, and that asymmetry is the
    # point. A judgement made seconds after solving is inflated -- the solution
    # is still in working memory, so it feels more durable than it is, and the
    # metamemory literature measures immediate judgements of learning as only
    # weakly predictive next to delayed ones. A *high* rating therefore runs with
    # the bias and earns nothing: it no longer promotes anything to Easy, which
    # is a change from v1, where it was the only way to reach Easy at all.
    #
    # A *low* rating runs against the bias. Saying a solve will not stick, right
    # after producing one, is the one self-report the bias does not explain --
    # so it is the one worth acting on.
    if confidence is not None and int(confidence) <= 2:
        return Rating.Hard

    if active <= 0.6 * par and tier == 0:
        return Rating.Easy

    return Rating.Good


# --- the card projection ---------------------------------------------------


def _load_card(conn: sqlite3.Connection, slug: str) -> Card | None:
    row = conn.execute("SELECT * FROM fsrs_cards WHERE slug = ?", (slug,)).fetchone()
    if row is None:
        return None
    return Card(
        card_id=_card_id(slug),
        state=STATE_VALUES[row["state"]],
        step=row["step"],
        stability=row["stability"],
        difficulty=row["difficulty"],
        due=parse_ts(row["due"]),
        last_review=parse_ts(row["last_review"]) if row["last_review"] else None,
    )


def _store_card(
    conn: sqlite3.Connection,
    slug: str,
    card: Card,
    *,
    reps: int,
    lapses: int,
    rungs_left: int | None,
    mastered_at: str | None,
) -> None:
    conn.execute(
        "INSERT INTO fsrs_cards"
        "(slug, stability, difficulty, due, last_review, reps, lapses, state, step, "
        " rungs_left, mastered_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(slug) DO UPDATE SET "
        "  stability = excluded.stability, difficulty = excluded.difficulty, "
        "  due = excluded.due, last_review = excluded.last_review, "
        "  reps = excluded.reps, lapses = excluded.lapses, "
        "  state = excluded.state, step = excluded.step, "
        "  rungs_left = excluded.rungs_left, mastered_at = excluded.mastered_at",
        (
            slug,
            card.stability,
            card.difficulty,
            card.due.isoformat(),
            card.last_review.isoformat() if card.last_review else None,
            reps,
            lapses,
            STATE_NAMES[card.state],
            card.step,
            rungs_left,
            mastered_at,
        ),
    )


def _next_rungs(params: Params, rating: Rating, prior: sqlite3.Row | None) -> int | None:
    """How many successful reviews this card still owes before it is mastered.

    None all the way down when the parameter set has no `[mastery]` table, which
    is how v1 and v2 keep scheduling exactly as they always did.

    Three cases, and the first is the one that keeps the counter honest: a
    failure restarts the failed ladder from the bottom, however far up the card
    had climbed. Forgetting a problem is not a smaller version of remembering
    it, and a card that lapses at the last rung should not be mastered on the
    next solve. Otherwise a card with no counter yet is entering, and takes the
    ladder its rating names; a card that already has one has just cleared a rung.
    """
    if not params.mastery:
        return None
    if rating == Rating.Again:
        return params.rungs_for(Rating.Again)
    if prior is None or prior["rungs_left"] is None:
        return params.rungs_for(rating)
    return max(0, int(prior["rungs_left"]) - 1)


def grade_attempt(
    conn: sqlite3.Connection,
    attempt_uuid: str,
    *,
    at: str,
    params: Params | None = None,
    weights: scoring.Weights | None = None,
) -> Rating | None:
    """Fold one finished attempt into its problem's card.

    Called from `events.apply` on `problem_finished` and `problem_abandoned`,
    *after* the attempt row has been updated -- every input to the rating map is
    on the row by then: the verdict and timing from the event being applied, the
    hint tier from earlier `hint_revealed` events.

    Returns the rating applied, or None if the attempt or its problem is
    missing. Reads no clock: `at` is the event's own timestamp.
    """
    row = conn.execute(
        "SELECT a.slug, a.verdict, a.active_seconds, a.max_hint_tier, a.self_confidence, "
        "       p.difficulty "
        "FROM attempts a LEFT JOIN problems p ON p.slug = a.slug "
        "WHERE a.uuid = ?",
        (attempt_uuid,),
    ).fetchone()
    # No verdict: the attempt is still open. `ungraded`: it is closed, but
    # nothing judged it, so there is no outcome to fold in. Both leave the card
    # exactly where it was rather than inventing a rating.
    if row is None or row["verdict"] is None:
        return None
    if row["verdict"] in scoring.UNSCHEDULED_VERDICTS:
        return None

    params = params or load_params()
    weights = weights or scoring.load_weights()
    # A plain mapping, so `rate` takes the same shape `scoring.score_attempt`
    # does and both stay testable without a database.
    facts = dict(row)
    slug = facts["slug"]
    difficulty = facts["difficulty"] or "medium"
    reviewed_at = parse_ts(at)

    rating = rate(facts, difficulty, weights)
    card = _load_card(conn, slug) or new_card(slug, reviewed_at)
    prior = conn.execute(
        "SELECT reps, lapses, rungs_left, mastered_at FROM fsrs_cards WHERE slug = ?", (slug,)
    ).fetchone()
    reps = (prior["reps"] or 0) + 1 if prior else 1
    lapses = (prior["lapses"] or 0) if prior else 0
    # A lapse is forgetting something you knew, which is what FSRS's Again means
    # on a card that already exists. A problem you have never finished has no
    # card, and failing it is not forgetting -- it is not knowing yet. It still
    # rates Again, because a day-scale interval is the right answer either way;
    # it just does not go in the column that counts how often you have lost
    # something. Without this the count reads as a forgetting rate while mostly
    # measuring first contact.
    if rating == Rating.Again and prior is not None:
        lapses += 1

    duration_ms = int(facts["active_seconds"] or 0) * 1000
    card, _ = scheduler(params, params.retention_for(difficulty)).review_card(
        card, rating, review_datetime=reviewed_at, review_duration=duration_ms or None
    )

    # Spec §8 mitigation (a) as v1 implemented it: shrink the computed interval
    # for hard problems. v2 does not set the multiplier and never enters here,
    # because the approach is unsound -- and the comment this replaces claimed
    # otherwise. Moving `due` back does not leave the model's state alone: the
    # next review then lands above the target retrievability, FSRS grants less
    # stability for an early review, and the shortfall compounds. Six on-time
    # Good reviews under v1's 0.8 end at 48.9% of the stability the model asked
    # for. v2 reaches for the same shorter intervals through `desired_retention`
    # instead, which moves `due` by moving what the model is aiming at.
    #
    # Kept, and kept working, because v1 is still selectable: choosing it has to
    # get you v1's model rather than a file that quietly no longer does anything.
    if difficulty.lower() == "hard" and params.hard_interval_mult != 1.0:
        interval = card.due - reviewed_at
        if interval > timedelta(0):
            card.due = reviewed_at + interval * params.hard_interval_mult

    # Mastery (v3's `[mastery]` table). The counter is the whole mechanism: it
    # is set on the way in from the rating, decremented by every review that is
    # not a failure, and reset by one that is. At zero the problem is mastered
    # and leaves the rotation -- `due_cards` stops offering it, and only a mixed
    # or mock run brings it back.
    #
    # `due` is still stored, and still moves. A mastered card is hidden, not
    # deleted: fail it in a mock and `_next_rungs` puts it back on the failed
    # ladder with a live schedule already under it, rather than having to invent
    # one from nothing.
    rungs_left = _next_rungs(params, rating, prior)
    mastered_at = None
    if rungs_left is not None and rungs_left <= 0:
        # The date it was *first* mastered, not the last time something
        # confirmed it. A mastered problem that comes back in a mock and goes
        # well should not look like it was mastered today.
        mastered_at = (prior["mastered_at"] if prior else None) or at

    _store_card(
        conn,
        slug,
        card,
        reps=reps,
        lapses=lapses,
        rungs_left=rungs_left,
        mastered_at=mastered_at,
    )
    return rating


# --- reads -----------------------------------------------------------------


def card_row(conn: sqlite3.Connection, slug: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM fsrs_cards WHERE slug = ?", (slug,)).fetchone()


def is_due_review(conn: sqlite3.Connection, slug: str) -> bool:
    """Is starting this problem a scheduled review rather than a first encounter?

    True once the problem has a card in a review state -- which is what
    `attempts.is_review` means, and what earns `review_mult` in scoring. The
    due date deliberately does not enter into it: solving a review early is
    still a review.
    """
    row = card_row(conn, slug)
    return bool(row and row["state"] in REVIEW_STATES)


#: The catalog columns a card is read with. `difficulty` is aliased because both
#: tables have one and they are different things: `problems.difficulty` is
#: 'easy'/'medium'/'hard', `fsrs_cards.difficulty` is FSRS's internal float.
#: `sqlite3.Row` resolves a duplicate name to the *first* matching column, so an
#: unaliased `SELECT c.*, p.difficulty` silently hands back the float -- which is
#: a `float` where a string is expected, three call sites away.
_PROBLEM_COLUMNS = (
    "p.title AS title, p.difficulty AS problem_difficulty, "
    "p.pattern AS pattern, p.tags AS tags, p.lists AS lists"
)


def is_mastered(row: sqlite3.Row | Mapping[str, Any] | None) -> bool:
    """Has this problem been mastered, and so left the rotation?

    One predicate rather than `row["mastered_at"] is not None` spelled out at
    six call sites, because a row that predates the column raises `IndexError`
    on the subscript and every one of those sites would have to say so.
    """
    if row is None:
        return False
    try:
        return row["mastered_at"] is not None
    except (IndexError, KeyError):
        return False


def retrievability(
    row: sqlite3.Row | Mapping[str, Any], on: datetime, params: Params | None = None
) -> float:
    """The model's estimate that you could recall this problem right now.

    The one number that says both "how overdue" and "how weak" at once, which is
    what a one-review-a-day budget has to rank on: a card three days past a
    four-day interval has forgotten far more than one three days past a
    hundred-day interval, and ordering by `due` alone cannot see the difference.

    Reads only the decay term (`weights[20]`) out of `params`, so the ordering is
    the same under v1, v2 and v3 -- until an optimizer run moves it.
    """
    stability = row["stability"]
    last_review = row["last_review"]
    if not stability or not last_review:
        return 0.0
    # `difficulty` is not in the retrievability formula at all -- it is
    # `(1 + FACTOR * elapsed / stability) ** DECAY` -- but `Card` wants one, and
    # reading it off the row would mean caring which table's column won the join.
    card = Card(
        card_id=0,
        stability=float(stability),
        due=parse_ts(row["due"]) if row["due"] else on,
        last_review=parse_ts(last_review),
    )
    return scheduler(params or load_params()).get_card_retrievability(card, on)


def due_cards(
    conn: sqlite3.Connection, on: datetime, params: Params | None = None
) -> list[sqlite3.Row]:
    """Cards due at or before `on`, **weakest first**, mastered ones excluded.

    "Weakest" and not "oldest": due dates are priorities, not deadlines, and the
    queue takes about one of these a day. Ordering by `due` hands that slot to
    whatever happens to have waited longest, which on a backlog is a card you
    failed once in June rather than the one you are actively losing. Ranking by
    retrievability spends the slot on the problem furthest below its target.

    The tie-break is still `due` then slug, so the order stays reproducible when
    two cards are equally forgotten -- which is the common case at the floor,
    where everything has decayed to the same near-zero.
    """
    rows = conn.execute(
        f"SELECT c.*, {_PROBLEM_COLUMNS} "
        "FROM fsrs_cards c JOIN problems p ON p.slug = c.slug "
        "WHERE c.due <= ? AND c.mastered_at IS NULL",
        (on.isoformat(),),
    ).fetchall()
    resolved = params or load_params()
    return sorted(rows, key=lambda r: (retrievability(r, on, resolved), r["due"], r["slug"]))


def mastered_cards(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Everything mastered, most recently mastered first."""
    return list(
        conn.execute(
            f"SELECT c.*, {_PROBLEM_COLUMNS} "
            "FROM fsrs_cards c JOIN problems p ON p.slug = c.slug "
            "WHERE c.mastered_at IS NOT NULL "
            "ORDER BY c.mastered_at DESC, c.slug ASC"
        ).fetchall()
    )


def cards_by_due(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Every card, soonest due first — due or not.

    `due_cards` answers "what does the scheduler want today"; this answers "in
    what order do these problems matter", which is what the offline cache walks
    when its budget is too small for the whole list.

    Mastered cards sort last rather than dropping out. They are still reachable
    -- a mixed or mock run can serve one -- so a big enough budget should still
    cache them; they just have no claim on a small one.
    """
    return list(
        conn.execute(
            "SELECT * FROM fsrs_cards "
            "ORDER BY mastered_at IS NOT NULL, due IS NULL, due ASC, slug ASC"
        ).fetchall()
    )


def counts(conn: sqlite3.Connection, on: datetime) -> tuple[int, int, int]:
    """(cards, due now, mastered) — for the home overview and `p99 doctor`.

    `cards` counts everything with a card, mastered included: it answers "how
    much of the list have you finished at least once". `due` counts only what
    the scheduler will actually offer, so the two no longer sum to anything and
    are not meant to.
    """
    total = int(conn.execute("SELECT COUNT(*) AS n FROM fsrs_cards").fetchone()["n"])
    due = int(
        conn.execute(
            "SELECT COUNT(*) AS n FROM fsrs_cards "
            "WHERE due <= ? AND mastered_at IS NULL",
            (on.isoformat(),),
        ).fetchone()["n"]
    )
    mastered = int(
        conn.execute(
            "SELECT COUNT(*) AS n FROM fsrs_cards WHERE mastered_at IS NOT NULL"
        ).fetchone()["n"]
    )
    return total, due, mastered


def next_due(conn: sqlite3.Connection) -> str | None:
    row = conn.execute(
        "SELECT MIN(due) AS d FROM fsrs_cards WHERE mastered_at IS NULL"
    ).fetchone()
    return row["d"] if row and row["d"] else None
