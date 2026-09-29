"""The examples in the docs are files somebody will copy. They have to work.

A page that shows a type with a typo in it is worse than no page: the app will
not stop on it — a broken type never stops a run — so what you get is a field
quietly missing and no idea why. Every fenced example of a type, a set or a
`[sets]` table is therefore parsed here by the code that would read the real
file.
"""

from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path

import pytest

from core import catalog, config, problemtypes

ROOT = Path(__file__).resolve().parent.parent
PAGES = ("docs/problem-types.md", "README.md")

FENCE = re.compile(r"^```(toml|json)\n(.*?)^```", re.MULTILINE | re.DOTALL)

#: Top-level keys that make a TOML example a type rather than a config.
TYPE_KEYS = {"fields", "screens", "solve", "verdicts", "par_seconds", "capture"}


def _examples(language: str) -> list:
    found = []
    for page in PAGES:
        text = (ROOT / page).read_text(encoding="utf-8")
        for n, match in enumerate(FENCE.finditer(text)):
            if match.group(1) == language:
                found.append(pytest.param(match.group(2), id=f"{page}#{n}"))
    return found


def _types_the_docs_provide() -> set[str]:
    written = set()
    for page in PAGES:
        text = (ROOT / page).read_text(encoding="utf-8")
        written |= set(re.findall(r"types --new ([a-z0-9-]+)", text))
    return set(problemtypes.bundled()) | written


def test_there_are_examples_to_check():
    """A regex that stopped matching would pass every test below by finding none."""
    assert len(_examples("toml")) >= 6
    assert len(_examples("json")) >= 2


@pytest.mark.parametrize("text", _examples("toml"))
def test_a_toml_example_is_one_the_app_would_accept(text):
    raw = tomllib.loads(text)

    if TYPE_KEYS & set(raw):
        parsed = problemtypes.parse("example", raw)
        assert parsed.problems == (), parsed.problems
        # Every field written in the example came through, none dropped.
        assert len(parsed.fields) == len(raw.get("fields", []))

    for name, body in raw.get("sets", {}).items():
        named = {s.name: s for s in config._sets(raw)}[name]
        assert named.type == body["type"]
        assert named.path == body.get("path")
        assert named.queue_n == body.get("queue_n")
        # And the type it names is one that exists to be named: bundled, or
        # one the docs themselves have you write with `types --new`.
        assert named.type in _types_the_docs_provide(), named.type


@pytest.mark.parametrize("text", _examples("json"))
def test_a_json_example_is_a_set_the_app_would_seed(text):
    entries = json.loads(text)
    assert isinstance(entries, list) and entries
    for raw in entries:
        row, why = catalog._entry(raw, "example", None)
        assert row is not None, why


def test_the_walkthrough_names_commands_that_exist():
    """Every `p99 <command>` on the page is one the parser knows."""
    from core import branding, cli

    parser = cli.build_parser()
    known = set(parser._subparsers._group_actions[0].choices)  # type: ignore[union-attr]
    page = (ROOT / "docs/problem-types.md").read_text(encoding="utf-8")
    # In code only -- after a backtick, or at the start of a line in a fenced
    # block. "How p99 knows what to ask" is a sentence, not a command.
    used = set(
        re.findall(rf"(?:`|^){branding.COMMAND} ([a-z]+)\b", page, flags=re.MULTILINE)
    )
    assert {"types", "seed", "doctor", "replay"} <= used
    assert used <= known, used - known
