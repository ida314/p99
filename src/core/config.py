"""User config (`~/.config/<slug>/config.toml`, see `paths`).

Phase 1 only needs a handful of knobs. Everything has a default, so a missing
config file is not an error — it is written on first run for discoverability.

Two layers, in this order:

  1. `config.toml` — hand-edited, comments and all.
  2. the `settings` table — what the in-app settings screen writes, as
     `settings_changed` events keyed by the dotted names in `options()`.

The file stays the base layer rather than being rewritten, because a settings
screen that regenerates TOML would eat the comments explaining every knob. An
override is undone by clearing it (`clear_option`), which drops back to
whatever the file says — so the two layers never have to be reconciled.
"""

from __future__ import annotations

import json
import os
import sqlite3
import tomllib
from dataclasses import dataclass, field
from typing import Any

from . import branding, events, paths

DEFAULT_CONFIG_TOML = f"""\
# {branding.NAME} config

[session]
# default number of problems in a run — a default, not a floor; the setup
# screen accepts 1. Three because the spec's 30/week is explicitly a ceiling
# (§2.2) and three a day lands mid-range at daily cadence, and because a run
# with capture in it is ~30-45 min per problem: three is one sitting, and a
# run you finish is worth more than a run you abandon halfway.
planned_n = 3
# which catalog list is active: neetcode150 | blind75
active_list = "neetcode150"
# random | manual
selection = "random"

[capture]
# language solutions are archived as; drives the temp-file extension so your
# editor's syntax highlighting and LSP work during the $EDITOR handoff.
language = "python"
# set to false to skip the solution/reflection editor steps entirely
enabled = true
# archive the code behind a failed submit too: pressing `s` on the solve
# screen opens $EDITOR so you can paste what you just got rejected. Skippable
# with :q! like every other capture step. Set to false to log the failed
# submit and nothing else.
on_failed_submit = true

[scoring]
# which weights file in the package's data/scoring/ to compute scores with
weights = "v1"

[srs]
# which FSRS parameter file in the package's data/srs/ schedules reviews.
# `fsrs_cards` is a projection, so changing this and running
# `{branding.COMMAND} replay` re-derives every card and reschedules all history.
params = "v1"

[stats]
# below this many samples in a slice, p99 is one data point and is greyed out
min_samples = 20
# default lookback window for the stats screen
window_days = 60

[cache]
# offline mode: `o` on the solve screen opens the cached copy of the problem
# instead of leetcode.com. Flip it when you board — nothing auto-detects,
# because captive-portal wifi lies about being a network.
offline = false
# ceiling on the on-disk problem cache. `{branding.COMMAND} fetch` walks the
# active list in priority order until this is spent; the whole neetcode150 is
# about 1.5 MB, so this is a runaway guard, not a budget you have to manage.
max_mb = 50
"""

EXT_BY_LANGUAGE = {
    "python": "py",
    "go": "go",
    "rust": "rs",
    "c": "c",
    "cpp": "cpp",
    "c++": "cpp",
    "java": "java",
    "javascript": "js",
    "typescript": "ts",
    "ruby": "rb",
    "kotlin": "kt",
    "swift": "swift",
    "scala": "scala",
    "csharp": "cs",
    "sql": "sql",
}


@dataclass(frozen=True)
class SessionConfig:
    planned_n: int = 3
    active_list: str = "neetcode150"
    selection: str = "random"


@dataclass(frozen=True)
class CaptureConfig:
    language: str = "python"
    enabled: bool = True
    on_failed_submit: bool = True

    @property
    def ext(self) -> str:
        return EXT_BY_LANGUAGE.get(self.language.lower(), "txt")


@dataclass(frozen=True)
class ScoringConfig:
    weights: str = "v1"


@dataclass(frozen=True)
class SrsConfig:
    params: str = "v1"


@dataclass(frozen=True)
class StatsConfig:
    min_samples: int = 20
    window_days: int = 60


@dataclass(frozen=True)
class CacheConfig:
    offline: bool = False
    max_mb: int = 50

    @property
    def budget_bytes(self) -> int:
        return max(0, int(self.max_mb)) * 1024 * 1024


@dataclass(frozen=True)
class Config:
    session: SessionConfig = field(default_factory=SessionConfig)
    capture: CaptureConfig = field(default_factory=CaptureConfig)
    scoring: ScoringConfig = field(default_factory=ScoringConfig)
    srs: SrsConfig = field(default_factory=SrsConfig)
    stats: StatsConfig = field(default_factory=StatsConfig)
    cache: CacheConfig = field(default_factory=CacheConfig)

    @property
    def active_lists(self) -> tuple[str, ...]:
        """Every catalog list in play, for the things that span more than one.

        One today. It is a tuple rather than `session.active_list` spelled out
        at each call site because the offline cache is scoped by it: when a
        second list arrives, this property changes and nothing downstream does.
        """
        return (self.session.active_list,)


def _section(raw: dict[str, Any], name: str) -> dict[str, Any]:
    value = raw.get(name, {})
    return value if isinstance(value, dict) else {}


def _build(raw: dict[str, Any]) -> Config:
    def pick(cls, name: str):
        allowed = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in _section(raw, name).items() if k in allowed})

    return Config(
        session=pick(SessionConfig, "session"),
        capture=pick(CaptureConfig, "capture"),
        scoring=pick(ScoringConfig, "scoring"),
        srs=pick(SrsConfig, "srs"),
        stats=pick(StatsConfig, "stats"),
        cache=pick(CacheConfig, "cache"),
    )


def write_default_config() -> None:
    path = paths.config_file()
    if path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(DEFAULT_CONFIG_TOML)


def _read_file() -> dict[str, Any]:
    path = paths.config_file()
    if not path.exists():
        return {}
    try:
        return tomllib.loads(path.read_text())
    except (tomllib.TOMLDecodeError, OSError):
        # A broken config must never stop a run. Defaults are always valid.
        return {}


def load(conn: sqlite3.Connection | None = None) -> Config:
    """The file, with the settings screen's overrides on top when given a `conn`."""
    raw = _read_file()
    if conn is not None:
        for key, value in overrides(conn).items():
            section, _, name = key.partition(".")
            current = raw.get(section)
            raw[section] = {**current, name: value} if isinstance(current, dict) else {name: value}
    return _build(raw)


# --- settings, the second layer --------------------------------------------


@dataclass(frozen=True)
class Option:
    """One settable knob: where it lives, what it may be, how to say it."""

    key: str  # dotted: "capture.on_failed_submit"
    label: str
    help: str
    choices: tuple[Any, ...]

    @property
    def section(self) -> str:
        return self.key.partition(".")[0]

    @property
    def name(self) -> str:
        return self.key.partition(".")[2]

    def render(self, value: Any) -> str:
        if isinstance(value, bool):
            return "on" if value else "off"
        return str(value)

    def step(self, value: Any, delta: int) -> Any:
        """The next choice along, wrapping. Unknown values land on the first."""
        try:
            index = self.choices.index(value)
        except ValueError:
            return self.choices[0]
        return self.choices[(index + delta) % len(self.choices)]


def options() -> tuple[Option, ...]:
    """The knobs the settings screen offers, in display order.

    Deliberately not every field on `Config`: this is the set worth changing
    between runs. Anything else stays a config-file edit.
    """
    # Local: both read package data at call time, and `srs` imports `scoring`.
    from . import scoring, srs

    return (
        Option(
            "capture.enabled",
            "capture",
            "the two $EDITOR steps after every problem — archive the solution, write the note",
            (True, False),
        ),
        Option(
            "capture.on_failed_submit",
            "capture wrong answers",
            "`s` also opens $EDITOR so you can paste the code that just got rejected",
            (True, False),
        ),
        Option(
            "capture.language",
            "language",
            "extension the archived code is written as, so your editor knows what it is",
            tuple(sorted(EXT_BY_LANGUAGE)),
        ),
        Option(
            "session.planned_n",
            "problems per run",
            "what the setup screen rolls by default — a starting point, not a floor",
            tuple(range(1, 11)),
        ),
        Option(
            "session.active_list",
            "problem list",
            "which catalog list runs are drawn from",
            ("neetcode150", "blind75"),
        ),
        Option(
            "scoring.weights",
            "scoring weights",
            "which weights file scores are computed with — history rescores itself",
            tuple(scoring.available_weights()),
        ),
        Option(
            "srs.params",
            "review schedule",
            "which FSRS parameter file schedules reviews — replay reschedules everything",
            tuple(srs.available_params()),
        ),
        Option(
            "cache.offline",
            "offline",
            "`o` opens the cached copy of the problem instead of leetcode.com — for planes",
            (False, True),
        ),
    )


def value_of(cfg: Config, option: Option) -> Any:
    return getattr(getattr(cfg, option.section), option.name)


def overrides(conn: sqlite3.Connection) -> dict[str, Any]:
    """The settings-table layer, filtered to keys this version still knows.

    A row for a retired key, or a value no longer on offer, is ignored rather
    than loaded: the settings table outlives any one version of `options()`.
    """
    known = {o.key: o for o in options()}
    out: dict[str, Any] = {}
    try:
        rows = conn.execute("SELECT key, value FROM settings").fetchall()
    except sqlite3.Error:
        return out
    for row in rows:
        option = known.get(row["key"])
        if option is None:
            continue
        try:
            value = json.loads(row["value"])
        except (TypeError, ValueError):
            continue
        if value in option.choices:
            out[option.key] = value
    return out


def set_option(conn: sqlite3.Connection, key: str, value: Any) -> None:
    """Override one setting. Appends to the log like everything else."""
    events.append(conn, events.SETTINGS_CHANGED, {"key": key, "value": value})


def clear_option(conn: sqlite3.Connection, key: str) -> None:
    """Drop the override, falling back to `config.toml`."""
    events.append(conn, events.SETTINGS_CHANGED, {"key": key, "value": None})


ENV_EDITOR = branding.env("EDITOR")


def editor() -> list[str]:
    """The $EDITOR handoff command (spec §7)."""
    raw = os.environ.get(ENV_EDITOR) or os.environ.get("VISUAL") or os.environ.get("EDITOR") or "vi"
    return raw.split()
