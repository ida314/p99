"""Solving strategies — the reusable patterns you name yourself.

A strategy is what you reached for: "bottom-up tabulation", "monotonic stack",
"quickselect". It is deliberately not `problems.pattern`, which is the catalog's
word for where a problem sits in someone else's list. This is your word for the
technique you actually used, and there is no supplied taxonomy — the vocabulary
is empty until you type into it.

Shared across problems, and that is the whole reason it is a vocabulary. The same
technique turning up on `coin-change` and on `house-robber` is the fact worth
having: a strategy you keep being slow under is a weak spot in its own right,
which a per-problem note could never say, and `stats.distributions` slices your
solve times by it for exactly that reason.

Not a way of solving any one problem. That is `methods` — the whole route through
one problem, named in that problem's own terms and keyed by its slug. The two
lists are independent: this one says which patterns you reached for, that one says
which ways the problem admits, and nothing joins a row of one to a row of the
other. A method is usually built out of several strategies, and it says so in its
own name rather than in a foreign key.

One role now: `used`, the patterns you reached for this time.

`worth_learning` is **legacy**: it was a second role here, naming a better
approach you could see and had not written, and attempts recorded under it keep
it forever. It still renders and it still grades -- `srs.rate` reads `saw_better`,
which is now "you named a better approach" *or* "this problem has an optimal
method recorded that is not the one you wrote", so an old answer and a new one
reach the same place. Nothing new is ever written under it, exactly as
`scoring.VERDICT_LABELS` keeps the retired verdicts renderable while
`scoring.VERDICTS` gates what you can pick.

Nothing here writes. Like `scoring` and `catalog`, this module is a pure function
plus reads; the write path is `events.apply` folding a `problem_finished` payload.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from typing import Iterable, Mapping

#: The patterns you reached for. The only role a new attempt can record.
USED = "used"
#: Legacy: the better approach you could see and did not write, asked here
#: before methods were a list of their own. Still folded, still rendered, still
#: read by `saw_better` -- never written by anything new.
WORTH_LEARNING = "worth_learning"
#: Every role the log can contain, for folding and rendering.
ROLES = (USED, WORTH_LEARNING)
#: What the prompt can put you in. `ROLES` is what history can hold; this is
#: what tonight can add to it -- the same split `scoring.VERDICTS` makes against
#: `scoring.VERDICT_LABELS`.
SELECTABLE_ROLES = (USED,)

ROLE_LABELS = {
    USED: "used",
    WORTH_LEARNING: "worth learning",
}

# Long enough for "bottom-up tabulation over the coin axis", short enough that a
# pasted paragraph cannot become a permanent row in the vocabulary. A whole route
# through a problem belongs in `methods`, whose names are allowed to be longer
# for exactly that reason.
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
# Both of these order by `name`, not by key and not by when you added it. The
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
    """The strategies you have named on one problem, in any role.

    Read straight off the attempts rather than out of a per-problem list, because
    there is no per-problem list to keep: a strategy belongs to the vocabulary and
    to the solves that reached for it, and "which patterns have I used on this
    problem" is a question those two already answer between them. Nothing extra is
    written, so nothing extra can fall out of step.
    """
    return _rows(
        conn,
        "SELECT s.key AS key, s.name AS name, s.first_seen AS first_seen "
        "FROM attempt_strategies a JOIN strategies s ON s.key = a.key "
        "WHERE a.slug = ? GROUP BY s.key, s.name, s.first_seen ORDER BY s.name",
        (slug,),
    )


def payload(used: Iterable[str]) -> dict[str, list[str]]:
    """The `strategies` block of a `problem_finished` payload.

    Names, not keys: the log records what you typed, and the key is derived on
    the way into the projection. A rule change in `normalise` is then a replay
    away from being applied to everything you ever wrote, which is the same
    bargain every other derived thing in here makes.

    One role. `worth_learning` used to be the second parameter here and is now
    something the log can contain but nothing can produce -- what it was reaching
    for is a property of the problem, and `methods.payload` is where that goes.
    """
    return {USED: [entry.name for entry in clean(used)]}


def is_empty(block: Mapping[str, list[str]] | None) -> bool:
    """True when a payload block records nothing -- a skipped prompt."""
    return not block or not any(block.get(role) for role in ROLES)
