"""Scoring (spec §5).

Principle: **store components, compute score on read.** `attempts` holds only
measured facts; the scalar is a pure function of those facts and a versioned
weights file. Nothing here writes to the database.

The score is multiplicative, but the stat line displays per-component deltas.
Those deltas are an exact decomposition — each one is the change in the running
product caused by that factor, so they sum to the final score.
"""

from __future__ import annotations

import functools
import tomllib
from dataclasses import dataclass
from importlib import resources
from typing import Any, Mapping

# See the note in `catalog`: a module object, not a dotted name, so the lookup
# survives a package rename.
from .data import scoring as _weights_pkg

DEFAULT_WEIGHTS = "v1"

# The verdict records how much of the answer you needed handed to you, not what
# the judge said. `accepted` describes LeetCode; `solved_after_pseudocode`
# describes the thing that actually predicts whether you'll have it in a month.
SOLVED_VERDICTS = (
    "solved_unaided",
    "solved_with_hints",
    "solved_after_description",
    "solved_after_pseudocode",
    "solved_after_implementation",
)
# Selectable in the finish modal, in radio order. Index 0 is the default.
VERDICTS = (*SOLVED_VERDICTS, "gave_up", "ungraded")

# Verdicts that mean "the problem is solved". Every rung of the ladder counts --
# the price of the help is paid through `help_tier` and the hint multipliers,
# not by withholding credit twice.
CLEAN_VERDICTS = frozenset(SOLVED_VERDICTS) | {"accepted"}
# Verdicts where the answer came from somewhere other than you: the attempt is
# worth nothing regardless of how it went, penalties included.
ZERO_VERDICTS = frozenset({"gave_up", "used_editorial"})
# Verdicts where nothing checked the answer, so there is no outcome to learn
# from: offline, with no judge to submit to. Unlike every other verdict these
# schedule no review at all -- see `srs.grade_attempt`. Rating a card on an
# outcome nobody established is worse than having no card.
UNSCHEDULED_VERDICTS = frozenset({"ungraded"})

# Written by earlier versions and still in the log. Renderable and scorable,
# never selectable: `VERDICTS` is the only thing the radio set reads. Rewriting
# them would have been a lie about what was actually recorded at the time.
LEGACY_VERDICTS = frozenset({"accepted", "wrong_answer", "tle", "used_editorial"})

# How much of the answer the verdict admits to, on the same 0..4 scale as the
# hint tiers -- so one multiplier table prices both, and the two ways of getting
# help can never be double-charged.
#
# `solved_with_hints` is a floor, not a rung: revealing tier 3 in the app and
# then picking it does not launder the attempt back down to 1. The floor exists
# because hints read on LeetCode itself never touch `max_hint_tier`, and without
# it "solved with hints" would score a flawless solve.
VERDICT_HELP_TIER = {
    "solved_unaided": 0,
    "solved_with_hints": 1,
    "solved_after_description": 2,
    "solved_after_pseudocode": 3,
    "solved_after_implementation": 4,
}

# The inverse of `VERDICT_HELP_TIER`, which is a bijection onto 0..4: the rung
# of the ladder a given effective help tier lands on. Used to report an attempt
# in the terms it earned rather than the ones it claimed -- a `solved_unaided`
# with a tier-2 hint revealed reads as the tier-2 rung.
HELP_TIER_VERDICT = {tier: verdict for verdict, tier in VERDICT_HELP_TIER.items()}

VERDICT_LABELS = {
    "solved_unaided": "SOLVED, NO HELP",
    "solved_with_hints": "SOLVED WITH HINTS",
    "solved_after_description": "SOLVED AFTER DESCRIPTION",
    "solved_after_pseudocode": "SOLVED AFTER PSEUDOCODE",
    "solved_after_implementation": "SOLVED AFTER IMPLEMENTATION",
    "gave_up": "GAVE UP",
    "ungraded": "NOT GRADED",
    # legacy
    "accepted": "ACCEPTED",
    "wrong_answer": "WRONG ANSWER",
    "tle": "TIME LIMIT EXCEEDED",
    "used_editorial": "USED EDITORIAL",
}


def help_tier(attempt: Mapping[str, Any]) -> int:
    """How much of the answer you had, 0..4: hints revealed or help confessed to.

    Whichever is worse wins. Revealing a tier-1 nudge and then reading the
    implementation anyway is a tier-4 attempt, and claiming `solved_unaided`
    after three hints does not undo the hints -- they were logged as they
    happened, and that log is the part you cannot argue with.
    """
    return max(
        int(attempt.get("max_hint_tier") or 0),
        VERDICT_HELP_TIER.get(attempt.get("verdict") or "", 0),
    )


@dataclass(frozen=True)
class Weights:
    name: str
    version: int
    base: Mapping[str, float]
    par_seconds: Mapping[str, int]
    time_intercept: float
    time_floor: float
    time_ceiling: float
    hint_mult: tuple[float, ...]
    per_extra_submission: float
    runtime_pct_threshold: float
    runtime_bonus: float
    review_mult: float

    def base_for(self, difficulty: str) -> float:
        return float(self.base.get((difficulty or "medium").lower(), 50))

    def par_for(self, difficulty: str) -> int:
        return int(self.par_seconds.get((difficulty or "medium").lower(), 1800))


def _parse(raw: dict[str, Any]) -> Weights:
    tm = raw.get("time_mult", {})
    return Weights(
        name=raw.get("name", "?"),
        version=int(raw.get("version", 0)),
        base=raw.get("base", {}),
        par_seconds=raw.get("par_seconds", {}),
        time_intercept=float(tm.get("intercept", 2.0)),
        time_floor=float(tm.get("floor", 0.4)),
        time_ceiling=float(tm.get("ceiling", 1.6)),
        hint_mult=tuple(float(x) for x in raw.get("hint_mult", [1.0])),
        per_extra_submission=float(raw.get("penalties", {}).get("per_extra_submission", 2)),
        runtime_pct_threshold=float(raw.get("bonuses", {}).get("runtime_pct_threshold", 80)),
        runtime_bonus=float(raw.get("bonuses", {}).get("runtime", 5)),
        review_mult=float(raw.get("review_mult", 1.25)),
    )


@functools.lru_cache(maxsize=8)
def load_weights(name: str = DEFAULT_WEIGHTS) -> Weights:
    with resources.files(_weights_pkg).joinpath(f"{name}.toml").open("rb") as fh:
        return _parse(tomllib.load(fh))


def available_weights() -> list[str]:
    return sorted(
        p.name.removesuffix(".toml")
        for p in resources.files(_weights_pkg).iterdir()
        if p.name.endswith(".toml")
    )


@dataclass(frozen=True)
class Component:
    """One line of the stat line: what happened, and what it was worth."""

    label: str
    detail: str
    delta: int
    ratio: float | None = None  # 0..1, for the little bar; None = no bar


@dataclass(frozen=True)
class Score:
    total: int
    components: tuple[Component, ...]
    par_seconds: int
    time_mult: float
    hint_mult: float
    review_mult: float
    weights_name: str
    is_clean: bool


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def score_attempt(attempt: Mapping[str, Any], difficulty: str, weights: Weights | None = None) -> Score:
    """Score a single attempt. Pure: `attempt` is a plain mapping of facts."""
    w = weights or load_weights()
    difficulty = (difficulty or "medium").lower()
    par = w.par_for(difficulty)
    base = w.base_for(difficulty)

    verdict = attempt.get("verdict")
    active = attempt.get("active_seconds")
    active = int(active) if active is not None else 0
    # The effective tier, not the raw column: a verdict that admits to reading
    # the pseudocode is priced exactly like the tier-3 hint that would have
    # shown it to you.
    tier = help_tier(attempt)
    help_label = _help_label(attempt)
    submissions = int(attempt.get("submissions") or 0)
    runtime_pct = attempt.get("lc_runtime_pct")
    is_review = bool(attempt.get("is_review"))

    time_mult = _clamp(w.time_intercept - (active / par if par else 0), w.time_floor, w.time_ceiling)
    hint_mult = w.hint_mult[min(tier, len(w.hint_mult) - 1)]
    review_mult = w.review_mult if is_review else 1.0
    # The spec's formula is `2 * max(0, submissions - 1)`, written when
    # `submissions` meant *total* submits including the accepted one. The schema
    # stores failed submits only (db.py: "failed submits before accept"), so the
    # -1 would hand you one free wrong answer. Charging per failure also matches
    # the spec's own worked example: "submits 2 -> -4".
    submit_pen = w.per_extra_submission * max(0, submissions)
    bonus = (
        w.runtime_bonus
        if runtime_pct is not None and float(runtime_pct) >= w.runtime_pct_threshold
        else 0.0
    )

    time_line = Component(
        "time",
        f"{fmt_duration(active)}   (par {fmt_duration(par)})",
        0,
        ratio=_clamp(1 - (active / par if par else 0), 0.0, 1.0),
    )

    # gave_up scores 0, but the attempt is still logged and still schedules a
    # review (spec §5). Zero is the point: it costs you the run, not the record.
    # `used_editorial` is the same bargain — you did reach an answer, but not one
    # you can claim, so the only thing it buys is the review.
    if verdict in ZERO_VERDICTS:
        return Score(
            total=0,
            components=(
                time_line,
                Component("hints", help_label,0),
                Component("verdict", VERDICT_LABELS[verdict], 0),
            ),
            par_seconds=par,
            time_mult=time_mult,
            hint_mult=hint_mult,
            review_mult=review_mult,
            weights_name=w.name,
            is_clean=False,
        )

    if verdict not in CLEAN_VERDICTS:
        # Unsolved but not surrendered: no base credit, only the submit penalty.
        # `ungraded` lands here too, and that is the point: an unverified solve
        # scores exactly what an unfinished one does, because in both cases
        # nothing established that you were right.
        total = -submit_pen
        components = [
            time_line,
            Component("hints", help_label,0),
            Component(
                "submits",
                str(submissions),
                -round(submit_pen) if submit_pen else 0,
            ),
            Component("verdict", VERDICT_LABELS.get(verdict or "", "UNRESOLVED"), 0),
        ]
        return Score(
            total=round(total),
            components=tuple(components),
            par_seconds=par,
            time_mult=time_mult,
            hint_mult=hint_mult,
            review_mult=review_mult,
            weights_name=w.name,
            is_clean=False,
        )

    # Exact decomposition of `round(base * time_mult * hint_mult * review_mult)
    # - submit_pen + bonus` into additive per-component deltas.
    after_time = base * time_mult
    after_hint = after_time * hint_mult
    after_review = after_hint * review_mult
    raw_total = round(after_review) - submit_pen + bonus

    components = [
        Component("verdict", VERDICT_LABELS.get(verdict or "", "SOLVED"), round(base)),
        Component(
            "time",
            f"{fmt_duration(active)}   (par {fmt_duration(par)})",
            round(after_time) - round(base),
            ratio=_clamp(1 - (active / par if par else 0), 0.0, 1.0),
        ),
        Component("hints", help_label,round(after_hint) - round(after_time)),
    ]
    if is_review:
        components.append(
            Component("review", f"x{w.review_mult:g}", round(after_review) - round(after_hint))
        )
    components.append(
        Component("submits", str(submissions), -round(submit_pen) if submit_pen else 0)
    )
    if runtime_pct is not None:
        components.append(
            Component("runtime", f"{ordinal(int(float(runtime_pct)))} pct", round(bonus))
        )

    # Guard the decomposition: rounding each stage independently can drift by a
    # point or two, so the last component absorbs any residual.
    total = round(raw_total)
    drift = total - sum(c.delta for c in components)
    if drift:
        last = components[-1]
        components[-1] = Component(last.label, last.detail, last.delta + drift, last.ratio)

    clean = verdict in CLEAN_VERDICTS and tier == 0 and submissions == 0
    return Score(
        total=total,
        components=tuple(components),
        par_seconds=par,
        time_mult=time_mult,
        hint_mult=hint_mult,
        review_mult=review_mult,
        weights_name=w.name,
        is_clean=clean,
    )


HINT_TIER_NAMES = ["none", "nudge", "approach", "pseudocode", "solution"]

# What the stat line says when the verdict, not a revealed hint, set the tier.
VERDICT_HELP_LABELS = {
    "solved_with_hints": "used hints",
    "solved_after_description": "saw the description",
    "solved_after_pseudocode": "saw the pseudocode",
    "solved_after_implementation": "read the implementation",
}


def _hint_label(tier: int) -> str:
    if not tier:
        return "none"
    return f"tier {tier}  ({HINT_TIER_NAMES[min(tier, 4)]})"


def _help_label(attempt: Mapping[str, Any]) -> str:
    """The "hints" line of the stat line, naming where the help came from.

    A revealed hint and a confessed one cost the same but are not the same
    thing, and the line that charges you for it should say which it was.
    """
    revealed = int(attempt.get("max_hint_tier") or 0)
    verdict = attempt.get("verdict") or ""
    if VERDICT_HELP_TIER.get(verdict, 0) > revealed:
        return VERDICT_HELP_LABELS.get(verdict, _hint_label(help_tier(attempt)))
    return _hint_label(revealed)


def is_clean_solve(attempt: Mapping[str, Any]) -> bool:
    """Solved with no help at all, and no failed submits — what you're training for.

    `help_tier` is what makes this strict: only `solved_unaided` (or a legacy
    `accepted`) with nothing revealed can reach tier 0.
    """
    return (
        attempt.get("verdict") in CLEAN_VERDICTS
        and help_tier(attempt) == 0
        and int(attempt.get("submissions") or 0) == 0
    )


def score_session(attempts: list[Mapping[str, Any]], weights: Weights | None = None) -> int:
    w = weights or load_weights()
    return sum(score_attempt(a, a.get("difficulty", "medium"), w).total for a in attempts)


# --- small formatting helpers, shared by every renderer --------------------


def fmt_duration(seconds: int | float | None) -> str:
    if seconds is None:
        return "--:--"
    seconds = int(seconds)
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def ordinal(n: int) -> str:
    if 10 <= n % 100 <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"
