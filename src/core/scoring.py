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

# Verdicts that mean "the problem is solved".
CLEAN_VERDICTS = frozenset({"accepted"})
VERDICTS = ("accepted", "wrong_answer", "tle", "gave_up")
VERDICT_LABELS = {
    "accepted": "ACCEPTED",
    "wrong_answer": "WRONG ANSWER",
    "tle": "TIME LIMIT EXCEEDED",
    "gave_up": "GAVE UP",
}


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
    tier = int(attempt.get("max_hint_tier") or 0)
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
    if verdict == "gave_up":
        return Score(
            total=0,
            components=(
                time_line,
                Component("hints", _hint_label(tier), 0),
                Component("verdict", VERDICT_LABELS["gave_up"], 0),
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
        total = -submit_pen
        components = [
            time_line,
            Component("hints", _hint_label(tier), 0),
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
        Component("verdict", VERDICT_LABELS["accepted"], round(base)),
        Component(
            "time",
            f"{fmt_duration(active)}   (par {fmt_duration(par)})",
            round(after_time) - round(base),
            ratio=_clamp(1 - (active / par if par else 0), 0.0, 1.0),
        ),
        Component("hints", _hint_label(tier), round(after_hint) - round(after_time)),
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


def _hint_label(tier: int) -> str:
    if not tier:
        return "none"
    return f"tier {tier}  ({HINT_TIER_NAMES[min(tier, 4)]})"


def is_clean_solve(attempt: Mapping[str, Any]) -> bool:
    """Accepted, no hints, no failed submits — the thing you're training for."""
    return (
        attempt.get("verdict") in CLEAN_VERDICTS
        and int(attempt.get("max_hint_tier") or 0) == 0
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
