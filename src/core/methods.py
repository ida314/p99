"""Canonical methods: the ways one problem can be solved.

A method is a whole route through *one* problem — "sort, then two pointers from
both ends", "count with a hash map and scan once". It is the answer to "how is
this problem solved", and a problem admits the methods it admits whether or not
you have ever taken them.

Deliberately not `strategies`, and the split is the whole point of this module.
A strategy is a reusable pattern that spans problems: `two-pointers` means the
same thing on `3sum` as it does on `container-with-most-water`, which is why that
vocabulary is shared and why the stats screen can slice your solve times by it. A
method means nothing away from its problem — "sort, then two pointers from both
ends" is not a technique, it is *this problem's* way — so methods are keyed by
`(slug, key)` and there is no cross-problem vocabulary to collide in.

The two lists never reference each other. A method is usually composed of several
strategies, and that composition is said in the method's own name rather than
stored as a link: what you want back from this list is the route, and a route
assembled out of foreign keys reads worse than the sentence you would have
written anyway. `attempt_strategies` says which patterns you used; `problem_methods`
says which ways the problem admits; `attempt_methods` says which of those you
wrote. Nothing joins the first to the second.

Each row carries what that route costs (`optimality`), and the newest file you
wrote for it. `optimality` is what `srs.rate` reads as `saw_better`: an optimal
method recorded on this problem that is not the one you wrote is you knowing
there was better, and unlike an answer given in the ninety seconds after a solve
it can be recorded two months later and still be true.

Nothing here writes. Like `strategies` and `scoring`, this module is a pure
function plus reads; the write path is `events.apply` folding a `methods` block.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

#: What a method costs, relative to the best this problem admits. The same three
#: words `attempts.time_optimality` uses, deliberately: one vocabulary for one
#: question, whether it is asked about a solve or about a route.
OPTIMAL = "optimal"
SUBOPTIMAL = "suboptimal"
UNSURE = "unsure"
OPTIMALITIES = (OPTIMAL, SUBOPTIMAL, UNSURE)

# Longer than a strategy's, because a method is allowed to be a sentence: "sort,
# then two pointers from both ends" is the name that actually tells you the
# route, and truncating it to a technique's length would turn every method back
# into the strategy this module exists to stop it being.
MAX_NAME = 80

_SEPARATORS = re.compile(r"[^a-z0-9]+")


@dataclass(frozen=True)
class Named:
    """A typed method name and the key it stores under."""

    key: str
    name: str


@dataclass(frozen=True)
class Method:
    """One way this problem can be solved.

    Not a fact about an attempt. `optimality` is what the route costs, `code_path`
    is your write-up of it if you have one, and both are empty on a method you
    have only ever heard of — which is the row worth opening this list for.

    `attempt_id` is the solve that last wrote code for it, null for a route you
    added without sitting the problem. `complexity` comes off that attempt: you
    typed it at the verdict prompt, and asking again here would be asking twice.
    """

    key: str
    name: str
    optimality: str | None = None
    code_path: str | None = None
    language: str | None = None
    attempt_id: int | None = None
    complexity: str | None = None
    space_complexity: str | None = None
    first_seen: str = ""

    @property
    def written(self) -> bool:
        return bool(self.code_path)


def normalise(name: str) -> str:
    """The identity of a method within its problem: lowercased, punctuation to hyphens.

    "Sort + Two Pointers" and "sort, two pointers" are one method on this
    problem, not two. The same rule `strategies.normalise` applies, written out
    here rather than imported: the two vocabularies are separate by design, and
    sharing an import is how one of them ends up quietly following the other's
    rules.

    Returns "" for anything that normalises to nothing, which the caller drops.
    """
    return _SEPARATORS.sub("-", name.strip().lower()).strip("-")


def clean(names: Iterable[str]) -> list[Named]:
    """Typed names to storable `(key, name)` pairs: trimmed, deduped, ordered.

    First spelling of a key wins, so a list holding both "Sort + Two Pointers"
    and "sort two pointers" records one method under the name you gave it first.
    """
    out: list[Named] = []
    seen: set[str] = set()
    for raw in names:
        name = " ".join(str(raw).split())[:MAX_NAME]
        key = normalise(name)
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(Named(key=key, name=name))
    return out


# --- reads -----------------------------------------------------------------
#
# Ordered by name, not by key and not by when you added it. The list is
# alphabetical everywhere it is drawn, because a list that reorders itself
# between two solves of the same problem is a list you read every time instead
# of reaching into.

#: One problem's methods, with the code and the cost claim on each.
#:
#: The complexity columns come off the attempt that wrote the file, because that
#: is where you typed them. They are read-only here.
_FOR_PROBLEM_SQL = """
SELECT m.key AS key, m.name AS name, m.first_seen AS first_seen,
       m.optimality AS optimality, m.code_path AS code_path,
       m.language AS language, m.attempt_id AS attempt_id,
       a.claimed_complexity AS complexity,
       a.claimed_space_complexity AS space_complexity
  FROM problem_methods m
  LEFT JOIN attempts a ON a.id = m.attempt_id
 WHERE m.slug = ?
 ORDER BY m.name
"""


def for_problem(conn: sqlite3.Connection, slug: str) -> list[Method]:
    """Every way this problem can be solved that you have recorded."""
    return [
        Method(
            key=r["key"],
            name=r["name"],
            optimality=r["optimality"],
            code_path=r["code_path"],
            language=r["language"],
            attempt_id=r["attempt_id"],
            complexity=r["complexity"],
            space_complexity=r["space_complexity"],
            first_seen=r["first_seen"],
        )
        for r in conn.execute(_FOR_PROBLEM_SQL, (slug,)).fetchall()
    ]


def used_by_attempt(conn: sqlite3.Connection, attempt_uuid: str) -> list[Named]:
    """The methods one attempt says it wrote, alphabetical."""
    return [
        Named(key=r["key"], name=r["name"])
        for r in conn.execute(
            "SELECT am.key AS key, m.name AS name FROM attempt_methods am "
            "LEFT JOIN problem_methods m ON m.slug = am.slug AND m.key = am.key "
            "WHERE am.attempt_uuid = ? ORDER BY m.name",
            (attempt_uuid,),
        ).fetchall()
    ]


def problems_with_methods(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Every problem with at least one recorded method, most recently touched first.

    Ordered by when the problem last gained one rather than alphabetically: this
    is a list you come back to right after a solve, and the problem you just
    worked on should be the row the cursor is already sitting on.
    """
    return conn.execute(
        "SELECT m.slug AS slug, p.title AS title, p.difficulty AS difficulty, "
        "COUNT(*) AS ways, "
        "SUM(CASE WHEN m.code_path IS NULL THEN 0 ELSE 1 END) AS written, "
        "SUM(CASE WHEN m.optimality = 'optimal' THEN 1 ELSE 0 END) AS optimal, "
        "MAX(m.updated_at) AS last_touched "
        "FROM problem_methods m "
        "JOIN problems p ON p.slug = m.slug "
        "GROUP BY m.slug ORDER BY last_touched DESC, p.title ASC"
    ).fetchall()


def payload(entries: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """The `methods` block of a `problem_finished` payload.

    One entry per way this problem can be solved, as
    `{"name", "optimality", "used"}`. Deduped by key with the first spelling
    winning, the same rule `clean` applies everywhere else.

    `used` is the one part of an entry that is about tonight rather than about
    the problem: it is the method you actually wrote, and it is what the archived
    file is tagged with and what `saw_better` compares the problem's optimal
    methods against.

    An optimality outside `OPTIMALITIES` is dropped rather than stored: the
    column feeds `saw_better`, and a value nothing recognises would sit in it
    forever meaning neither yes nor no.
    """
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for entry in entries:
        named = clean([str(entry.get("name") or "")])
        if not named or named[0].key in seen:
            continue
        seen.add(named[0].key)
        optimality = entry.get("optimality")
        out.append(
            {
                "name": named[0].name,
                "optimality": optimality if optimality in OPTIMALITIES else None,
                "used": bool(entry.get("used")),
            }
        )
    return out
