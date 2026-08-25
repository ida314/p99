"""FSRS cards are a projection: derived from the log, and reproducible from it."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fsrs import Rating

from core import config, events, scoring, srs
from core.engine import RunEngine

WEIGHTS = scoring.load_weights()
PARAMS = srs.load_params()
#: v2 is still selectable, and several properties below belong to it rather than
#: to whatever the default happens to be: per-difficulty retention is a v2
#: feature that v3 deliberately drops. Naming it here keeps those tests testing
#: the file that makes the claim.
V2 = srs.load_params("v2")

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

#: What a solve has to say about itself to be eligible for Easy: optimal on
#: time, and priced on both axes. Spelled once because every Easy case below
#: needs all three and none of them is what that case is about.
PRICED = {
    "time_optimality": "optimal",
    "claimed_complexity": "O(n)",
    "claimed_space_complexity": "O(1)",
}


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
            {"verdict": "solved_unaided", "active_seconds": 100, "self_confidence": 4, **PRICED},
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
            {"verdict": "accepted", "active_seconds": 100, "self_confidence": 3, **PRICED},
            "medium",
            Rating.Easy,
        ),
        ({"verdict": "accepted", "active_seconds": 100, **PRICED}, "medium", Rating.Easy),
        (
            {"verdict": "accepted", "active_seconds": 100, "self_confidence": 4, **PRICED},
            "medium",
            Rating.Easy,
        ),
        # ...and a low self-report pulls a fast clean solve back down.
        (
            {"verdict": "accepted", "active_seconds": 100, "self_confidence": 2, **PRICED},
            "medium",
            Rating.Hard,
        ),
        # --- the second axis: what the solution cost ------------------------
        #
        # An asymptotic gap you did not spot is a gap in the pattern, so a fast
        # flawless solve that admits to being beaten is still Hard.
        (
            {"verdict": "accepted", "active_seconds": 100, "time_optimality": "suboptimal"},
            "medium",
            Rating.Hard,
        ),
        # ...unless you named the better approach yourself afterwards, which is
        # the thing being trained. Okay, not Hard.
        (
            {
                "verdict": "accepted",
                "active_seconds": 100,
                "time_optimality": "suboptimal",
                "saw_better": 1,
            },
            "medium",
            Rating.Good,
        ),
        # Spotting it does not launder away a slow solve or a revealed hint:
        # the floor is the worst of every demote, not the last one to fire.
        (
            {
                "verdict": "accepted",
                "active_seconds": PAR_MEDIUM * 2,
                "time_optimality": "suboptimal",
                "saw_better": 1,
            },
            "medium",
            Rating.Hard,
        ),
        (
            {
                "verdict": "accepted",
                "active_seconds": 100,
                "time_optimality": "suboptimal",
                "saw_better": 1,
                "self_confidence": 1,
            },
            "medium",
            Rating.Hard,
        ),
        # `unsure` is the default answer and costs nothing. Being unsure is the
        # honest state of most solves; it is not evidence of anything.
        (
            {"verdict": "accepted", "active_seconds": 100, "time_optimality": "unsure"},
            "medium",
            Rating.Good,
        ),
        # Easy also asks you to price it. A solution you cannot cost is one you
        # pattern-matched, and half an answer is not an answer: time alone,
        # space alone and neither all stop short of Easy.
        (
            {"verdict": "accepted", "active_seconds": 100, "time_optimality": "optimal"},
            "medium",
            Rating.Good,
        ),
        (
            {
                "verdict": "accepted",
                "active_seconds": 100,
                "time_optimality": "optimal",
                "claimed_complexity": "O(n)",
            },
            "medium",
            Rating.Good,
        ),
        (
            {
                "verdict": "accepted",
                "active_seconds": 100,
                "time_optimality": "optimal",
                "claimed_complexity": "   ",
                "claimed_space_complexity": "O(1)",
            },
            "medium",
            Rating.Good,
        ),
        # An attempt written before any of this existed answers none of it, and
        # grades exactly as it always did -- except that it cannot reach Easy,
        # which now needs a claim nobody made.
        ({"verdict": "accepted", "active_seconds": 100}, "medium", Rating.Good),
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
    fast = {"verdict": "accepted", "active_seconds": 100, **PRICED}
    # Nothing the self-report says can promote a solve the clock does not rate.
    at_par = {"verdict": "accepted", "active_seconds": PAR_MEDIUM, **PRICED}
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
    """A solve that answered everything, unless the caller says otherwise.

    The priced defaults are what make this an *Easy* solve rather than merely a
    fast one: since the rating map grew its second axis, Easy needs a time
    optimality claim and a cost on both axes. Every caller below that reasons
    about the mastery ladder wants the top rung, and spelling three unrelated
    kwargs at each of them would bury what each test is actually about.
    """
    kw = {
        "time_optimality": "optimal",
        "claimed_complexity": "O(n)",
        "claimed_space_complexity": "O(1)",
        **kw,
    }
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


def test_the_default_params_load_and_only_the_entry_intervals_are_hand_set():
    """v3 replaces `weights[0..3]` by hand and leaves the other seventeen alone.

    The narrowness is the claim. `weights[0..3]` are the initial stabilities, and
    stability is the interval at which recall falls to 0.9 -- so at
    `desired_retention = 0.9` those four numbers *are* the first-review intervals
    in days, which is what makes hand-setting them a statement about this domain
    rather than a guess at the model. Everything from index 4 on describes the
    shape of forgetting and stays fitted.
    """
    assert PARAMS.name == srs.DEFAULT_PARAMS == "v3"
    assert len(PARAMS.weights) == 21  # FSRS-6; index 20 is decay
    assert 0 < PARAMS.desired_retention <= 1
    assert {"v1", "v2", "v3"} <= set(srs.available_params())

    # Failed / Hard / Okay / Easy, in days.
    assert PARAMS.weights[:4] == (2.0, 5.0, 9.0, 21.0)
    # And nothing else moved.
    assert PARAMS.weights[4:] == V2.weights[4:]


def test_the_entry_intervals_really_are_the_days_they_claim_to_be():
    """The arithmetic the weights file rests on, checked rather than asserted.

    If `desired_retention` ever drifts off 0.9, or the decay term moves, the
    first four weights stop meaning days and the file's whole comment is wrong.
    """
    assert PARAMS.desired_retention == 0.9
    assert PARAMS.retention == ()  # flat, not per-difficulty: see v3.toml

    at = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)
    wanted = {Rating.Again: 2, Rating.Hard: 5, Rating.Good: 9, Rating.Easy: 21}
    for rating, days in wanted.items():
        card, _ = srs.scheduler(PARAMS).review_card(
            srs.new_card("x", at), rating, review_datetime=at
        )
        assert (card.due - at).days == days, rating


def test_a_failed_problem_no_longer_comes_back_tomorrow(conn):
    """The regression this whole version exists to fix.

    Under v2 every failure scheduled a one-day interval, forever, because
    `weights[0]` is 0.212 days. With a verdict history full of `gave_up` that
    produced 18 due cards against 19 problems ever seen.
    """
    _solve(conn, "two-sum", verdict="gave_up")
    card = _cards(conn)["two-sum"]
    ended = conn.execute(
        "SELECT ended_at FROM attempts WHERE slug = 'two-sum'"
    ).fetchone()["ended_at"]
    assert (srs.parse_ts(card["due"]) - srs.parse_ts(ended)).days == 2


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
    """A v2 property, and still v2's. v3 drops the split -- see below."""
    assert V2.retention_for("easy") == V2.retention_for("medium") == 0.90
    assert V2.retention_for("hard") > V2.retention_for("medium")
    # Unknown difficulties fall back rather than raising mid-replay.
    assert V2.retention_for("") == V2.desired_retention
    assert V2.retention_for("insane") == V2.desired_retention
    # `Params` is an lru_cache key on `scheduler`, so it has to stay hashable.
    assert hash(V2) and hash(PARAMS)


def test_v3_asks_the_same_retention_of_every_difficulty():
    """Deliberate, and load-bearing on the entry intervals above.

    0.92 multiplies an interval by ~0.73, so under v2's table `weights[0] = 2.0`
    would mean 1 day for hard problems -- exactly the ones v3 exists to stop
    scheduling for tomorrow. Difficulty is already priced in through par, which
    is what `srs.rate` compares the clock against.
    """
    assert {PARAMS.retention_for(d) for d in ("easy", "medium", "hard", "")} == {0.9}


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
        sched = srs.scheduler(V2, V2.retention_for(difficulty))
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

    Pinned to v2, which is the file that has a retention table. It also exercises
    the thing the table was always for: selecting a parameter file through the
    settings layer and having the projection grade against it.
    """
    config.set_option(conn, "srs.params", "v2")
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


# --- mastery (v3's `[mastery]` table) --------------------------------------


def _rungs(conn, slug):
    row = srs.card_row(conn, slug)
    return row["rungs_left"], row["mastered_at"]


def test_the_mastery_ladder_is_parameters_not_code():
    """v3 masters; v1 and v2 do not, and that is what makes the change replayable.

    Mastery had to be addable without rescheduling history recorded under the
    older files. It is a table in the toml, so an older file simply has none and
    `rungs_for` answers None all the way down.
    """
    assert PARAMS.rungs_for(Rating.Again) == 4
    assert PARAMS.rungs_for(Rating.Hard) == 3
    assert PARAMS.rungs_for(Rating.Good) == 2
    assert PARAMS.rungs_for(Rating.Easy) == 1
    for older in (srs.load_params("v1"), V2):
        assert older.mastery == ()
        assert all(older.rungs_for(r) is None for r in Rating)


def test_a_clean_solve_is_mastered_on_its_second_recall(conn):
    """The Easy ladder as specified: 21 days, then out.

    Short on purpose -- it was the option chosen over a 60- and a 90-day
    threshold -- so a problem you recognise and implement at pace twice running
    leaves the rotation and stops competing for the one review slot a day.
    """
    _solve(conn, "two-sum", self_confidence=3)  # instant clean solve rates Easy
    assert _rungs(conn, "two-sum") == (1, None)

    _solve(conn, "two-sum", self_confidence=3)
    rungs, mastered_at = _rungs(conn, "two-sum")
    assert rungs == 0 and mastered_at is not None


def test_a_failed_problem_takes_the_long_ladder(conn):
    """Four recalls, not one, before something you could not solve is mastered."""
    _solve(conn, "two-sum", verdict="gave_up")
    assert _rungs(conn, "two-sum")[0] == 4
    for expected in (3, 2, 1, 0):
        _solve(conn, "two-sum", self_confidence=3)
        assert _rungs(conn, "two-sum")[0] == expected
    assert _rungs(conn, "two-sum")[1] is not None


def test_forgetting_puts_you_back_at_the_bottom_of_the_failed_ladder(conn):
    """A lapse is not a smaller version of a recall.

    Without this a card that failed on its last rung would be mastered on the
    very next solve, which is the one moment the evidence says it should not.
    """
    _solve(conn, "two-sum", self_confidence=3)  # Easy: one rung left
    assert _rungs(conn, "two-sum")[0] == 1
    _solve(conn, "two-sum", verdict="gave_up")
    assert _rungs(conn, "two-sum") == (4, None)


def test_a_mastered_problem_leaves_the_schedule(conn):
    """Mastered is hidden, not deleted — the card and its due date stay put."""
    _solve(conn, "two-sum", self_confidence=3)
    _solve(conn, "two-sum", self_confidence=3)
    now = datetime.now(timezone.utc) + timedelta(days=400)

    assert srs.is_mastered(srs.card_row(conn, "two-sum"))
    assert srs.due_cards(conn, now) == []
    assert srs.next_due(conn) is None
    cards, due, mastered = srs.counts(conn, now)
    assert (cards, due, mastered) == (1, 0, 1)
    assert [r["slug"] for r in srs.mastered_cards(conn)] == ["two-sum"]
    # Still a review if a mock serves it, and still carrying a live schedule.
    assert srs.is_due_review(conn, "two-sum")
    assert srs.card_row(conn, "two-sum")["due"]


def test_failing_a_mastered_problem_brings_it_back(conn):
    """The safety valve on a one-recall mastery.

    Mastered problems can still turn up in a mixed run. Losing one there is the
    strongest evidence available that it was mastered too early, so it goes back
    on the failed ladder with the schedule it already had underneath it.
    """
    _solve(conn, "two-sum", self_confidence=3)
    _solve(conn, "two-sum", self_confidence=3)
    assert srs.is_mastered(srs.card_row(conn, "two-sum"))

    _solve(conn, "two-sum", verdict="gave_up")
    assert not srs.is_mastered(srs.card_row(conn, "two-sum"))
    assert _rungs(conn, "two-sum") == (4, None)
    assert srs.due_cards(conn, datetime.now(timezone.utc) + timedelta(days=400))


def test_mastery_survives_a_replay(conn):
    """It is a projection like everything else, or it is a bug."""
    _solve(conn, "two-sum", self_confidence=3)
    _solve(conn, "two-sum", self_confidence=3)
    before = _cards(conn)
    events.replay(conn)
    assert _cards(conn) == before


def test_the_date_a_problem_was_mastered_does_not_move(conn):
    """A mock run that goes well is not a fresh mastery."""
    _solve(conn, "two-sum", self_confidence=3)
    _solve(conn, "two-sum", self_confidence=3)
    first = srs.card_row(conn, "two-sum")["mastered_at"]

    _solve(conn, "two-sum", self_confidence=3)
    row = srs.card_row(conn, "two-sum")
    assert row["mastered_at"] == first
    assert row["rungs_left"] == 0  # stays at the floor rather than going negative


def test_due_cards_come_back_weakest_first(conn):
    """One review a day means the order is the decision.

    `due` alone cannot tell a card three days past a four-day interval from one
    three days past a hundred-day interval. Retrievability can, and the first is
    much further gone.
    """
    at = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)
    conn.execute("INSERT OR IGNORE INTO problems VALUES(?,?,?,?,?,?,?)",
                 ("weak", "Weak", "u", "medium", "[]", "p", '["neetcode150"]'))
    conn.execute("INSERT OR IGNORE INTO problems VALUES(?,?,?,?,?,?,?)",
                 ("strong", "Strong", "u", "medium", "[]", "p", '["neetcode150"]'))
    # `strong` is the older due date; `weak` has decayed much further past its.
    for slug, stability, last, due in (
        ("weak", 4.0, at - timedelta(days=40), at - timedelta(days=36)),
        ("strong", 200.0, at - timedelta(days=250), at - timedelta(days=50)),
    ):
        conn.execute(
            "INSERT INTO fsrs_cards(slug, stability, difficulty, due, last_review, "
            "reps, lapses, state, step, rungs_left, mastered_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (slug, stability, 5.0, due.isoformat(), last.isoformat(), 1, 0, "review", None, 2, None),
        )

    order = [r["slug"] for r in srs.due_cards(conn, at)]
    assert order == ["weak", "strong"]


# --- the second axis, end to end --------------------------------------------


def test_naming_the_better_approach_saves_a_beaten_solve(conn):
    """The whole point of the `worth_learning` role, measured on the schedule.

    Two identical solves, both admitting they were beaten on time. One names the
    approach it should have taken; the other names nothing. The first found the
    pattern late, the second missed it — so only the second comes back soon.
    """
    from core import strategies

    beaten = dict(
        time_optimality="suboptimal",
        claimed_complexity="O(n^2)",
        claimed_space_complexity="O(1)",
    )
    _solve(conn, "two-sum", **beaten, strategies=strategies.payload([], worth_learning=["hash map"]))
    _solve(conn, "3sum", **beaten)

    cards = _cards(conn)
    diagnosed = srs.parse_ts(cards["two-sum"]["due"])
    missed = srs.parse_ts(cards["3sum"]["due"])
    assert diagnosed > missed


def test_an_equal_alternative_changes_no_schedule(conn):
    """`also_works` is the one strategy role the scheduler must not read.

    Two identical beaten solves. One says "there is another route and it is
    about as good"; the other says nothing. That is a fact about the problem's
    library, not a diagnosis of the gap — so unlike `worth_learning` above, it
    buys back nothing, and the two cards come back on the same day.
    """
    from core import strategies

    beaten = dict(
        time_optimality="suboptimal",
        claimed_complexity="O(n^2)",
        claimed_space_complexity="O(1)",
    )
    _solve(conn, "two-sum", **beaten, strategies=strategies.payload([], also_works=["sorting"]))
    _solve(conn, "3sum", **beaten)

    cards = _cards(conn)
    assert cards["two-sum"]["due"] == cards["3sum"]["due"]
    assert cards["two-sum"]["stability"] == cards["3sum"]["stability"]


def test_admitting_you_were_beaten_costs_a_grade(conn):
    """An asymptotic gap is a gap in the pattern, however fast you closed it."""
    _solve(conn, "two-sum")  # priced and optimal by default: Easy
    _solve(conn, "3sum", time_optimality="suboptimal")

    cards = _cards(conn)
    assert srs.parse_ts(cards["two-sum"]["due"]) > srs.parse_ts(cards["3sum"]["due"])


def test_not_sure_is_free(conn):
    """The default answer cannot cost you anything, or it stops being honest."""
    unsure = srs.rate(
        {"verdict": "accepted", "active_seconds": 100, "time_optimality": "unsure"},
        "medium",
        WEIGHTS,
    )
    silent = srs.rate({"verdict": "accepted", "active_seconds": 100}, "medium", WEIGHTS)
    assert unsure == silent == Rating.Good
