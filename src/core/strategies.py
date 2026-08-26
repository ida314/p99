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

One role now: `used`, what you wrote this time. The prompt that asks it is a
question about *you* -- which technique did you reach for -- and the answer is a
word from a vocabulary that spans every problem you have ever solved.

Everything about the *problem* moved out. "There is also a monotonic stack
solution, and it is the optimal one, and I have never written it" is not a fact
about tonight's attempt at all; it is a standing fact about the problem, true
before you sat down and true after. It lives in `problem_solutions` -- see the
`solutions` prompt that follows this one -- where it can carry its own optimality
and its own code, neither of which a per-attempt answer could ever hold.

`worth_learning` is **legacy**: it was a second role here before the solutions
page existed, and attempts recorded under it keep it forever. It still renders
and it still grades -- `srs.rate` reads `saw_better`, which is now "you named a
better approach" *or* "this problem has an optimal solution that is not the one
you wrote", so an old answer and a new one reach the same place. Nothing new is
ever written under it, exactly as `scoring.VERDICT_LABELS` keeps the retired
verdicts renderable while `scoring.VERDICTS` gates what you can pick.

Nothing here writes. Like `scoring` and `catalog`, this module is a pure function
plus reads; the write path is `events.apply` folding a `problem_finished` payload.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

#: What you wrote. The only role a new attempt can record.
USED = "used"
#: Legacy: the better approach you could see and did not write, asked here
#: before the solutions page existed. Still folded, still rendered, still read
#: by `saw_better` -- never written by anything new.
WORTH_LEARNING = "worth_learning"
#: Every role the log can contain, for folding and rendering.
ROLES = (USED, WORTH_LEARNING)
#: What the prompt can put you in. `ROLES` is what history can hold; this is
#: what tonight can add to it -- the same split `scoring.VERDICTS` makes against
#: `scoring.VERDICT_LABELS`.
SELECTABLE_ROLES = (USED,)

ROLE_LABELS = {
    USED: "solved by",
    WORTH_LEARNING: "worth learning",
}

#: What a solution costs, relative to the best this problem admits. The same
#: three words `attempts.time_optimality` uses, deliberately: one vocabulary for
#: one question, whether it is asked about a solve or about a solution.
OPTIMAL = "optimal"
SUBOPTIMAL = "suboptimal"
UNSURE = "unsure"
OPTIMALITIES = (OPTIMAL, SUBOPTIMAL, UNSURE)

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


@dataclass(frozen=True)
class Solution:
    """One way this problem can be solved.

    Not a fact about an attempt. A problem admits the approaches it admits
    whether or not you have ever taken them, and this row says so: `optimality`
    is what that route costs, `code_path` is your write-up of it if you have one,
    and both are empty on a way you have only ever heard of.

    `attempt_id` is the solve that last wrote it, which is null for a route you
    added to the list without sitting the problem. `written` is the question the
    screen actually asks -- is there code here -- and it is deliberately not
    "have you ever solved it this way": you can write an approach up months after
    the solve, and the file is what makes it worth coming back to.
    """

    key: str
    name: str
    optimality: str | None = None
    code_path: str | None = None
    language: str | None = None
    attempt_id: int | None = None
    #: The complexity you typed at the finish prompt of the solve that wrote
    #: this. Shown, never asked for twice -- the solutions page has no
    #: complexity field of its own, because it would be asking again for
    #: something the verdict prompt already has.
    complexity: str | None = None
    space_complexity: str | None = None
    first_seen: str = ""

    @property
    def written(self) -> bool:
        return bool(self.code_path)


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
    """The strategies recorded against one problem, in any role or as a solution."""
    return _rows(
        conn,
        "SELECT s.key AS key, s.name AS name, ps.first_seen AS first_seen "
        "FROM problem_solutions ps JOIN strategies s ON s.key = ps.key "
        "WHERE ps.slug = ? ORDER BY s.name",
        (slug,),
    )


#: One problem's ways, with the code and the cost claim on each.
#:
#: The complexity columns come off the attempt that wrote the file, because that
#: is where you typed them and asking twice for the same number is how a prompt
#: earns being skipped. They are read-only here.
_SOLUTIONS_SQL = """
SELECT s.key AS key, s.name AS name, ps.first_seen AS first_seen,
       ps.optimality AS optimality, ps.code_path AS code_path,
       ps.language AS language, ps.attempt_id AS attempt_id,
       a.claimed_complexity AS complexity,
       a.claimed_space_complexity AS space_complexity
  FROM problem_solutions ps
  JOIN strategies s ON s.key = ps.key
  LEFT JOIN attempts a ON a.id = ps.attempt_id
 WHERE ps.slug = ?
 ORDER BY s.name
"""


def solutions(conn: sqlite3.Connection, slug: str) -> list[Solution]:
    """Every way this problem can be solved that you have recorded."""
    return [
        Solution(
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
        for r in conn.execute(_SOLUTIONS_SQL, (slug,)).fetchall()
    ]


def problems_with_solutions(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Every problem with at least one recorded way, most recently added first.

    Ordered by when the problem last gained one rather than alphabetically: this
    is a list you come back to right after a solve, and the problem you just
    worked on should be the row the cursor is already sitting on.
    """
    return conn.execute(
        "SELECT ps.slug AS slug, p.title AS title, p.difficulty AS difficulty, "
        "COUNT(*) AS ways, "
        "SUM(CASE WHEN ps.code_path IS NULL THEN 0 ELSE 1 END) AS written, "
        "SUM(CASE WHEN ps.optimality = 'optimal' THEN 1 ELSE 0 END) AS optimal, "
        "MAX(ps.updated_at) AS last_touched "
        "FROM problem_solutions ps "
        "JOIN problems p ON p.slug = ps.slug "
        "GROUP BY ps.slug ORDER BY last_touched DESC, p.title ASC"
    ).fetchall()


def payload(used: Iterable[str]) -> dict[str, list[str]]:
    """The `strategies` block of a `problem_finished` payload.

    Names, not keys: the log records what you typed, and the key is derived on
    the way into the projection. A rule change in `normalise` is then a replay
    away from being applied to everything you ever wrote, which is the same
    bargain every other derived thing in here makes.

    One role. `worth_learning` used to be the second parameter here and is now
    something the log can contain but nothing can produce -- what it was reaching
    for is a property of the problem, and `solutions_payload` is where that
    goes.
    """
    return {USED: [entry.name for entry in clean(used)]}


def solutions_payload(entries: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """The `solutions` block of a `problem_finished` payload.

    One entry per way this problem can be solved, as `{"name", "optimality"}`.
    Deduped by key with the first spelling winning, the same rule `clean` applies
    everywhere else, so a list holding both "Two Pointers" and "two pointers"
    records one way rather than two rows that disagree about the same route.

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
            }
        )
    return out


def is_empty(block: Mapping[str, list[str]] | None) -> bool:
    """True when a payload block records nothing -- a skipped prompt."""
    return not block or not any(block.get(role) for role in ROLES)
