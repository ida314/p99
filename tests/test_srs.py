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
        # Solved clean, at or under par — but not fast enough for Easy.
        ({"verdict": "accepted", "active_seconds": PAR_MEDIUM}, "medium", Rating.Good),
        # Fast and clean. The self-report is no longer what unlocks this.
        (
            {"verdict": "accepted", "active_seconds": 100, "self_confidence": 3},
            "medium",
            Rating.Easy,
        ),
        ({"verdict": "accepted", "active_seconds": 100}, "medium", Rating.Easy),
        (
            {"verdict": "accepted", "active_seconds": 100, "self_confidence": 4},
            "medium",
            Rating.Easy,
        ),
        # ...and a low self-report pulls a fast clean solve back down.
        (
            {"verdict": "accepted", "active_seconds": 100, "self_confidence": 2},
            "medium",
            Rating.Hard,
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


def test_the_self_report_only_ever_costs_you():
    """It is read in one direction, because it is only trustworthy in one.

    Confidence is claimed seconds after solving, with the answer still on screen,
    which is when self-assessment is at its least reliable and its most flattering.
    So a high claim buys nothing -- it has to agree with the clock, and the clock
    decides. A low one is the case the flattery does not explain, so it is allowed
    to demote.
    """
    fast = {"verdict": "accepted", "active_seconds": 100}
    # Nothing the self-report says can promote a solve the clock does not rate.
    at_par = {"verdict": "accepted", "active_seconds": PAR_MEDIUM}
    assert srs.rate({**at_par, "self_confidence": 4}, "medium", WEIGHTS) == Rating.Good
    assert srs.rate({**at_par}, "medium", WEIGHTS) == Rating.Good
    # A high claim adds nothing to a fast solve that already earned Easy.
    assert srs.rate(fast, "medium", WEIGHTS) == Rating.Easy
    assert srs.rate({**fast, "self_confidence": 4}, "medium", WEIGHTS) == Rating.Easy
    assert srs.rate({**fast, "self_confidence": 3}, "medium", WEIGHTS) == Rating.Easy
    # A low one takes it away.
    for shaky in (1, 2):
        assert srs.rate({**fast, "self_confidence": shaky}, "medium", WEIGHTS) == Rating.Hard
        assert srs.rate({**at_par, "self_confidence": shaky}, "medium", WEIGHTS) == Rating.Hard


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
    assert card["due"]


def test_failing_a_problem_you_have_never_seen_is_not_a_lapse(conn):
    """A lapse is losing something. There was nothing there to lose.

    It still rates Again and still comes back tomorrow -- only the count that
    says how often you forget stays out of it.
    """
    _solve(conn, "two-sum", verdict="gave_up")
    first = _cards(conn)["two-sum"]
    assert first["lapses"] == 0
    assert first["reps"] == 1 and first["due"]

    # Now there is a card, so the next failure is forgetting.
    _solve(conn, "two-sum", verdict="gave_up")
    assert _cards(conn)["two-sum"]["lapses"] == 1


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
    # Three attempts, two of them failures — but the first failure was first
    # contact, so only the last one counts as forgetting.
    assert card["lapses"] == 1


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


def test_the_default_params_load_and_are_the_published_ones():
    assert PARAMS.name == srs.DEFAULT_PARAMS == "v2"
    assert len(PARAMS.weights) == 21  # FSRS-6; index 20 is decay
    assert 0 < PARAMS.desired_retention <= 1
    assert {"v1", "v2"} <= set(srs.available_params())


def test_v1_still_loads_and_still_behaves_the_way_it_was_recorded():
    """v2 is a new file, not an edit. History replayed under v1 comes back intact.

    The shape of `Params` changed to carry per-difficulty retention, and v1 does
    not use it -- so this is the guard that the old file still parses, and that
    the multiplier v2 abandoned is still there and still applied for anyone who
    switches back.
    """
    v1 = srs.load_params("v1")
    assert v1.name == "v1" and v1.version == 1
    assert v1.hard_interval_mult == 0.8
    assert v1.maximum_interval == 36500
    # A scalar `desired_retention` means every difficulty gets the same target.
    assert v1.retention == ()
    assert {v1.retention_for(d) for d in ("easy", "medium", "hard")} == {0.9}


def test_retention_is_per_difficulty_and_hard_aims_higher():
    assert PARAMS.retention_for("easy") == PARAMS.retention_for("medium") == 0.90
    assert PARAMS.retention_for("hard") > PARAMS.retention_for("medium")
    # Unknown difficulties fall back rather than raising mid-replay.
    assert PARAMS.retention_for("") == PARAMS.desired_retention
    assert PARAMS.retention_for("insane") == PARAMS.desired_retention
    # `Params` is an lru_cache key on `scheduler`, so it has to stay hashable.
    assert hash(PARAMS)


def _fold_hard(params, *, apply_mult, reps=6):
    """Re-solve one hard problem `reps` times, rated Good, always exactly on time.

    Mirrors what `grade_attempt` does, including v1's post-hoc shrink, so the two
    parameter files can be compared on the thing that actually differs.
    """
    at = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)
    card, t = srs.new_card("x", at), at
    sched = srs.scheduler(params, params.retention_for("hard"))
    moved = False
    for _ in range(reps):
        card, _ = sched.review_card(card, Rating.Good, review_datetime=t)
        asked_for = card.due
        if apply_mult and params.hard_interval_mult != 1.0:
            interval = card.due - t
            if interval > timedelta(0):
                card.due = t + interval * params.hard_interval_mult
        moved = moved or card.due != asked_for
        t = card.due
    return card, moved


def test_v2_never_moves_due_behind_the_models_back():
    """The invariant v1's multiplier broke: `due` is the model's own answer.

    v1 shrank it afterwards, which reads as touching only the date. It is not:
    the next review then lands above the target retrievability, FSRS grants less
    stability for an early review, and the shortfall compounds every rep.
    """
    assert PARAMS.hard_interval_mult == 1.0
    _, moved = _fold_hard(PARAMS, apply_mult=True)
    assert moved is False  # nothing to apply, so nothing is overridden

    # v1, same fold, with and without its shrink. The stability the model ends
    # up holding is roughly half of what it asked for — measured at ~0.49.
    v1 = srs.load_params("v1")
    asked, _ = _fold_hard(v1, apply_mult=False)
    shrunk, moved = _fold_hard(v1, apply_mult=True)
    assert moved is True
    assert shrunk.stability < 0.6 * asked.stability


def test_a_hard_problem_comes_back_sooner_than_a_medium_one(conn):
    """Spec §8 mitigation (a), now bought with retention rather than date surgery.

    The property v1's multiplier was reaching for and failed to hold: a shorter
    interval, with the model's own state left exactly where the model put it.
    """
    at = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)
    cards, intervals = {}, {}
    for difficulty in ("medium", "hard"):
        sched = srs.scheduler(PARAMS, PARAMS.retention_for(difficulty))
        card, t = srs.new_card("x", at), at
        # Two reps, not one: intervals are whole days, and on a first encounter
        # both difficulties round to 2. The split resolves from the second
        # review on (medium 11d against hard 8d) and widens from there.
        for _ in range(2):
            card, _ = sched.review_card(card, Rating.Good, review_datetime=t)
            intervals[difficulty] = card.due - t
            t = card.due
        cards[difficulty] = card

    assert intervals["hard"] < intervals["medium"]
    # Same rating, same history, so the model's state is identical — only the
    # date it asks for differs. This is what `hard_interval_mult` broke.
    assert cards["hard"].stability == cards["medium"].stability
    assert cards["hard"].difficulty == cards["medium"].difficulty


def test_no_interval_ever_runs_past_the_horizon():
    """v1 inherited py-fsrs's 100-year default; nothing bounded the tail.

    Eight clean solves reached 7398 days under it. The cap is a coverage
    guarantee: a problem you have nailed still comes back within the year.
    """
    assert PARAMS.maximum_interval == 365
    at = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)
    card, t = srs.new_card("two-sum", at), at
    for _ in range(12):
        card, _ = srs.scheduler(PARAMS).review_card(card, Rating.Easy, review_datetime=t)
        assert card.due - t <= timedelta(days=365)
        t = card.due


def test_learning_steps_are_empty_on_purpose():
    """A 30-minute problem does not come back in 60 seconds (see v1.toml)."""
    assert PARAMS.learning_steps == ()
    assert PARAMS.relearning_steps == ()

    at = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)
    card, _ = srs.scheduler(PARAMS).review_card(
        srs.new_card("two-sum", at), Rating.Good, review_datetime=at
    )
    assert card.due - at >= timedelta(days=1)


def test_the_retention_table_reaches_the_projection(conn):
    """End to end: a hard problem's card really is scheduled tighter.

    The unit test above proves the scheduler does it; this proves `grade_attempt`
    asks for the right one, which is the wiring that actually decides your queue.
    """
    _solve(conn, "trapping-rain-water", self_confidence=3)  # hard
    _solve(conn, "3sum", self_confidence=3)  # medium

    cards = _cards(conn)

    def interval(slug):
        ended = conn.execute(
            "SELECT ended_at FROM attempts WHERE slug = ?", (slug,)
        ).fetchone()["ended_at"]
        return srs.parse_ts(cards[slug]["due"]) - srs.parse_ts(ended)

    assert interval("trapping-rain-water") > timedelta(0)
    assert interval("trapping-rain-water") < interval("3sum")
    # Same rating either way, so the stability is the model's untouched answer.
    assert cards["trapping-rain-water"]["stability"] == cards["3sum"]["stability"]


def test_parse_ts_always_returns_utc():
    assert srs.parse_ts("2026-01-01T12:00:00+00:00").tzinfo == timezone.utc
    assert srs.parse_ts("2026-01-01T12:00:00").tzinfo == timezone.utc  # naive treated as UTC
