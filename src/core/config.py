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

from . import branding, catalog, events, paths, problemtypes
from .catalog import ProblemSet

#: What the finish screen copies for LeetCode's AI to diagnose the solve. One
#: line, so it pastes as one message and sits in config.toml as a plain string.
DEFAULT_POST_SOLVE_PROMPT = (
    "What was the time and space complexity of my solution? "
    "How is the coding style of my solution? "
    "Did I reach the optimal solution? "
    "What would you name my solution? "
    "What other methods exist that I should know to solve this problem? "
    "What other suggestions do you have to make my solution more interview ready?"
)

DEFAULT_CONFIG_TOML = f"""\
# {branding.NAME} config

[session]
# default number of problems in a run — a default, not a floor; the setup
# screen accepts 1. Three because the spec's 30/week is explicitly a ceiling
# (§2.2) and three a day lands mid-range at daily cadence, and because a run
# with capture in it is ~30-45 min per problem: three is one sitting, and a
# run you finish is worth more than a run you abandon halfway.
planned_n = 3
# how many problems today's queue holds. Separate from `planned_n` because the
# two numbers answer different questions: this one is how much the scheduler
# thinks you owe it today, `planned_n` is how big a hand-picked run starts out.
# Left unset it follows `planned_n`, so splitting them is opt-in.
queue_n = 3
# how many of those slots reviews may take. One, so the other two go to
# problems you have never opened: due dates are priorities, not deadlines, and
# when several are due the queue spends the slot on the weakest and lets the
# rest wait. Raise it to work off a backlog faster, at the cost of new coverage.
reviews_per_day = 1
# which list runs are drawn from: neetcode150 | blind75 | any set named under
# [sets] below. The queue and the setup screen open on this one; `h` and `l`
# step across to the others without changing it.
active_list = "neetcode150"
# random | manual
selection = "random"

# Problem sets. Each one is a JSON list of problems and the *type* every problem
# in it is -- the type decides what the prompt after a solve asks, which screens
# follow it and how the answers move a review. `neetcode150` is bundled and is
# always there; add your own underneath:
#
#   [sets.system-design]
#   type = "system-design"            # a bundled type, or one in types/
#   path = "sets/system-design.json"  # relative to this file; ~ works too
#   queue_n = 1                       # optional: this set's own queue size
#   reviews_per_day = 1               # optional: and its own review budget
#
# Leave `path` out to use a list bundled with {branding.NAME}: `system-design`
# is one, a dozen prompts to get started on. A set is picked up the next time
# the app opens; `{branding.COMMAND} seed` does it now and says what it found.
#
# Types are TOML files of their own. `{branding.COMMAND} types` lists them, and
# `{branding.COMMAND} types --new <name>` writes one into types/ beside this
# file for you to edit.

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

[strategy]
# after every solve, name the patterns you reached for and the method you took
# through the problem. Both lists are yours: they start empty and fill with
# whatever you type into them. Marking a method optimal that is not the one you
# wrote is also what tells the scheduler that a suboptimal solve found the route
# late rather than missing it -- see docs/spaced-repetition.md. Set to false to
# skip both prompts entirely.
enabled = true

[scoring]
# which weights file in the package's data/scoring/ to compute scores with
weights = "v1"

[srs]
# which FSRS parameter file in the package's data/srs/ schedules reviews.
# `fsrs_cards` is a projection, so changing this and running
# `{branding.COMMAND} replay` re-derives every card and reschedules all history.
# v3 sets the entry intervals directly (2/5/9/21 days) and masters a problem once
# you have recalled it across its ladder; v1 and v2 stay selectable and unchanged.
params = "v3"

[stats]
# below this many samples in a slice, p99 is one data point and is greyed out
min_samples = 20
# default lookback window for the stats screen
window_days = 60

[audio]
# speech mode: while a problem is running, record what you say out loud. The
# recording pauses exactly when the clock does, so the file is your solve and
# nothing else. Nothing transcribes it and nothing scores it — the cadence is
# the artifact. Needs ffmpeg on PATH; without it a run just says so and carries
# on. This is the default; the setup screen can flip it for one run.
speech_mode = false
# mono Opus at constrained VBR, so this is a ceiling rather than an average.
# 24 kbps is 10.8 MB an hour (24000 / 8 = 3000 bytes a second); 12 is audibly
# compressed but still perfectly intelligible, 48 is headroom if you ever want
# to feed these to something that listens to them.
bitrate_kbps = 24
# how ffmpeg reaches the microphone. "pulse" / "default" is right on anything
# running pipewire-pulse; "alsa" / "hw:0" is the fallback on a machine without
# it. Not in the settings screen: these are facts about the machine, not knobs
# worth cycling between runs.
input_format = "pulse"
device = "default"

[cache]
# offline mode: `o` on the solve screen opens the cached copy of the problem
# instead of leetcode.com. Flip it when you board — nothing auto-detects,
# because captive-portal wifi lies about being a network.
offline = false
# ceiling on the on-disk problem cache. `{branding.COMMAND} fetch` walks the
# active list in priority order until this is spent; the whole neetcode150 is
# about 1.5 MB, so this is a runaway guard, not a budget you have to manage.
max_mb = 50

[ai]
# ctrl+y on the finish screen copies this to the clipboard, for pasting into
# LeetCode's AI assistant once you have submitted. It is sent as-is: the
# assistant already has the problem and your code open beside it.
post_solve_prompt = "{DEFAULT_POST_SOLVE_PROMPT}"
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
    # Not a language you solve in: what a problem type archives a write-up as.
    "markdown": "md",
}


@dataclass(frozen=True)
class SessionConfig:
    planned_n: int = 3
    queue_n: int = 3
    reviews_per_day: int = 1
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
class StrategyConfig:
    #: Ask, after every solve, which patterns you reached for and which method
    #: you took. Off, neither prompt appears and nothing is recorded -- old
    #: answers stay exactly where they are.
    enabled: bool = True


@dataclass(frozen=True)
class ScoringConfig:
    weights: str = "v1"


@dataclass(frozen=True)
class SrsConfig:
    params: str = "v3"


@dataclass(frozen=True)
class StatsConfig:
    min_samples: int = 20
    window_days: int = 60


@dataclass(frozen=True)
class AudioConfig:
    speech_mode: bool = False
    bitrate_kbps: int = 24
    input_format: str = "pulse"
    device: str = "default"


@dataclass(frozen=True)
class CacheConfig:
    offline: bool = False
    max_mb: int = 50

    @property
    def budget_bytes(self) -> int:
        return max(0, int(self.max_mb)) * 1024 * 1024


@dataclass(frozen=True)
class AiConfig:
    post_solve_prompt: str = DEFAULT_POST_SOLVE_PROMPT


#: Lists that live inside a bundled set rather than being one. `blind75` is a
#: mark on seventy-nine of `neetcode150`'s entries, so no `[sets]` table names
#: it and it is still a list you can run from.
BUNDLED_LISTS = (catalog.DEFAULT_LIST, "blind75")


@dataclass(frozen=True)
class Config:
    session: SessionConfig = field(default_factory=SessionConfig)
    capture: CaptureConfig = field(default_factory=CaptureConfig)
    strategy: StrategyConfig = field(default_factory=StrategyConfig)
    scoring: ScoringConfig = field(default_factory=ScoringConfig)
    srs: SrsConfig = field(default_factory=SrsConfig)
    stats: StatsConfig = field(default_factory=StatsConfig)
    audio: AudioConfig = field(default_factory=AudioConfig)
    cache: CacheConfig = field(default_factory=CacheConfig)
    ai: AiConfig = field(default_factory=AiConfig)
    #: Every problem set, bundled ones first, then yours in the order the file
    #: names them. The order is `catalog.sync`'s order of precedence.
    sets: tuple[ProblemSet, ...] = catalog.BUNDLED_SETS

    @property
    def lists(self) -> tuple[str, ...]:
        """Every list a run can be drawn from, in the order they are stepped through."""
        return list_names(self.sets)

    def set_named(self, name: str) -> ProblemSet | None:
        for problem_set in self.sets:
            if problem_set.name == name:
                return problem_set
        return None

    def type_of(self, name: str) -> str:
        """The type a list's problems are, as `config.toml` has it.

        A list that is not a set is LeetCode's. The only one a run can be drawn
        from is `blind75`, which is a mark on entries of the bundled set.
        """
        problem_set = self.set_named(name)
        return problem_set.type if problem_set else problemtypes.DEFAULT_TYPE

    def queue_n_for(self, name: str) -> int:
        """How many problems one list's queue holds: its own number, or the session's."""
        problem_set = self.set_named(name)
        if problem_set is not None and problem_set.queue_n is not None:
            return problem_set.queue_n
        return self.session.queue_n

    def reviews_per_day_for(self, name: str) -> int:
        problem_set = self.set_named(name)
        if problem_set is not None and problem_set.reviews_per_day is not None:
            return problem_set.reviews_per_day
        return self.session.reviews_per_day

    @property
    def active_lists(self) -> tuple[str, ...]:
        """Every list the offline cache covers: the active one, then the rest.

        A tuple rather than `session.active_list` spelled out at each call site
        because the cache is scoped by it, and now that a second list can exist
        this is where it shows up and nothing downstream had to change.

        Only lists whose type has somewhere to fetch from. A set of system
        design prompts has no statements to download, and listing it on the
        fetch screen would be promising a cache that cannot exist. The active
        list leads, so it is what a budget too small for everything keeps.
        """
        ordered = dict.fromkeys((self.session.active_list, *(s.name for s in self.sets)))
        return tuple(
            name for name in ordered if problemtypes.load(self.type_of(name)).fetch
        )


def _section(raw: dict[str, Any], name: str) -> dict[str, Any]:
    value = raw.get(name, {})
    return value if isinstance(value, dict) else {}


def _queue_n_follows_planned_n(raw: dict[str, Any]) -> dict[str, Any]:
    """An unset `queue_n` is not 3 — it is whatever `planned_n` came out as.

    The two numbers used to be one, so a config written before the split says
    only `planned_n`, and taking the dataclass default there would silently
    shrink a queue somebody had already sized. Setting `queue_n` once, in either
    layer, decouples them for good.
    """
    session = _section(raw, "session")
    if "queue_n" in session:
        return raw
    follows = session.get("planned_n", SessionConfig.planned_n)
    return {**raw, "session": {**session, "queue_n": follows}}


def _count(raw: Any, floor: int) -> int | None:
    """A per-set number, or None to follow the session's. Never a bool."""
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < floor:
        return None
    return raw


def _sets(raw: dict[str, Any]) -> tuple[ProblemSet, ...]:
    """The `[sets]` table, laid over the sets that are always there.

    A set named in the file replaces a bundled one of the same name, in place --
    which is how you point `neetcode150` at a copy you have corrected -- and
    anything else is added after, in the order the file names it.

    A table that makes no sense is skipped rather than raised on. The rule at
    the top of `_read_file` covers this too: a broken config must never stop a
    run, and what was wrong with a set is `doctor`'s to say.
    """
    sets = {s.name: s for s in catalog.BUNDLED_SETS}
    for name, body in _section(raw, "sets").items():
        if not isinstance(body, dict):
            continue
        name = problemtypes.normalise(name)
        if not name:
            continue
        path = body.get("path")
        sets[name] = ProblemSet(
            name=name,
            type=problemtypes.normalise(body.get("type")) or problemtypes.DEFAULT_TYPE,
            path=path if isinstance(path, str) and path.strip() else None,
            queue_n=_count(body.get("queue_n"), 1),
            reviews_per_day=_count(body.get("reviews_per_day"), 0),
        )
    return tuple(sets.values())


def list_names(sets: tuple[ProblemSet, ...] | None = None) -> tuple[str, ...]:
    """Every list there is to run from: the bundled ones, then each set.

    Reads the file when not handed the sets, because `options` is asked for the
    choices without a `Config` in hand.
    """
    if sets is None:
        sets = _sets(_read_file())
    return tuple(dict.fromkeys((*BUNDLED_LISTS, *(s.name for s in sets))))


def _build(raw: dict[str, Any]) -> Config:
    raw = _queue_n_follows_planned_n(raw)

    def pick(cls, name: str):
        allowed = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in _section(raw, name).items() if k in allowed})

    return Config(
        session=pick(SessionConfig, "session"),
        capture=pick(CaptureConfig, "capture"),
        strategy=pick(StrategyConfig, "strategy"),
        scoring=pick(ScoringConfig, "scoring"),
        srs=pick(SrsConfig, "srs"),
        stats=pick(StatsConfig, "stats"),
        audio=pick(AudioConfig, "audio"),
        cache=pick(CacheConfig, "cache"),
        ai=pick(AiConfig, "ai"),
        sets=_sets(raw),
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
    #: Empty means free text: there is nothing to step through, so the
    #: settings screen opens an editor for it instead.
    choices: tuple[Any, ...]

    @property
    def free_text(self) -> bool:
        return not self.choices

    def accepts(self, value: Any) -> bool:
        if self.free_text:
            return isinstance(value, str) and bool(value.strip())
        return value in self.choices

    @property
    def section(self) -> str:
        return self.key.partition(".")[0]

    @property
    def name(self) -> str:
        return self.key.partition(".")[2]

    def render(self, value: Any) -> str:
        if isinstance(value, bool):
            return "on" if value else "off"
        text = " ".join(str(value).split())
        # The value column is 16 wide; a prompt would push "set here" off the row.
        return text if len(text) <= 15 else text[:14] + "…"

    def step(self, value: Any, delta: int) -> Any:
        """The next choice along, wrapping. Unknown values land on the first."""
        if self.free_text:
            return value
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
            "problems per new run",
            "what the setup screen rolls by default — a starting point, not a floor",
            tuple(range(1, 11)),
        ),
        # Beside `planned_n` rather than on the end: they are the same question
        # asked of two screens, and a settings list that separates them invites
        # exactly the confusion the split exists to end.
        Option(
            "session.queue_n",
            "problems in the queue",
            "how many the scheduler puts on today's queue — unset, it follows the number above",
            tuple(range(1, 11)),
        ),
        Option(
            "session.reviews_per_day",
            "reviews per day",
            "how many queue slots go to problems you have already seen — the rest are new",
            tuple(range(0, 6)),
        ),
        Option(
            "session.active_list",
            "problem list",
            "which list the queue and the setup screen open on — h and l step across there",
            list_names(),
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
        # New options go on the end unless they belong beside an existing one.
        # The settings screen is an OptionList and its tests navigate it by row
        # index, so an insert silently retargets every row below it: check
        # `test_settings_opens_from_home_and_toggles_wrong_answer_capture`,
        # `test_settings_values_move_sideways_and_come_back` and
        # `test_new_options_go_on_the_end` before moving anything.
        Option(
            "audio.speech_mode",
            "speech mode",
            "record what you say while a problem runs — the setup screen can override it per run",
            (False, True),
        ),
        Option(
            "audio.bitrate_kbps",
            "recording quality",
            "mono Opus, as a ceiling — 24 kbps is 10.8 MB an hour, 12 is smaller and still clear",
            (12, 16, 24, 32, 48),
        ),
        Option(
            "strategy.enabled",
            "name what you did",
            "after a solve, name the patterns you used and the method you took",
            (True, False),
        ),
        Option(
            "ai.post_solve_prompt",
            "post-solve prompt",
            "what ctrl+y on the finish screen copies, for LeetCode's AI to review the solve",
            (),
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
        if option.accepts(value):
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
