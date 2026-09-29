"""Problem catalog (spec §1: title, slug, url, difficulty, tags — never content).

Seeded from JSON lists: the one checked in here, and any you name under
`[sets]` in `config.toml`. Seeding is an upsert, so shipping a corrected or
extended list is safe: it never touches attempt history.

Every set says which *type* its problems are (see `problemtypes`), and that is
the one thing a problem carries that its own entry does not say: a list of
LeetCode problems and a list of system design prompts are the same JSON, and
what differs is what finishing one of them asks.
"""

from __future__ import annotations

import json
import random
import re
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any

from . import paths, problemtypes

# Imported as a module rather than looked up by dotted name: `resources.files`
# accepts either, and this way renaming the package cannot leave a stale
# "somepackage.data" string behind to fail at runtime.
from . import data as _data_pkg

DEFAULT_LIST = "neetcode150"

DIFFICULTIES = ("easy", "medium", "hard")

#: What a slug may be. It names a directory under `code/` and `notes/` and a
#: file under `cache/`, so it has to survive being one.
_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]*$")
_SEPARATORS = re.compile(r"[^a-z0-9]+")


@dataclass(frozen=True)
class Problem:
    slug: str
    title: str
    url: str
    difficulty: str
    tags: tuple[str, ...]
    pattern: str | None
    lists: tuple[str, ...]
    #: Which `problemtypes` type it is. Last and defaulted, so everything that
    #: built a `Problem` before there were types still builds a LeetCode one.
    type: str = problemtypes.DEFAULT_TYPE

    @property
    def difficulty_label(self) -> str:
        return self.difficulty.upper()


@dataclass(frozen=True)
class ProblemSet:
    """One list of problems, and the type every problem in it is.

    What a `[sets.<name>]` table in `config.toml` comes to. `path` is None for a
    set bundled with the package, which is found by its name.

    The two numbers are the set's own answer to `session.queue_n` and
    `session.reviews_per_day`, for a set that should not be sized like the
    others: three LeetCode problems is an evening and three system designs is
    not. None follows the session's.
    """

    name: str
    type: str = problemtypes.DEFAULT_TYPE
    path: str | None = None
    queue_n: int | None = None
    reviews_per_day: int | None = None

    @property
    def bundled(self) -> bool:
        return self.path is None

    @property
    def source(self) -> str:
        """Where it is read from, in words: for `doctor` and `seed`."""
        return "bundled" if self.path is None else str(paths.resolve(self.path))


#: The sets that exist before `config.toml` names any.
BUNDLED_SETS = (ProblemSet(DEFAULT_LIST, problemtypes.DEFAULT_TYPE),)


@dataclass(frozen=True)
class SetReport:
    """What seeding one set did. Read by `seed` and by `doctor`."""

    name: str
    type: str
    source: str
    problems: int = 0
    #: Entries that were left out, each with the reason.
    skipped: tuple[str, ...] = ()
    #: The whole set could not be read. Nothing from it was seeded.
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error and not self.skipped


def _row_to_problem(row: sqlite3.Row) -> Problem:
    return Problem(
        slug=row["slug"],
        title=row["title"],
        url=row["url"],
        difficulty=row["difficulty"],
        tags=tuple(json.loads(row["tags"])),
        pattern=row["pattern"],
        lists=tuple(json.loads(row["lists"])),
        type=row["type"] or problemtypes.DEFAULT_TYPE,
    )


def bundled_catalog(name: str = DEFAULT_LIST) -> list[dict]:
    with resources.files(_data_pkg).joinpath(f"{name}.json").open("r") as fh:
        return json.load(fh)


def bundled_sets() -> list[str]:
    """Every list checked in to the package, whether or not anything names it."""
    return sorted(
        p.name.removesuffix(".json")
        for p in resources.files(_data_pkg).iterdir()
        if p.name.endswith(".json")
    )


def slugify(title: str) -> str:
    """A slug for an entry that did not bring one: the title, hyphenated."""
    return _SEPARATORS.sub("-", title.strip().lower()).strip("-")


def _where(url: Any, base: Path | None) -> str:
    """An entry's `url`, which may also be a file beside the set.

    A system design prompt lives wherever you wrote it, and that is as likely to
    be a markdown file next to the list as a page on the web. So a `url` with no
    scheme is a path: `~` is your home, and anything relative is relative to the
    set's own file, which is what lets a set and its prompts be moved together.
    """
    if not isinstance(url, str) or not url.strip():
        return ""
    url = url.strip()
    if "://" in url:
        return url
    path = Path(url).expanduser()
    if not path.is_absolute() and base is not None:
        path = base / path
    return str(path)


def _entry(raw: Any, name: str, base: Path | None) -> tuple[dict | None, str]:
    """One entry of a set, as `(row, "")` or `(None, why not)`.

    Only the title is required. A list you are writing by hand should be a list
    of titles first and everything else as you get to it, and the defaults are
    the ones that keep the entry usable: a slug from the title, `medium`, no
    tags, no pattern, nothing to open.
    """
    if not isinstance(raw, dict):
        return None, "an entry is not a table"
    title = raw.get("title")
    if not isinstance(title, str) or not title.strip():
        return None, f"{raw.get('slug') or 'an entry'}: no title"
    slug = raw.get("slug") or slugify(title)
    if not isinstance(slug, str) or not _SLUG.match(slug):
        return None, f"{title.strip()}: the slug {slug!r} is not lowercase-and-hyphens"

    difficulty = str(raw.get("difficulty") or "medium").lower()
    tags = raw.get("tags")
    lists = raw.get("lists")
    member = [name, *(str(x) for x in lists if x != name)] if isinstance(lists, list) else [name]
    return {
        "slug": slug,
        "title": " ".join(title.split()),
        "url": _where(raw.get("url"), base),
        "difficulty": difficulty if difficulty in DIFFICULTIES else "medium",
        "tags": [str(t) for t in tags] if isinstance(tags, list) else [],
        "pattern": raw.get("pattern") if isinstance(raw.get("pattern"), str) else None,
        "lists": member,
    }, ""


def _read(problem_set: ProblemSet) -> tuple[list[Any], Path | None, str]:
    """A set's entries, the directory they are relative to, and what went wrong."""
    if problem_set.path is None:
        try:
            return bundled_catalog(problem_set.name), None, ""
        except (OSError, ValueError):
            return [], None, f"no list called `{problem_set.name}` is bundled — give it a `path`"
    path = paths.resolve(problem_set.path)
    try:
        entries = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        return [], path.parent, f"{path} could not be read ({exc.strerror or exc})"
    except (ValueError, UnicodeDecodeError) as exc:
        return [], path.parent, f"{path} is not valid JSON ({exc})"
    if not isinstance(entries, list):
        return [], path.parent, f"{path} is not a list of problems"
    return entries, path.parent, ""


_UPSERT = (
    "INSERT INTO problems(slug, title, url, difficulty, tags, pattern, lists, type) "
    "VALUES(:slug, :title, :url, :difficulty, :tags, :pattern, :lists, :type) "
    "ON CONFLICT(slug) DO UPDATE SET "
    "  title = excluded.title, url = excluded.url, difficulty = excluded.difficulty, "
    "  tags = excluded.tags, pattern = excluded.pattern, lists = excluded.lists, "
    "  type = excluded.type"
)


def _stored(row: dict) -> dict:
    return {**row, "tags": json.dumps(row["tags"]), "lists": json.dumps(row["lists"])}


def seed(
    conn: sqlite3.Connection,
    source: Path | None = None,
    name: str = DEFAULT_LIST,
    type: str = problemtypes.DEFAULT_TYPE,
) -> int:
    """Upsert one list. Returns the number of problems in it.

    The by-hand way in, behind `seed --file`. `sync` is what runs on launch, and
    is the one that knows about every set at once.
    """
    # Made absolute against where you are standing, not against the config
    # directory: a path typed at a prompt means what the shell says it means.
    where = str(Path(source).expanduser().resolve()) if source is not None else None
    report = sync(conn, [ProblemSet(name, type, where)])[0]
    if report.error:
        raise ValueError(report.error)
    return report.problems


def sync(conn: sqlite3.Connection, sets: Iterable[ProblemSet]) -> list[SetReport]:
    """Seed every set, in order. Never raises on what a set's file holds.

    Run on every launch, which is what makes adding a set a matter of naming it
    in `config.toml`: there is no second step, and correcting an entry in a list
    you wrote is picked up the next time the app opens.

    The order is the order of precedence. A problem in two sets of the *same*
    type is one problem in two lists -- one card, one history, the first set's
    wording -- which is what `blind75` has always been to `neetcode150`. A slug
    claimed by two sets of *different* types is a collision, and the second one
    is left out and named: `design-twitter` is already a LeetCode problem, and
    letting a system design list take the slug would hand that problem's whole
    history to a different question.

    A set that cannot be read costs that set and nothing else. A broken path in
    `config.toml` must never be the reason a run does not start.
    """
    claimed: dict[str, tuple[str, str]] = {}
    rows: dict[str, dict] = {}
    reports: list[SetReport] = []

    for problem_set in sets:
        entries, base, error = _read(problem_set)
        skipped: list[str] = []
        seeded = 0
        for raw in entries:
            row, why = _entry(raw, problem_set.name, base)
            if row is None:
                skipped.append(why)
                continue
            slug = row["slug"]
            owner = claimed.get(slug)
            if owner is None:
                claimed[slug] = (problem_set.name, problem_set.type)
                rows[slug] = {**row, "type": problem_set.type}
            elif owner[1] != problem_set.type:
                skipped.append(
                    f"{slug}: already a {owner[1]} problem in `{owner[0]}` — "
                    "give this one a slug of its own"
                )
                continue
            else:
                # One problem, another list to find it in. The first set's entry
                # stands, so two lists that word a title differently cannot
                # rename the problem back and forth on alternate launches.
                held = rows[slug]["lists"]
                rows[slug]["lists"] = held + [n for n in row["lists"] if n not in held]
            seeded += 1
        reports.append(
            SetReport(
                name=problem_set.name,
                type=problem_set.type,
                source=problem_set.source,
                problems=seeded,
                skipped=tuple(skipped),
                error=error,
            )
        )

    conn.executemany(_UPSERT, [_stored(row) for row in rows.values()])
    return reports


def complaints(reports: Iterable[SetReport]) -> list[str]:
    """What is wrong with the sets and their types, one line each. Empty is fine.

    For the two places that have to say it: `doctor`, and the app on launch. A
    set that did not load is otherwise silent -- its list is simply short, or
    missing -- and a type that does not exist is quieter still, because its
    problems run, and ask nothing.
    """
    lines: list[str] = []
    seen: set[str] = set()
    for report in reports:
        if report.error:
            lines.append(f"set `{report.name}`: {report.error}")
        elif report.skipped:
            n = len(report.skipped)
            lines.append(
                f"set `{report.name}`: {n} entr{'y' if n == 1 else 'ies'} left out"
                f" — {report.skipped[0]}" + (f" (and {n - 1} more)" if n > 1 else "")
            )
        if report.type in seen:
            continue
        seen.add(report.type)
        ptype = problemtypes.load(report.type)
        lines.extend(f"type `{report.type}`: {problem}" for problem in ptype.problems)
    return lines


def count(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT COUNT(*) AS n FROM problems").fetchone()["n"])


def get(conn: sqlite3.Connection, slug: str) -> Problem | None:
    row = conn.execute("SELECT * FROM problems WHERE slug = ?", (slug,)).fetchone()
    return _row_to_problem(row) if row else None


def all_problems(conn: sqlite3.Connection, active_list: str | None = None) -> list[Problem]:
    rows = conn.execute("SELECT * FROM problems ORDER BY rowid").fetchall()
    problems = [_row_to_problem(r) for r in rows]
    if active_list:
        problems = [p for p in problems if active_list in p.lists]
    return problems


def type_of(conn: sqlite3.Connection, active_list: str | None) -> str:
    """The type a list's problems are.

    Read off the problems rather than off `config.toml`, because a list is not
    always a set: `blind75` is a list inside `neetcode150` and no table names
    it. Every problem in a list is the same type by construction -- `sync` will
    not put one slug in two types -- so the first one answers for all of them.
    """
    for problem in all_problems(conn, active_list):
        return problem.type
    return problemtypes.DEFAULT_TYPE


def pick_random(
    conn: sqlite3.Connection,
    n: int,
    *,
    active_list: str | None = DEFAULT_LIST,
    exclude: set[str] | None = None,
    prefer_unseen: bool = True,
    rng: random.Random | None = None,
) -> list[Problem]:
    """Phase 1 selection: random from the active list.

    Not a scheduler — Phase 2 replaces this with FSRS. It does one cheap thing
    the scheduler will also do: prefer problems you have never attempted, so a
    fresh catalog doesn't hand you the same five problems all week.
    """
    rng = rng or random.Random()
    pool = [p for p in all_problems(conn, active_list) if p.slug not in (exclude or set())]
    if not pool:
        return []

    if prefer_unseen:
        seen = {r["slug"] for r in conn.execute("SELECT DISTINCT slug FROM attempts").fetchall()}
        unseen = [p for p in pool if p.slug not in seen]
        seen_pool = [p for p in pool if p.slug in seen]
        rng.shuffle(unseen)
        rng.shuffle(seen_pool)
        ordered = unseen + seen_pool
    else:
        ordered = list(pool)
        rng.shuffle(ordered)

    return ordered[:n]
