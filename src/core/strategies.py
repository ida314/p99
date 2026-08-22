"""Solving strategies — the vocabulary of approaches you name yourself.

A strategy is what you reached for: "bottom-up tabulation", "monotonic stack",
"quickselect". It is deliberately not `problems.pattern`, which is the catalog's
word for where a problem sits in someone else's list. This is your word for how
you actually solved it, and there is no supplied taxonomy — the vocabulary is
empty until you type into it.

Shared across problems, not scoped to one. The same technique turning up on
`coin-change` and on `house-robber` is the fact worth having: a strategy you keep
failing under is a weak spot in its own right, which a per-problem note could
never say.

Two roles per attempt, and the difference between them is the whole point:

  used            what you wrote this time
  worth_learning  the better approach you can see now, and did not write

`worth_learning` is how "can you identify a meaningfully better approach?" gets
asked without asking it. Naming the approach is what makes it schedulable, and it
is also what separates a suboptimal solve you diagnosed yourself from one you
didn't -- see `srs.rate`.

Nothing here writes. Like `scoring` and `catalog`, this module is a pure function
plus reads; the write path is `events.apply` folding a `problem_finished` payload.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from typing import Iterable, Mapping

#: What you wrote.
USED = "used"
#: The better approach you can see now and did not write.
WORTH_LEARNING = "worth_learning"
ROLES = (USED, WORTH_LEARNING)

ROLE_LABELS = {
    USED: "solved by",
    WORTH_LEARNING: "worth learning",
}

# Long enough for "bottom-up tabulation over the coin axis", short enough that a
# pasted paragraph cannot become a permanent row in the vocabulary.
MAX_NAME = 60

_SEPARATORS = re.compile(r"[^a-z0-9]+")


@dataclass(frozen=True)
class Strategy:
    key: str
    name: str
    first_seen: str | None = None


def normalise(name: str) -> str:
    """The identity of a strategy: lowercased, punctuation collapsed to hyphens.

    "Top-Down DP", "top down dp" and "  Top   Down   DP  " are one strategy, not
    three. The key is what the tables join on; the display name is whatever you
    typed the first time, because the vocabulary is yours and correcting your
    spelling of it is not this module's job.

    Returns "" for anything that normalises to nothing, which the caller drops.
    """
    return _SEPARATORS.sub("-", name.strip().lower()).strip("-")


def clean(names: Iterable[str]) -> list[Strategy]:
    """Typed names to storable (key, name) pairs: trimmed, deduped, ordered.

    First spelling of a key wins, so a list that says both "Two Pointers" and
    "two pointers" records one strategy under the name you gave it first.
    """
    out: list[Strategy] = []
    seen: set[str] = set()
    for raw in names:
        name = " ".join(str(raw).split())[:MAX_NAME]
        key = normalise(name)
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(Strategy(key=key, name=name))
    return out


# --- reads -----------------------------------------------------------------
#
# All of these order by `name`, not by key and not by when you added it. The
# picker is alphabetical, and a list that reorders itself between two solves of
# the same problem is a list you have to read every time instead of reaching into.


def _rows(conn: sqlite3.Connection, sql: str, args: tuple = ()) -> list[Strategy]:
    return [
        Strategy(key=r["key"], name=r["name"], first_seen=r["first_seen"])
        for r in conn.execute(sql, args).fetchall()
    ]


def vocabulary(conn: sqlite3.Connection) -> list[Strategy]:
    """Every strategy you have ever named, on any problem."""
    return _rows(conn, "SELECT key, name, first_seen FROM strategies ORDER BY name")


def for_problem(conn: sqlite3.Connection, slug: str) -> list[Strategy]:
    """The strategies recorded against one problem, in either role."""
    return _rows(
        conn,
        "SELECT s.key AS key, s.name AS name, ps.first_seen AS first_seen "
        "FROM problem_strategies ps JOIN strategies s ON s.key = ps.key "
        "WHERE ps.slug = ? ORDER BY s.name",
        (slug,),
    )


def payload(used: Iterable[str], worth_learning: Iterable[str]) -> dict[str, list[str]]:
    """The `strategies` block of a `problem_finished` payload.

    Names, not keys: the log records what you typed, and the key is derived on
    the way into the projection. A rule change in `normalise` is then a replay
    away from being applied to everything you ever wrote, which is the same
    bargain every other derived thing in here makes.

    A name in both roles is `used` only. You cannot simultaneously have written a
    thing and be wishing you had written it, and letting both through would make
    `saw_better` true for a solve that spotted nothing.
    """
    used_clean = clean(used)
    used_keys = {s.key for s in used_clean}
    worth_clean = [s for s in clean(worth_learning) if s.key not in used_keys]
    return {
        USED: [s.name for s in used_clean],
        WORTH_LEARNING: [s.name for s in worth_clean],
    }


def is_empty(block: Mapping[str, list[str]] | None) -> bool:
    """True when a payload block records nothing -- a skipped prompt."""
    return not block or not any(block.get(role) for role in ROLES)
