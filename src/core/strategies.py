"""Solving strategies — the reusable patterns you name yourself.

A strategy is a technique: "bottom-up tabulation", "monotonic stack",
"quickselect". It is deliberately not `problems.pattern`, which is the catalog's
word for where a problem sits in someone else's list. This is your word for the
technique, and there is no supplied taxonomy — the vocabulary is empty until you
type into it.

Shared across problems, and that is the whole reason it is a vocabulary. The same
technique turning up on `coin-change` and on `house-robber` is the fact worth
having: a strategy you keep being slow under is a weak spot in its own right,
which a per-problem note could never say.

**Tagged on the problem, not on the solve.** The prompt after a solve asks which
patterns *can* solve this problem, and the answer keeps every tag you have ever
put on it — tick min-heap, quickselect and sorting, and all three stay ticked the
next time you come back, whichever one you actually wrote tonight. A problem
solvable three ways is solvable three ways on an evening you took none of them.

What that buys is the question the per-attempt version could not answer: *every
problem quickselect is an answer to*, which is the list you want when you sit
down to drill quickselect. The per-attempt version could only say which problems
quickselect happened to be the answer you gave, which is a fact about your
evenings and not about the problems.

Which route you took tonight is not asked here, and not lost either: it is the
methods prompt one screen later, where a whole route through one problem gets a
row of its own. Splitting the two is the point — `problem_strategies` is what the
problem admits, `problem_methods` is the way through it — and the two tables
still never join. See `methods`.

Unticking removes. A tag is a claim about the problem, so taking one off is
saying the claim was wrong, and the next solve opens without it. The log still
only grows: the removal rides a `problem_strategies_set` event carrying the whole
list, exactly the way `settings_changed` carries a value rather than a diff.

One role now: `used`, which since the question changed means "this problem can be
solved with it" on every attempt that has been open while the tags stood.

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
from typing import Any, Iterable, Mapping

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
    """Every technique you have said can solve this problem.

    The problem's own list, read from `problem_strategies` and not folded up out
    of the attempts. It used to be the union over the attempts, and that read
    could only ever grow: unticking a tag you had decided was wrong changed
    nothing, because the solve that first recorded it still said so. A list you
    cannot take something off is not a list you can trust to be a claim.

    This is what the prompt after a solve opens with already ticked, so tagging a
    problem is something you do once and correct rather than something you redo
    every time you solve it.
    """
    return _rows(
        conn,
        "SELECT s.key AS key, s.name AS name, s.first_seen AS first_seen "
        "FROM problem_strategies p JOIN strategies s ON s.key = p.key "
        "WHERE p.slug = ? ORDER BY s.name",
        (slug,),
    )


def problems_with_strategies(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Every problem carrying at least one tag, most recently tagged first.

    Ordered by when the problem was last tagged rather than alphabetically, for
    the same reason `methods.problems_with_methods` is: this is a list you open
    right after a solve, and the problem you just worked on should be the row the
    cursor is already on.
    """
    return conn.execute(
        "SELECT p.slug AS slug, pr.title AS title, pr.difficulty AS difficulty, "
        "COUNT(*) AS tags, MAX(p.updated_at) AS last_tagged "
        "FROM problem_strategies p JOIN problems pr ON pr.slug = p.slug "
        "GROUP BY p.slug ORDER BY last_tagged DESC, pr.title ASC"
    ).fetchall()


def problem_counts(conn: sqlite3.Connection) -> dict[str, int]:
    """How many problems carry each technique, keyed by strategy.

    The number that makes a tag worth reading back: "hash map — 14 problems"
    says this is a technique you keep meeting, and a 1 says you have named
    something once and may have meant a word you already had. It is also the
    shape of the question the tags were moved onto the problem to answer, one
    step short of asking it: which problems, rather than how many.
    """
    return {
        r["key"]: r["problems"]
        for r in conn.execute(
            "SELECT key, COUNT(*) AS problems FROM problem_strategies GROUP BY key"
        ).fetchall()
    }


def payload(used: Iterable[str]) -> dict[str, list[str]]:
    """The `strategies` block of a `problem_finished` payload.

    Names, not keys: the log records what you typed, and the key is derived on
    the way into the projection. A rule change in `normalise` is then a replay
    away from being applied to everything you ever wrote, which is the same
    bargain every other derived thing in here makes.

    One role. `worth_learning` used to be the second parameter here and is now
    something the log can contain but nothing can produce -- what it was reaching
    for is a property of the problem, and `methods.payload` is where that goes.

    Still carried on the finish, now that the tags belong to the problem, because
    it is what `attempt_strategies` is folded from: which tags this problem wore
    on the night of this attempt. The problem's live list is set by the
    `problem_strategies_set` event that rides alongside -- see `set_payload`.
    """
    return {USED: [entry.name for entry in clean(used)]}


def set_payload(slug: str, names: Iterable[str]) -> dict[str, Any]:
    """The whole `problem_strategies_set` payload: this problem's tags, entire.

    The list and not a diff, which is what makes a removal expressible in an
    append-only log: the event says what the set *is*, the fold makes the table
    match, and a replay lands on the same set by reading the same last word.
    `settings_changed` carries a value for the same reason.

    No `attempt_uuid`, deliberately. A tag is a claim about the problem, so it
    outlives the attempt that happened to be on screen when you made it -- and
    throwing that attempt away, which skips its `problem_finished` outright, must
    not quietly untag the problem.
    """
    return {"slug": slug, USED: [entry.name for entry in clean(names)]}


def is_empty(block: Mapping[str, list[str]] | None) -> bool:
    """True when a payload block records nothing -- a skipped prompt."""
    return not block or not any(block.get(role) for role in ROLES)
