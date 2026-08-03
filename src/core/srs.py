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
editing them and replaying reschedules all of history.
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

DEFAULT_PARAMS = "v1"

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
    desired_retention: float
    learning_steps: tuple[int, ...]  # minutes
    relearning_steps: tuple[int, ...]  # minutes
    maximum_interval: int
    hard_interval_mult: float


def _parse(raw: dict[str, Any]) -> Params:
    return Params(
        name=raw.get("name", "?"),
        version=int(raw.get("version", 0)),
        weights=tuple(float(x) for x in raw["weights"]),
        desired_retention=float(raw.get("desired_retention", 0.9)),
        learning_steps=tuple(int(x) for x in raw.get("learning_steps", [])),
        relearning_steps=tuple(int(x) for x in raw.get("relearning_steps", [])),
        maximum_interval=int(raw.get("maximum_interval", 36500)),
        hard_interval_mult=float(raw.get("hard_interval_mult", 1.0)),
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


@functools.lru_cache(maxsize=8)
def scheduler(params: Params) -> Scheduler:
    """The one construction site for a `Scheduler`.

    `enable_fuzzing=False` is not a preference. See the module docstring: it is
    what makes `p99 replay` reproducible, and `tests/test_srs.py` asserts it.
    """
    return Scheduler(
        parameters=params.weights,
        desired_retention=params.desired_retention,
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

    Spec §8, with performance owning the decision and `self_confidence` only
    breaking the tie at the top. The self-report is the thing FSRS actually
    models, but it is also the thing you can flatter yourself with; the clock
    and the hint tier cannot be argued with.

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

    if active <= 0.6 * par and tier == 0 and confidence is not None and int(confidence) == 4:
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


def _store_card(conn: sqlite3.Connection, slug: str, card: Card, *, reps: int, lapses: int) -> None:
    conn.execute(
        "INSERT INTO fsrs_cards"
        "(slug, stability, difficulty, due, last_review, reps, lapses, state, step) "
        "VALUES(?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(slug) DO UPDATE SET "
        "  stability = excluded.stability, difficulty = excluded.difficulty, "
        "  due = excluded.due, last_review = excluded.last_review, "
        "  reps = excluded.reps, lapses = excluded.lapses, "
        "  state = excluded.state, step = excluded.step",
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
        ),
    )


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
        "SELECT reps, lapses FROM fsrs_cards WHERE slug = ?", (slug,)
    ).fetchone()
    reps = (prior["reps"] or 0) + 1 if prior else 1
    lapses = (prior["lapses"] or 0) if prior else 0
    if rating == Rating.Again:
        lapses += 1

    duration_ms = int(facts["active_seconds"] or 0) * 1000
    card, _ = scheduler(params).review_card(
        card, rating, review_datetime=reviewed_at, review_duration=duration_ms or None
    )

    # Spec §8 mitigation (a): the published constants are fitted on flashcards
    # and run too aggressive for a hard problem. Shrink the interval, never the
    # stability -- the model's state stays the model's, only the date moves.
    if difficulty.lower() == "hard" and params.hard_interval_mult != 1.0:
        interval = card.due - reviewed_at
        if interval > timedelta(0):
            card.due = reviewed_at + interval * params.hard_interval_mult

    _store_card(conn, slug, card, reps=reps, lapses=lapses)
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


def due_cards(conn: sqlite3.Connection, on: datetime) -> list[sqlite3.Row]:
    """Cards due at or before `on`, most overdue first."""
    return list(
        conn.execute(
            "SELECT c.*, p.title, p.difficulty, p.pattern, p.tags, p.lists "
            "FROM fsrs_cards c JOIN problems p ON p.slug = c.slug "
            "WHERE c.due <= ? ORDER BY c.due ASC",
            (on.isoformat(),),
        ).fetchall()
    )


def cards_by_due(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Every card, soonest due first — due or not.

    `due_cards` answers "what does the scheduler want today"; this answers "in
    what order do these problems matter", which is what the offline cache walks
    when its budget is too small for the whole list.
    """
    return list(
        conn.execute(
            "SELECT * FROM fsrs_cards ORDER BY due IS NULL, due ASC, slug ASC"
        ).fetchall()
    )


def counts(conn: sqlite3.Connection, on: datetime) -> tuple[int, int]:
    """(cards, due now) — for the home overview and `p99 doctor`."""
    total = int(conn.execute("SELECT COUNT(*) AS n FROM fsrs_cards").fetchone()["n"])
    due = int(
        conn.execute(
            "SELECT COUNT(*) AS n FROM fsrs_cards WHERE due <= ?", (on.isoformat(),)
        ).fetchone()["n"]
    )
    return total, due


def next_due(conn: sqlite3.Connection) -> str | None:
    row = conn.execute("SELECT MIN(due) AS d FROM fsrs_cards").fetchone()
    return row["d"] if row and row["d"] else None
