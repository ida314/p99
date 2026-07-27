"""User config (`~/.config/<slug>/config.toml`, see `paths`).

Phase 1 only needs a handful of knobs. Everything has a default, so a missing
config file is not an error — it is written on first run for discoverability.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from typing import Any

from . import branding, paths

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

[scoring]
# which weights file in the package's data/scoring/ to compute scores with
weights = "v1"

[stats]
# below this many samples in a slice, p99 is one data point and is greyed out
min_samples = 20
# default lookback window for the stats screen
window_days = 60
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

    @property
    def ext(self) -> str:
        return EXT_BY_LANGUAGE.get(self.language.lower(), "txt")


@dataclass(frozen=True)
class ScoringConfig:
    weights: str = "v1"


@dataclass(frozen=True)
class StatsConfig:
    min_samples: int = 20
    window_days: int = 60


@dataclass(frozen=True)
class Config:
    session: SessionConfig = field(default_factory=SessionConfig)
    capture: CaptureConfig = field(default_factory=CaptureConfig)
    scoring: ScoringConfig = field(default_factory=ScoringConfig)
    stats: StatsConfig = field(default_factory=StatsConfig)


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
        stats=pick(StatsConfig, "stats"),
    )


def write_default_config() -> None:
    path = paths.config_file()
    if path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(DEFAULT_CONFIG_TOML)


def load() -> Config:
    path = paths.config_file()
    if not path.exists():
        return Config()
    try:
        raw = tomllib.loads(path.read_text())
    except (tomllib.TOMLDecodeError, OSError):
        # A broken config must never stop a run. Defaults are always valid.
        return Config()
    return _build(raw)


ENV_EDITOR = branding.env("EDITOR")


def editor() -> list[str]:
    """The $EDITOR handoff command (spec §7)."""
    raw = os.environ.get(ENV_EDITOR) or os.environ.get("VISUAL") or os.environ.get("EDITOR") or "vi"
    return raw.split()
