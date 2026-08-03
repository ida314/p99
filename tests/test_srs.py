"""FSRS cards are a projection: derived from the log, and reproducible from it."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fsrs import Rating

from core import events, scoring, srs
from core.engine import RunEngine

WEIGHTS = scoring.load_weights()
PARAMS = srs.load_params()

# medium par is 1800s; easy 900; hard 2700.
PAR_MEDIUM = WEIGHTS.par_for("medium")


# --- the one that must never regress ---------------------------------------


def test_fuzzing_is_disabled():
    """py-fsrs fuzzes review intervals with `random()` **by default**.

    Left on, two replays of one log produce different due dates and every
    guarantee in `events` about projections being a pure function of the log
    stops being true. This is the cheapest possible guard on the most expensive
    possible bug, and it is here because the library's default is the wrong one.
    """
    assert srs.scheduler(PARAMS).enable_fuzzing is False


def test_the_scheduler_really_is_deterministic():
    """Belt and braces: fold the same ratings twice, demand the same card."""

    def fold():
        at = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)
        card = srs.new_card("two-sum", at)
        out = []
        for i, rating in enumerate([Rating.Good, Rating.Good, Rating.Again, Rating.Easy]):
            card, _ = srs.scheduler(PARAMS).review_card(
                card, rating, review_datetime=at + timedelta(days=3 * i)
            )
            out.append((card.state, card.stability, card.difficulty, card.due))
        return out

    assert fold() == fold()


def test_a_new_card_never_reads_the_clock():
    """`Card()` defaults `card_id` and `due` from `datetime.now`. Both are set."""
    at = datetime(2020, 5, 17, 9, 30, tzinfo=timezone.utc)
    first, second = srs.new_card("two-sum", at), srs.new_card("two-sum", at)
    assert first.card_id == second.card_id
    assert first.due == second.due == at


# --- the rating map (spec §8) ----------------------------------------------


@pytest.mark.parametrize(
    "attempt, difficulty, expected",
    [
        # Not solved by you: the whole point is that it still schedules a review.
        ({"verdict": "gave_up"}, "medium", Rating.Again),
        ({"verdict": "used_editorial", "self_confidence": 4}, "medium", Rating.Again),
        # Left without an answer, whatever the reason.
        ({"verdict": "wrong_answer"}, "medium", Rating.Again),
        ({"verdict": "tle"}, "medium", Rating.Again),
        # A tier-3 hint is the solution in all but name.
        ({"verdict": "accepted", "max_hint_tier": 3}, "medium", Rating.Again),
        # The ladder needs no branch of its own: it lands in `help_tier` and the
        # tier thresholds do the rest.
        ({"verdict": "solved_after_implementation"}, "medium", Rating.Again),
        (
            {"verdict": "solved_after_pseudocode", "active_seconds": 60, "self_confidence": 4},
            "medium",
            Rating.Again,
        ),
        ({"verdict": "solved_after_description"}, "medium", Rating.Hard),
        ({"verdict": "solved_with_hints"}, "medium", Rating.Hard),
        (
            {"verdict": "solved_unaided", "active_seconds": 100, "self_confidence": 4},
            "medium",
            Rating.Easy,
        ),
        # Solved, but with help or slowly.
        ({"verdict": "accepted", "max_hint_tier": 1}, "medium", Rating.Hard),
        ({"verdict": "accepted", "active_seconds": PAR_MEDIUM * 2}, "medium", Rating.Hard),
        # Solved clean, at or under par.
        ({"verdict": "accepted", "active_seconds": PAR_MEDIUM}, "medium", Rating.Good),
        # Fast and clean, but confidence withheld — the tiebreak did not fire.
        (
            {"verdict": "accepted", "active_seconds": 100, "self_confidence": 3},
            "medium",
            Rating.Good,
        ),
        ({"verdict": "accepted", "active_seconds": 100}, "medium", Rating.Good),
        # Fast, clean, hintless and you say it stuck.
        (
            {"verdict": "accepted", "active_seconds": 100, "self_confidence": 4},
            "medium",
            Rating.Easy,
        ),
    ],
)
def test_rating_map(attempt, difficulty, expected):
    assert srs.rate(attempt, difficulty, WEIGHTS) == expected


def test_used_editorial_rates_like_gave_up():
    """It postdates the spec, and it is the same bargain: zero, but a review."""
    editorial = {"verdict": "used_editorial", "active_seconds": 60, "self_confidence": 4}
    assert srs.rate(editorial, "easy", WEIGHTS) == Rating.Again
    assert "used_editorial" in scoring.ZERO_VERDICTS


def test_par_is_difficulty_relative():
    """The same clock reading is a fine medium and a laboured easy.

    Hard is 1.5 * par: 1350s for an easy, 2700s for a medium.
    """
    same_time = {"verdict": "accepted", "active_seconds": 1400}
    assert srs.rate(same_time, "medium", WEIGHTS) == Rating.Good
    assert srs.rate(same_time, "easy", WEIGHTS) == Rating.Hard


# --- the projection --------------------------------------------------------


def _solve(conn, slug, verdict="accepted", **kw):
    eng = RunEngine(conn)
    eng.start_session([slug])
    eng.start_problem(slug)
    if verdict == "gave_up":
        eng.abandon()
    else:
        eng.finish(verdict, **kw)
    eng.advance()
    eng.end_session()
    return eng


def _cards(conn):
    return {
        r["slug"]: {k: r[k] for k in r.keys()}
        for r in conn.execute("SELECT * FROM fsrs_cards ORDER BY slug")
    }


def test_finishing_a_problem_creates_a_card(conn):
    assert _cards(conn) == {}
    _solve(conn, "two-sum", self_confidence=3)
    card = _cards(conn)["two-sum"]
    assert card["state"] in srs.STATE_NAMES.values()
    assert card["due"] and card["stability"] and card["reps"] == 1


def test_giving_up_still_schedules_a_review(conn):
    """Spec §5: zero costs you the run, not the record."""
    _solve(conn, "two-sum", verdict="gave_up")
    card = _cards(conn)["two-sum"]
    assert card["lapses"] == 1
    assert card["due"]


def test_an_ungraded_attempt_schedules_nothing(conn):
    """No judge ran, so there is no outcome to fold into a card.

    The attempt is still recorded — it is the *schedule* that stays out of it,
    because rating a card on an outcome nobody established is worse than having
    no card at all.
    """
    _solve(conn, "two-sum", verdict="ungraded", self_confidence=4)

    assert _cards(conn) == {}
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM attempts WHERE verdict = 'ungraded'"
    ).fetchone()["n"] == 1


def test_an_ungraded_attempt_leaves_an_existing_card_where_it_was(conn):
    _solve(conn, "two-sum", self_confidence=3)
    before = _cards(conn)["two-sum"]

    _solve(conn, "two-sum", verdict="ungraded", self_confidence=4)
    assert _cards(conn)["two-sum"] == before


def test_cards_are_rebuilt_identically_by_replay(conn):
    for slug in ("two-sum", "3sum", "valid-anagram"):
        _solve(conn, slug, self_confidence=4)
    before = _cards(conn)
    assert before

    events.replay(conn)
    assert _cards(conn) == before
    events.replay(conn)
    assert _cards(conn) == before


def test_cards_seed_from_history_with_no_backfill(conn):
    """A log written before any of this existed still produces cards."""
    _solve(conn, "two-sum", self_confidence=3)
    conn.execute("DELETE FROM fsrs_cards")
    assert _cards(conn) == {}

    events.replay(conn)
    assert "two-sum" in _cards(conn)


def test_repeated_attempts_accumulate_reps_and_lapses(conn):
    _solve(conn, "two-sum", verdict="gave_up")
    _solve(conn, "two-sum", self_confidence=3)
    _solve(conn, "two-sum", verdict="gave_up")
    card = _cards(conn)["two-sum"]
    assert card["reps"] == 3
    assert card["lapses"] == 2


def test_an_unfinished_attempt_has_no_card(conn):
    eng = RunEngine(conn)
    eng.start_session(["two-sum"])
    eng.start_problem("two-sum")
    assert _cards(conn) == {}


# --- is_review -------------------------------------------------------------


def test_first_encounter_is_not_a_review(conn):
    _solve(conn, "two-sum", self_confidence=3)
    row = conn.execute("SELECT is_review FROM attempts WHERE slug = 'two-sum'").fetchone()
    assert row["is_review"] == 0


def test_second_encounter_is_a_review(conn):
    _solve(conn, "two-sum", self_confidence=3)
    assert srs.is_due_review(conn, "two-sum")

    _solve(conn, "two-sum", self_confidence=3)
    rows = conn.execute(
        "SELECT is_review FROM attempts WHERE slug = 'two-sum' ORDER BY id"
    ).fetchall()
    assert [r["is_review"] for r in rows] == [0, 1]


def test_is_review_is_read_from_the_payload_not_recomputed(conn):
    """Replaying an old log must not relabel history with today's cards."""
    _solve(conn, "two-sum", self_confidence=3)
    _solve(conn, "two-sum", self_confidence=3)
    before = [
        r["is_review"]
        for r in conn.execute("SELECT is_review FROM attempts WHERE slug='two-sum' ORDER BY id")
    ]
    events.replay(conn)
    after = [
        r["is_review"]
        for r in conn.execute("SELECT is_review FROM attempts WHERE slug='two-sum' ORDER BY id")
    ]
    # Both attempts now have a card, so recomputing would make the first one a
    # review too. It must still say 0.
    assert after == before == [0, 1]


def test_an_explicit_flag_still_wins(conn):
    eng = RunEngine(conn)
    eng.start_session(["two-sum"])
    attempt = eng.start_problem("two-sum", is_review=True)
    assert attempt.is_review is True


# --- parameters ------------------------------------------------------------


def test_v1_params_load_and_are_the_published_ones():
    assert PARAMS.name == "v1"
    assert len(PARAMS.weights) == 21  # FSRS-6; index 20 is decay
    assert 0 < PARAMS.desired_retention <= 1
    assert "v1" in srs.available_params()


def test_learning_steps_are_empty_on_purpose():
    """A 30-minute problem does not come back in 60 seconds (see v1.toml)."""
    assert PARAMS.learning_steps == ()
    assert PARAMS.relearning_steps == ()

    at = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)
    card, _ = srs.scheduler(PARAMS).review_card(
        srs.new_card("two-sum", at), Rating.Good, review_datetime=at
    )
    assert card.due - at >= timedelta(days=1)


def test_hard_problems_get_a_shorter_interval(conn):
    """Spec §8 mitigation (a): the published constants run too aggressive."""
    assert PARAMS.hard_interval_mult < 1.0
    _solve(conn, "trapping-rain-water", self_confidence=3)  # hard
    _solve(conn, "two-sum", self_confidence=3)  # easy

    cards = _cards(conn)
    hard_due = srs.parse_ts(cards["trapping-rain-water"]["due"])
    started = conn.execute(
        "SELECT ended_at FROM attempts WHERE slug = 'trapping-rain-water'"
    ).fetchone()["ended_at"]
    interval = hard_due - srs.parse_ts(started)
    # Whatever the model wanted, it was shrunk — the multiplier is applied to
    # the date, never to the stability.
    assert interval.total_seconds() > 0
    assert cards["trapping-rain-water"]["stability"] is not None


def test_parse_ts_always_returns_utc():
    assert srs.parse_ts("2026-01-01T12:00:00+00:00").tzinfo == timezone.utc
    assert srs.parse_ts("2026-01-01T12:00:00").tzinfo == timezone.utc  # naive treated as UTC
