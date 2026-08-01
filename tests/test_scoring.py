"""Scoring is a pure function computed at read time (spec §5)."""

from __future__ import annotations

import pytest

from core import scoring
from core.scoring import load_weights, score_attempt


@pytest.fixture
def w():
    return load_weights("v1")


def attempt(**overrides):
    base = {
        "verdict": "accepted",
        "active_seconds": 900,
        "max_hint_tier": 0,
        "submissions": 0,
        "lc_runtime_pct": None,
        "is_review": 0,
    }
    base.update(overrides)
    return base


def test_matches_the_spec_formula(w):
    """score = round(base * time_mult * hint_mult * review_mult) - pen + bonus."""
    a = attempt(active_seconds=900, max_hint_tier=1, submissions=3, lc_runtime_pct=87)
    # base 50, time_mult = 2 - 900/1800 = 1.5, hint_mult = 0.85, 3 failed submits
    expected = round(50 * 1.5 * 0.85) - 2 * 3 + 5
    assert score_attempt(a, "medium", w).total == expected


def test_every_failed_submission_costs(w):
    """`submissions` counts failures only, so there is no free wrong answer.

    The spec's `max(0, submissions - 1)` assumed the column held *total*
    submits; it holds failures. Charging from the first one also matches the
    spec's own worked example ("submits 2 → −4").
    """
    scores = [score_attempt(attempt(submissions=n), "medium", w).total for n in range(4)]
    assert scores == [scores[0], scores[0] - 2, scores[0] - 4, scores[0] - 6]


def test_a_failed_submission_is_not_a_clean_solve(w):
    assert scoring.is_clean_solve(attempt(submissions=0))
    assert not scoring.is_clean_solve(attempt(submissions=1))


def test_components_sum_to_the_total(w):
    for a in (
        attempt(),
        attempt(active_seconds=3000, max_hint_tier=2, submissions=4),
        attempt(active_seconds=120, lc_runtime_pct=99, is_review=1),
        attempt(verdict="wrong_answer", submissions=2),
    ):
        for difficulty in ("easy", "medium", "hard"):
            score = score_attempt(a, difficulty, w)
            assert sum(c.delta for c in score.components) == score.total


def test_gave_up_scores_zero_but_is_not_erased(w):
    score = score_attempt(attempt(verdict="gave_up", active_seconds=2400), "medium", w)
    assert score.total == 0
    assert any(c.label == "verdict" for c in score.components)


def test_used_editorial_scores_zero_and_says_so(w):
    """Reading the answer is logged as its own thing, not laundered into a solve."""
    score = score_attempt(
        attempt(verdict="used_editorial", active_seconds=1200, submissions=3), "medium", w
    )
    assert score.total == 0
    assert not score.is_clean
    assert not scoring.is_clean_solve(attempt(verdict="used_editorial"))
    labels = [c.detail for c in score.components if c.label == "verdict"]
    assert labels == ["USED EDITORIAL"]


def test_ungraded_is_not_a_solve(w):
    """Offline there is no judge, so `accepted` would be a claim nothing checked."""
    score = score_attempt(attempt(verdict="ungraded", active_seconds=1200), "medium", w)
    assert score.total == 0
    assert not score.is_clean
    assert not scoring.is_clean_solve(attempt(verdict="ungraded"))
    labels = [c.detail for c in score.components if c.label == "verdict"]
    assert labels == ["NOT GRADED"]


def test_hint_penalty_never_reaches_zero(w):
    """Reading the solution after a real fight must still beat not logging it."""
    assert w.hint_mult[-1] > 0
    tier4 = score_attempt(attempt(max_hint_tier=4), "hard", w)
    assert tier4.total > 0


def test_time_multiplier_is_floored(w):
    """A slow correct solve is never worth less than a third of a fast one."""
    fast = score_attempt(attempt(active_seconds=60), "medium", w).total
    glacial = score_attempt(attempt(active_seconds=100_000), "medium", w).total
    assert glacial > 0
    assert glacial >= fast * 0.2
    assert score_attempt(attempt(active_seconds=100_000), "medium", w).time_mult == w.time_floor


def test_reviews_are_worth_more(w):
    plain = score_attempt(attempt(), "medium", w).total
    review = score_attempt(attempt(is_review=1), "medium", w).total
    assert review > plain


def test_harder_problems_score_higher_at_equal_par_fraction(w):
    """Same fraction of par on each difficulty — hard must pay more."""
    scores = [
        score_attempt(attempt(active_seconds=w.par_for(d) // 2), d, w).total
        for d in ("easy", "medium", "hard")
    ]
    assert scores == sorted(scores)


def test_clean_solve_definition(w):
    assert scoring.is_clean_solve(attempt())
    assert not scoring.is_clean_solve(attempt(max_hint_tier=1))
    assert not scoring.is_clean_solve(attempt(submissions=1))
    assert not scoring.is_clean_solve(attempt(verdict="gave_up"))


def test_weights_are_swappable_and_rescore_history(w):
    """Nothing is denormalized, so a different weights file rescores instantly."""
    a = attempt(active_seconds=600)
    doubled = scoring.Weights(**{**w.__dict__, "base": {"medium": 100}})
    assert score_attempt(a, "medium", doubled).total > score_attempt(a, "medium", w).total


def test_unsolved_but_not_surrendered_earns_no_base(w):
    score = score_attempt(attempt(verdict="wrong_answer", submissions=3), "medium", w)
    assert score.total < 0
    assert not score.is_clean


def test_fmt_duration():
    assert scoring.fmt_duration(0) == "00:00"
    assert scoring.fmt_duration(872) == "14:32"
    assert scoring.fmt_duration(6129) == "1:42:09"
    assert scoring.fmt_duration(None) == "--:--"


def test_ordinal():
    assert [scoring.ordinal(n) for n in (1, 2, 3, 4, 11, 12, 13, 21, 82)] == [
        "1st", "2nd", "3rd", "4th", "11th", "12th", "13th", "21st", "82nd",
    ]
