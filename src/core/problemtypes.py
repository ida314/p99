"""Problem types: what kind of thing a problem is, and what finishing one asks.

A LeetCode problem and a system design prompt are both things you sit down to,
time, finish and come back to on a schedule. What differs is what is worth
asking afterwards. One has a time complexity and a judge; the other has
requirements you either covered or did not. A type is that difference written
down: the fields the verdict prompt asks, the screens that follow it, how the
answers move the review, and what par is.

Everything else is shared, and that is the point of having types at all. There
is one event log, one `attempts` table, one `fsrs_cards` table and one rating
map. A new type is a TOML file, not a second scheduler.

Two places a type can live, and the second wins:

  1. `data/types/<name>.toml` in the package -- the bundled ones.
  2. `types/<name>.toml` in your config directory -- yours.

Read at the moment it is needed and never denormalised into the log, which is
the bargain `scoring` and `srs` already make with their own files: `attempts`
holds the answers you gave, the type says what they mean, and editing the type
and replaying regrades every card under the new rules.

A broken type must never stop a run. A file that will not parse falls back to
the bundled type of the same name, or to a bare one that asks nothing; a field
that makes no sense is dropped and the rest of the file stands. What was wrong
is kept on `ProblemType.problems` for `doctor` to say out loud.

Named for what it holds rather than `types`, which is the standard library's --
the same reason `queues` is plural.

Nothing here writes, and nothing here reads the database.
"""

from __future__ import annotations

import dataclasses
import functools
import json
import re
import tomllib
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any, Mapping

from . import paths

# A module object rather than a dotted name, for the reason given in `catalog`.
from .data import types as _types_pkg

#: What a problem is when nothing says otherwise. Every problem in a database
#: that predates types is one of these, which is what the column defaults to.
DEFAULT_TYPE = "leetcode"

# --- field kinds -----------------------------------------------------------

#: One line of free text. Stored as typed, trimmed.
TEXT = "text"
#: A number, clamped to `min`/`max` when the field sets them. Stored as a float.
NUMBER = "number"
#: A share of a total that belongs to the problem: "7/10", "70%", "0.7". Stored
#: as typed and measured on read, so a fix to `fraction` below corrects every
#: answer already in the log.
FRACTION = "fraction"
#: One answer off a short ladder. Stored as the option's `value`.
CHOICE = "choice"

KINDS = (TEXT, NUMBER, FRACTION, CHOICE)

#: Fields that have a column of their own on `attempts`, and ride the top level
#: of a `problem_finished` payload rather than its `answers` block.
#:
#: They are LeetCode's, and they are here because they predate types: the log
#: already holds events carrying them, and `render.approach_rows` knows how to
#: draw them as one line per axis. A type of your own may reuse a
#: key from this list and gets the column and the rendering with it. Any other
#: key lands in `attempts.answers`, which is where a new field belongs.
COLUMN_KEYS = (
    "claimed_complexity",
    "claimed_space_complexity",
    "time_optimality",
    "space_optimality",
    "code_style",
    "lc_runtime_pct",
    "lc_memory_pct",
)

#: Keys a field may not take. Most are columns the engine fills in itself, and a
#: field called `verdict` would be an answer competing with a measurement. The
#: last few are widget ids on the verdict prompt, which a field's own id -- its
#: key, hyphenated -- must not collide with.
RESERVED_KEYS = frozenset(
    {
        "id", "uuid", "session_id", "slug", "started_at", "ended_at",
        "active_seconds", "wall_seconds", "paused_seconds", "verdict",
        "max_hint_tier", "submissions", "self_confidence", "code_path",
        "language", "note_path", "audio_path", "confirmed_complexity",
        "optimality", "is_review", "suspended_seconds", "suspends", "answers",
        "type", "title", "difficulty", "tags", "pattern", "lists", "url",
        "strategies", "methods", "n", "resolves", "saw_better",
        "confidence", "save", "discard", "cancel", "copy_prompt",
    }
)

#: How many fields share a row of the verdict prompt before it wraps. Three,
#: because a 74-wide box leaves each of three ladders fourteen characters for
#: its answer and a fourth would leave nine.
MAX_PER_ROW = 3

#: What a ladder's answer may be worded in before the form cuts it off.
MAX_OPTION_LABEL = 14

#: The stat line's label column is twelve wide and its detail column twenty-six.
MAX_STAT_LABEL = 11
MAX_STAT_DETAIL = 26

DIFFICULTIES = ("easy", "medium", "hard")

_KEY = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
_SEPARATORS = re.compile(r"[^a-z0-9]+")

DEFAULT_SOLVE_PROMPT = "the clock is running — f when you are done"
DEFAULT_SOLUTION_PROMPT = "paste your solution below."


def normalise(name: str | None) -> str:
    """A type's name as a file is named: lowercased, punctuation to hyphens."""
    return _SEPARATORS.sub("-", str(name or "").strip().lower()).strip("-")


# --- reading an answer -----------------------------------------------------

_SHARE = re.compile(
    r"^\s*(\d+(?:\.\d+)?)\s*(?:/|of|out of)\s*(\d+(?:\.\d+)?)\s*$", re.IGNORECASE
)
_PERCENT = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*%\s*$")


def fraction(value: Any) -> float | None:
    """What a typed share comes to, 0..1. None if it cannot be read as one.

    "7/10" and "7 of 10" are seven tenths, "70%" is the same, and a bare number
    is taken as the share itself when it lies in 0..1. A bare "7" is not read at
    all: seven of how many is the half of the answer that was left out, and
    guessing it would turn a typo into a grade.

    More than the total is the total. "11/10" is somebody counting a
    requirement the reference did not list, which is not a reason to fail them.
    """
    if value is None:
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value) if 0 <= float(value) <= 1 else None
    text = str(value)
    share = _SHARE.match(text)
    if share:
        total = float(share.group(2))
        return min(1.0, float(share.group(1)) / total) if total > 0 else None
    percent = _PERCENT.match(text)
    if percent:
        return min(1.0, float(percent.group(1)) / 100)
    try:
        bare = float(text.strip())
    except ValueError:
        return None
    return bare if 0 <= bare <= 1 else None


def answered(value: Any) -> bool:
    """Was anything said. An empty string is not an answer, and neither is None."""
    return value is not None and str(value).strip() != ""


def answers_of(attempt: Mapping[str, Any]) -> dict[str, Any]:
    """The `answers` block of an attempt, whichever form it arrived in.

    A row straight off `attempts` carries it as the JSON it is stored as; one
    that came through `stats.load_attempts` carries it decoded. Anything else is
    no answers at all, not an error -- every attempt logged before types has
    none.
    """
    raw = attempt.get("answers")
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw:
        try:
            decoded = json.loads(raw)
        except ValueError:
            return {}
        return decoded if isinstance(decoded, dict) else {}
    return {}


def answer(attempt: Mapping[str, Any], key: str) -> Any:
    """What one attempt answered to one field, wherever it is kept.

    The top level first, which is where a column lives and where a plain dict
    in a test puts everything; then the `answers` block.
    """
    value = attempt.get(key)
    if value is not None:
        return value
    return answers_of(attempt).get(key)


def split(answers: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """A form's answers, as `(columns, the rest)`.

    The one place that decides which answers ride the top level of a payload
    and which go in its `answers` block, so the engine and the screen cannot
    disagree about it. Unanswered fields are dropped from the second half: the
    block is omitted entirely when nothing is in it, and an old event and a
    skipped prompt are then the same shape.
    """
    columns = {k: answers.get(k) for k in COLUMN_KEYS if k in answers}
    rest = {
        k: v
        for k, v in answers.items()
        if k not in COLUMN_KEYS and k not in RESERVED_KEYS and v is not None
    }
    return columns, rest


# --- the shape of a type ---------------------------------------------------


@dataclass(frozen=True)
class Choice:
    """One rung of a ladder: what is stored, and the two ways it is worded."""

    value: str
    label: str
    #: The stat line's wording, which has the room the form does not. Empty
    #: means the form's own word will do.
    report: str = ""


@dataclass(frozen=True)
class Review:
    """How one field's answer moves the review. Empty moves nothing.

    Three grades can be reached from here and each has its own direction.
    `again` and `hard` are floors an answer can lower the grade to; `easy` is a
    requirement the answer has to meet before the one promotion is allowed. An
    answer nobody gave fires nothing and satisfies nothing: an unanswered
    question is not an answer, and the rating map has never read one as such.
    """

    #: Ladder answers that fail the attempt outright, or demote it to Hard.
    again_on: tuple[str, ...] = ()
    hard_on: tuple[str, ...] = ()
    #: Ladder answers an Easy grade requires one of.
    easy_on: tuple[str, ...] = ()
    #: The same three, for an answer that measures: below this it fails, below
    #: this it is Hard, and Easy needs at least this.
    again_below: float | None = None
    hard_below: float | None = None
    easy_from: float | None = None
    #: Easy requires that the field was answered at all.
    easy_needs_answer: bool = False
    #: The Hard demotion is waived when the methods page holds an optimal method
    #: that is not the one you wrote: the gap you diagnosed is not the gap you
    #: missed. See `srs.rate`.
    unless_better_known: bool = False

    @property
    def moves_anything(self) -> bool:
        return self != Review()


@dataclass(frozen=True)
class Field:
    """One question on the verdict prompt."""

    key: str
    kind: str
    #: What the stat line calls it, and what sits above a ladder on the form.
    label: str = ""
    #: The line the field sits under. Fields that share one share a row.
    group: str = ""
    placeholder: str = ""
    options: tuple[Choice, ...] = ()
    #: The option the cursor starts on, by value.
    default: str | None = None
    minimum: float | None = None
    maximum: float | None = None
    review: Review = Review()

    @property
    def widget_id(self) -> str:
        """The field's id on the verdict prompt: its key, hyphenated."""
        return self.key.replace("_", "-")

    @property
    def values(self) -> tuple[str, ...]:
        return tuple(option.value for option in self.options)

    @property
    def default_index(self) -> int:
        """Where a ladder's cursor starts: on `default`, or on the last rung.

        The last and not the first, because the first rung of a ladder written
        best-to-worst is the flattering one, and a default of flattering is how
        a month of answers you never gave quietly claims to have gone well.
        """
        if self.default in self.values:
            return self.values.index(self.default)
        return max(0, len(self.options) - 1)

    def clean(self, raw: Any) -> Any:
        """What is stored for what was typed. None for nothing.

        A number is clamped rather than refused, and one that is not a number is
        dropped: the verdict prompt sits between you and the next problem, and
        it has never once stopped a save to argue about a field.
        """
        if self.kind == CHOICE:
            return raw if raw in self.values else None
        text = str(raw if raw is not None else "").strip()
        if not text:
            return None
        if self.kind != NUMBER:
            return text
        try:
            number = float(text)
        except ValueError:
            return None
        if self.minimum is not None:
            number = max(self.minimum, number)
        if self.maximum is not None:
            number = min(self.maximum, number)
        return number

    def measure(self, value: Any) -> float | None:
        """The answer as a number the thresholds can be held against."""
        if value is None:
            return None
        if self.kind == FRACTION:
            return fraction(value)
        if self.kind == NUMBER:
            try:
                return float(value)
            except (TypeError, ValueError):
                return None
        return None

    def _below(self, value: Any, threshold: float | None) -> bool:
        measured = self.measure(value)
        return threshold is not None and measured is not None and measured < threshold

    def fails(self, value: Any) -> bool:
        """Does this answer, on its own, grade the attempt Again."""
        if self.kind == CHOICE:
            return value is not None and value in self.review.again_on
        return self._below(value, self.review.again_below)

    def struggles(self, value: Any, *, better_known: bool = False) -> bool:
        """Does this answer demote the attempt to Hard."""
        if self.kind == CHOICE:
            fired = value is not None and value in self.review.hard_on
        else:
            fired = self._below(value, self.review.hard_below)
        if fired and self.review.unless_better_known and better_known:
            return False
        return fired

    def earns_easy(self, value: Any) -> bool:
        """Does this answer leave an Easy grade on the table.

        True for a field that asks nothing of Easy, which is most of them.
        """
        rule = self.review
        if rule.easy_needs_answer and not answered(value):
            return False
        if rule.easy_on and value not in rule.easy_on:
            return False
        if rule.easy_from is not None:
            measured = self.measure(value)
            if measured is None or measured < rule.easy_from:
                return False
        return True

    def report(self, value: Any) -> str:
        """The answer as the stat line says it. "" for nothing to say."""
        if not answered(value):
            return ""
        if self.kind == CHOICE:
            for option in self.options:
                if option.value == value:
                    return option.report or option.label
            return str(value)
        if self.kind == NUMBER:
            try:
                return f"{float(value):g}"
            except (TypeError, ValueError):
                return str(value)
        if self.kind == FRACTION:
            text = " ".join(str(value).split())
            measured = fraction(value)
            # The share you typed and what it comes to, unless you typed the
            # percentage yourself and the second half would only repeat it.
            if measured is None or "%" in text:
                return text
            return f"{text}  ·  {measured * 100:.0f}%"
        return " ".join(str(value).split())


@dataclass(frozen=True)
class Group:
    """One row of the verdict prompt: a line of text and the fields under it."""

    label: str
    fields: tuple[Field, ...]
    #: False for the second row of a group that was too wide for one, which sits
    #: under the first without saying the label again.
    labelled: bool = True

    @property
    def ladders(self) -> tuple[Field, ...]:
        return tuple(f for f in self.fields if f.kind == CHOICE)


@dataclass(frozen=True)
class Screens:
    """Which prompts follow the verdict. Every one is on until a type says not."""

    patterns: bool = True
    methods: bool = True
    solution: bool = True
    note: bool = True
    again: bool = True


@dataclass(frozen=True)
class ProblemType:
    name: str
    label: str = ""
    #: The rungs of the help ladder the verdict prompt offers, in radio order.
    verdicts: tuple[str, ...] = ()
    #: There is something to submit to. See `leetcode.toml`.
    judge: bool = False
    #: Which fetcher can download this type's statements. "" for none.
    fetch: str = ""
    solve_prompt: str = DEFAULT_SOLVE_PROMPT
    screens: Screens = Screens()
    #: The language the solution is archived as. "" follows `[capture] language`.
    language: str = ""
    solution_prompt: str = DEFAULT_SOLUTION_PROMPT
    #: The reflection buffer. "" is the default three questions.
    note_template: str = ""
    ai_copy: bool = False
    #: "" follows `[ai] post_solve_prompt` in config.toml.
    ai_prompt: str = ""
    ai_paste_into: str = ""
    #: The group whose row the copy button joins. "" gives it a row of its own.
    ai_group: str = ""
    fields: tuple[Field, ...] = ()
    #: Per-difficulty overrides of the scoring weights, as pairs rather than
    #: mappings for the reason `srs.Params.retention` is: the type is handed
    #: around as a value and has to stay hashable.
    par_seconds: tuple[tuple[str, int], ...] = ()
    base: tuple[tuple[str, float], ...] = ()
    #: Where it was read from: "bundled", a path, or "missing".
    source: str = "bundled"
    #: What was wrong with the file, in words. Empty for a type that is fine.
    #: Left out of comparisons: two readings of one type are the same type
    #: whatever was said about the file on the way in.
    problems: tuple[str, ...] = dataclasses.field(default=(), compare=False)

    @property
    def title(self) -> str:
        return self.label or self.name

    @property
    def missing(self) -> bool:
        """No file of this name exists anywhere. The type is the bare fallback."""
        return self.source == "missing"

    def field(self, key: str) -> Field | None:
        for candidate in self.fields:
            if candidate.key == key:
                return candidate
        return None

    def has(self, key: str) -> bool:
        return self.field(key) is not None

    def groups(self) -> tuple[Group, ...]:
        """The fields as rows, in the order they are asked.

        Consecutive fields that name the same `group` share a row. A field that
        names none gets a row of its own under its own label, and a row that
        would run past `MAX_PER_ROW` wraps.
        """
        runs: list[tuple[str, list[Field]]] = []
        for entry in self.fields:
            label = entry.group or entry.label or entry.key.replace("_", " ")
            if entry.group and runs and runs[-1][0] == label:
                runs[-1][1].append(entry)
            else:
                runs.append((label, [entry]))

        rows: list[Group] = []
        for label, members in runs:
            for start in range(0, len(members), MAX_PER_ROW):
                rows.append(
                    Group(
                        label=label,
                        fields=tuple(members[start : start + MAX_PER_ROW]),
                        labelled=start == 0,
                    )
                )
        return tuple(rows)


# --- parsing ---------------------------------------------------------------


def _text(raw: Any, default: str = "") -> str:
    return " ".join(raw.split()) if isinstance(raw, str) else default


def _flag(raw: Mapping[str, Any], key: str, default: bool) -> bool:
    value = raw.get(key, default)
    return value if isinstance(value, bool) else default


def _table(raw: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    value = raw.get(key, {})
    return value if isinstance(value, dict) else {}


def _number(raw: Any) -> float | None:
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    return float(raw)


def _values(raw: Any) -> tuple[str, ...]:
    if not isinstance(raw, list):
        return ()
    return tuple(str(v) for v in raw if isinstance(v, str))


def _review(raw: Any, kind: str, key: str, options: tuple[str, ...], said: list[str]) -> Review:
    if raw is None:
        return Review()
    if not isinstance(raw, dict):
        said.append(f"field `{key}`: `review` is not a table, so it moves nothing")
        return Review()

    known = {
        "again_on", "hard_on", "easy_on", "again_below", "hard_below",
        "easy_from", "easy_needs_answer", "unless_better_known",
    }
    for stray in sorted(set(raw) - known):
        said.append(f"field `{key}`: `review.{stray}` is not a rule this version knows")

    ladders = {name: _values(raw.get(name)) for name in ("again_on", "hard_on", "easy_on")}
    thresholds = {
        name: _number(raw.get(name)) for name in ("again_below", "hard_below", "easy_from")
    }
    if kind == CHOICE:
        for name, values in ladders.items():
            for value in values:
                if value not in options:
                    said.append(
                        f"field `{key}`: `review.{name}` names `{value}`, "
                        "which is not one of its options"
                    )
        if any(v is not None for v in thresholds.values()):
            said.append(f"field `{key}`: a ladder has no number to hold a threshold against")
            thresholds = dict.fromkeys(thresholds)
    else:
        if any(ladders.values()):
            said.append(f"field `{key}`: only a `choice` field has answers to list")
            ladders = dict.fromkeys(ladders, ())
        if kind == TEXT and any(v is not None for v in thresholds.values()):
            said.append(f"field `{key}`: free text has no number to hold a threshold against")
            thresholds = dict.fromkeys(thresholds)

    return Review(
        again_on=ladders["again_on"],
        hard_on=ladders["hard_on"],
        easy_on=ladders["easy_on"],
        again_below=thresholds["again_below"],
        hard_below=thresholds["hard_below"],
        easy_from=thresholds["easy_from"],
        easy_needs_answer=_flag(raw, "easy_needs_answer", False),
        unless_better_known=_flag(raw, "unless_better_known", False),
    )


def _options(raw: Any, key: str, said: list[str]) -> tuple[Choice, ...]:
    out: list[Choice] = []
    seen: set[str] = set()
    for entry in raw if isinstance(raw, list) else []:
        # A bare string is an option whose word is also what is stored.
        if isinstance(entry, str):
            entry = {"value": entry, "label": entry}
        if not isinstance(entry, dict):
            continue
        value = _text(entry.get("value"))
        if not value or value in seen:
            continue
        seen.add(value)
        label = _text(entry.get("label"), value) or value
        if len(label) > MAX_OPTION_LABEL:
            said.append(
                f"field `{key}`: \"{label}\" is longer than {MAX_OPTION_LABEL} "
                "characters and will be cut off on the form"
            )
        out.append(Choice(value=value, label=label, report=_text(entry.get("report"))))
    return tuple(out)


def _field(raw: Any, seen: set[str], said: list[str]) -> Field | None:
    if not isinstance(raw, dict):
        return None
    key = raw.get("key")
    if not isinstance(key, str) or not _KEY.match(key):
        said.append(
            f"a field with the key {key!r} was dropped: a key is lowercase "
            "letters, digits and underscores, starting with a letter"
        )
        return None
    if key in RESERVED_KEYS:
        said.append(f"field `{key}` was dropped: that name is one the app fills in itself")
        return None
    if key in seen:
        said.append(f"field `{key}` is defined twice; the first one stands")
        return None

    kind = raw.get("kind", TEXT)
    if kind not in KINDS:
        said.append(
            f"field `{key}` was dropped: `{kind}` is not a kind "
            f"({', '.join(KINDS)})"
        )
        return None

    options = _options(raw.get("options"), key, said) if kind == CHOICE else ()
    if kind == CHOICE and len(options) < 2:
        said.append(f"field `{key}` was dropped: a ladder needs at least two options")
        return None

    default = raw.get("default")
    values = tuple(o.value for o in options)
    if kind == CHOICE and default is not None and default not in values:
        said.append(f"field `{key}`: the default `{default}` is not one of its options")
        default = None

    label = _text(raw.get("label")) or key.replace("_", " ")
    if len(label) > MAX_STAT_LABEL and key not in COLUMN_KEYS:
        # Not for the fields with columns: those are drawn by
        # `render.approach_rows` under names of its own.
        said.append(
            f"field `{key}`: the label \"{label}\" is longer than {MAX_STAT_LABEL} "
            "characters and will be cut off on the stat line"
        )

    seen.add(key)
    return Field(
        key=key,
        kind=kind,
        label=label,
        group=_text(raw.get("group")),
        placeholder=raw.get("placeholder") if isinstance(raw.get("placeholder"), str) else "",
        options=options,
        default=default if isinstance(default, str) else None,
        minimum=_number(raw.get("min")),
        maximum=_number(raw.get("max")),
        review=_review(raw.get("review"), kind, key, values, said),
    )


def _per_difficulty(raw: Any, cast: type) -> tuple[tuple[str, Any], ...]:
    if not isinstance(raw, dict):
        return ()
    return tuple(
        (str(name).lower(), cast(value))
        for name, value in raw.items()
        if _number(value) is not None and float(value) > 0
    )


def _verdicts(raw: Any, said: list[str]) -> tuple[str, ...]:
    # Function-local: `scoring` imports this module to find a type's par, and
    # resolving the name at call time rather than at import time is what makes
    # that legal -- the trick `events.srs_context` uses to reach `config`.
    from . import scoring

    if raw is None:
        return tuple(scoring.VERDICTS)
    offered: list[str] = []
    for verdict in raw if isinstance(raw, list) else []:
        if verdict in scoring.VERDICTS and verdict not in offered:
            offered.append(verdict)
        elif verdict not in offered:
            said.append(f"`{verdict}` is not a verdict, and was left off the ladder")
    if not any(v in scoring.SOLVED_VERDICTS for v in offered):
        # A ladder you cannot succeed on is a type whose every attempt fails.
        said.append("`verdicts` offers no way to have solved it, so the whole ladder is used")
        return tuple(scoring.VERDICTS)
    return tuple(offered)


def parse(name: str, raw: Mapping[str, Any], source: str = "bundled") -> ProblemType:
    """A type, from what its file said. Never raises on a file's contents."""
    said: list[str] = []
    solve = _table(raw, "solve")
    screens = _table(raw, "screens")
    capture = _table(raw, "capture")
    ai = _table(raw, "ai")

    seen: set[str] = set()
    fields: list[Field] = []
    listed = raw.get("fields", [])
    for entry in listed if isinstance(listed, list) else []:
        parsed = _field(entry, seen, said)
        if parsed is not None:
            fields.append(parsed)

    ai_group = _text(ai.get("group"))
    if ai_group and ai_group not in {f.group for f in fields}:
        said.append(f"`ai.group` names \"{ai_group}\", which no field is in")
        ai_group = ""

    note = capture.get("note_template")
    return ProblemType(
        name=name,
        label=_text(raw.get("label")) or name,
        verdicts=_verdicts(raw.get("verdicts"), said),
        judge=_flag(solve, "judge", False),
        fetch=_text(solve.get("fetch")),
        solve_prompt=_text(solve.get("prompt"), DEFAULT_SOLVE_PROMPT) or DEFAULT_SOLVE_PROMPT,
        screens=Screens(
            patterns=_flag(screens, "patterns", True),
            methods=_flag(screens, "methods", True),
            solution=_flag(screens, "solution", True),
            note=_flag(screens, "note", True),
            again=_flag(screens, "again", True),
        ),
        language=_text(capture.get("language")).lower(),
        solution_prompt=_text(capture.get("prompt"), DEFAULT_SOLUTION_PROMPT)
        or DEFAULT_SOLUTION_PROMPT,
        note_template=note if isinstance(note, str) else "",
        ai_copy=_flag(ai, "copy", False),
        ai_prompt=_text(ai.get("prompt")),
        ai_paste_into=_text(ai.get("paste_into")),
        ai_group=ai_group,
        fields=tuple(fields),
        par_seconds=_per_difficulty(raw.get("par_seconds"), int),
        base=_per_difficulty(raw.get("base"), float),
        source=source,
        problems=tuple(said),
    )


# --- finding one -----------------------------------------------------------


def user_path(name: str) -> Path:
    return paths.types_dir() / f"{normalise(name)}.toml"


def bundled() -> list[str]:
    return sorted(
        p.name.removesuffix(".toml")
        for p in resources.files(_types_pkg).iterdir()
        if p.name.endswith(".toml")
    )


def bundled_text(name: str) -> str | None:
    """A bundled type's file, comments and all. None if there is no such type."""
    target = resources.files(_types_pkg).joinpath(f"{normalise(name)}.toml")
    try:
        return target.read_text(encoding="utf-8")
    except (OSError, FileNotFoundError):
        return None


def available() -> list[str]:
    """Every type there is a file for: the bundled ones, and yours."""
    mine: list[str] = []
    try:
        mine = [p.stem for p in paths.types_dir().glob("*.toml") if _NAME.match(p.stem)]
    except OSError:
        pass
    return sorted(set(bundled()) | set(mine))


def _bundled(name: str, problems: tuple[str, ...] = ()) -> ProblemType:
    text = bundled_text(name)
    if text is None:
        return ProblemType(
            name=name,
            label=name,
            verdicts=_verdicts(None, []),
            source="missing",
            problems=(
                *problems,
                f"there is no type called `{name}` — it asks nothing until there is",
            ),
        )
    parsed = parse(name, tomllib.loads(text), "bundled")
    if not problems:
        return parsed
    return dataclasses.replace(parsed, problems=(*problems, *parsed.problems))


@functools.lru_cache(maxsize=64)
def _load(name: str, path: str | None, stamp: tuple[int, int] | None) -> ProblemType:
    """`stamp` is only ever a cache key: a file you have just edited is a new one."""
    if path is None:
        return _bundled(name)
    try:
        raw = tomllib.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        return _bundled(name, (f"{path} could not be read ({exc}) — it was ignored",))
    return parse(name, raw, path)


def load(name: str | None = None) -> ProblemType:
    """The type called `name`, yours if you have written one.

    Cached on the file's own size and modification time rather than on the name
    alone, so an edit is picked up by the next thing that asks and a test that
    moves the config directory gets the type that lives in the new one.
    """
    name = normalise(name) or DEFAULT_TYPE
    path = user_path(name)
    try:
        stat = path.stat()
    except OSError:
        return _load(name, None, None)
    return _load(name, str(path), (stat.st_mtime_ns, stat.st_size))
