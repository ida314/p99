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

Three roles per attempt, and the differences between them are the whole point:

  used            what you wrote this time
  also_works      an equal alternative you did not write
  worth_learning  the better approach you can see now, and did not write

`worth_learning` is how "can you identify a meaningfully better approach?" gets
asked without asking it. Naming the approach is what makes it schedulable, and it
is also what separates a suboptimal solve you diagnosed yourself from one you
didn't -- see `srs.rate`.

`also_works` is deliberately *not* that. "There is a monotonic stack solution and
mine is a heap and they are both fine" is a fact about the problem, not evidence
that you were beaten -- so it is read by nothing that grades anything. Folding it
into `worth_learning` would make the record claim an asymptotic gap that nobody
reported, and would hand a suboptimal solve the demote-cancelling credit that
role exists to grant.

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
#: An approach of equal standing you did not write. Not a better one -- the
#: other route through the same problem that would have been just as good in
#: front of an interviewer, and that you want the problem to remember it has.
ALSO_WORKS = "also_works"
#: The better approach you can see now and did not write.
WORTH_LEARNING = "worth_learning"
#: Strongest claim first. `payload` resolves a name in two roles by this order,
#: and `is_empty` folds over it.
ROLES = (USED, ALSO_WORKS, WORTH_LEARNING)

ROLE_LABELS = {
    USED: "solved by",
    ALSO_WORKS: "also works",
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


@dataclass(frozen=True)
class Approach:
    """One row of a problem's library: an approach, and the code for it if any.

    `role` is the role this approach was named in most recently *on this
    problem*, which is not a fact about the approach itself -- the same
    monotonic stack can be what you wrote here and what you wish you had written
    there. It is None for one added from the library screen, where there was no
    attempt to name it in a role.

    `code_path` may be None: an approach you named and never wrote is still part
    of the problem's library, and the empty cell is the point of listing it.
    """

    key: str
    name: str
    role: str | None
    first_seen: str
    code_path: str | None = None
    language: str | None = None
    attempt_id: int | None = None
    time_optimality: str | None = None
    space_optimality: str | None = None
    written_at: str | None = None

    @property
    def written(self) -> bool:
        return bool(self.code_path)


#: A problem's approaches, with the solution attached where one exists.
#:
#: A LEFT JOIN rather than a read of `solutions` alone, because the library has
#: to be able to show you the approach you named and never wrote -- that gap is
#: the thing you would go to the screen to close.
_LIBRARY_SQL = """
SELECT s.key AS key, s.name AS name, ps.first_seen AS first_seen,
       (SELECT a.role FROM attempt_strategies a
         WHERE a.slug = ps.slug AND a.key = ps.key
         ORDER BY a.attempt_id DESC LIMIT 1) AS role,
       sol.code_path AS code_path, sol.language AS language,
       sol.attempt_id AS attempt_id,
       sol.time_optimality AS time_optimality,
       sol.space_optimality AS space_optimality,
       sol.written_at AS written_at
  FROM problem_strategies ps
  JOIN strategies s ON s.key = ps.key
  LEFT JOIN solutions sol ON sol.slug = ps.slug AND sol.key = ps.key
 WHERE ps.slug = ?
 ORDER BY s.name
"""


def library(conn: sqlite3.Connection, slug: str) -> list[Approach]:
    """Every approach recorded against one problem, written or not."""
    return [
        Approach(
            key=r["key"],
            name=r["name"],
            role=r["role"],
            first_seen=r["first_seen"],
            code_path=r["code_path"],
            language=r["language"],
            attempt_id=r["attempt_id"],
            time_optimality=r["time_optimality"],
            space_optimality=r["space_optimality"],
            written_at=r["written_at"],
        )
        for r in conn.execute(_LIBRARY_SQL, (slug,)).fetchall()
    ]


def problems_with_approaches(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Every problem that has named at least one approach, most recent first.

    Ordered by when the problem last gained one rather than alphabetically: the
    library is a thing you come back to right after a solve, and the problem you
    just worked on should be the row the cursor is already sitting on.
    """
    return conn.execute(
        "SELECT ps.slug AS slug, p.title AS title, p.difficulty AS difficulty, "
        "COUNT(*) AS approaches, "
        "SUM(CASE WHEN sol.key IS NULL THEN 0 ELSE 1 END) AS written, "
        "MAX(ps.first_seen) AS last_named "
        "FROM problem_strategies ps "
        "JOIN problems p ON p.slug = ps.slug "
        "LEFT JOIN solutions sol ON sol.slug = ps.slug AND sol.key = ps.key "
        "GROUP BY ps.slug ORDER BY last_named DESC, p.title ASC"
    ).fetchall()


def payload(
    used: Iterable[str],
    *,
    also_works: Iterable[str] = (),
    worth_learning: Iterable[str] = (),
) -> dict[str, list[str]]:
    """The `strategies` block of a `problem_finished` payload.

    Names, not keys: the log records what you typed, and the key is derived on
    the way into the projection. A rule change in `normalise` is then a replay
    away from being applied to everything you ever wrote, which is the same
    bargain every other derived thing in here makes.

    A name in several roles keeps the first one `ROLES` lists. You cannot
    simultaneously have written a thing and be wishing you had written it, and
    letting both through would make `saw_better` true for a solve that spotted
    nothing. The same collapse settles `also_works` against `worth_learning`:
    an approach cannot be both an equal and an improvement, and the weaker claim
    is the one to drop.

    Keyword-only past `used`, so the two-argument calls this replaced fail loudly
    instead of quietly filing every "better approach" as an equal one.
    """
    by_role = {USED: clean(used), ALSO_WORKS: clean(also_works), WORTH_LEARNING: clean(worth_learning)}
    block: dict[str, list[str]] = {}
    taken: set[str] = set()
    for role in ROLES:
        kept = [s for s in by_role[role] if s.key not in taken]
        taken.update(s.key for s in kept)
        block[role] = [s.name for s in kept]
    return block


def is_empty(block: Mapping[str, list[str]] | None) -> bool:
    """True when a payload block records nothing -- a skipped prompt."""
    return not block or not any(block.get(role) for role in ROLES)
