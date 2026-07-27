"""Problem catalog (spec §1: title, slug, url, difficulty, tags — never content).

Seeded from a checked-in JSON list. Re-seeding is an upsert, so shipping a
corrected or extended list is safe: it never touches attempt history.
"""

from __future__ import annotations

import json
import random
import sqlite3
from dataclasses import dataclass
from importlib import resources
from pathlib import Path

# Imported as a module rather than looked up by dotted name: `resources.files`
# accepts either, and this way renaming the package cannot leave a stale
# "somepackage.data" string behind to fail at runtime.
from . import data as _data_pkg

DEFAULT_LIST = "neetcode150"


@dataclass(frozen=True)
class Problem:
    slug: str
    title: str
    url: str
    difficulty: str
    tags: tuple[str, ...]
    pattern: str | None
    lists: tuple[str, ...]

    @property
    def difficulty_label(self) -> str:
        return self.difficulty.upper()


def _row_to_problem(row: sqlite3.Row) -> Problem:
    return Problem(
        slug=row["slug"],
        title=row["title"],
        url=row["url"],
        difficulty=row["difficulty"],
        tags=tuple(json.loads(row["tags"])),
        pattern=row["pattern"],
        lists=tuple(json.loads(row["lists"])),
    )


def bundled_catalog(name: str = DEFAULT_LIST) -> list[dict]:
    with resources.files(_data_pkg).joinpath(f"{name}.json").open("r") as fh:
        return json.load(fh)


def seed(conn: sqlite3.Connection, source: Path | None = None, name: str = DEFAULT_LIST) -> int:
    """Upsert the catalog. Returns the number of problems in the list."""
    entries = json.loads(source.read_text()) if source else bundled_catalog(name)
    conn.executemany(
        "INSERT INTO problems(slug, title, url, difficulty, tags, pattern, lists) "
        "VALUES(:slug, :title, :url, :difficulty, :tags, :pattern, :lists) "
        "ON CONFLICT(slug) DO UPDATE SET "
        "  title = excluded.title, url = excluded.url, difficulty = excluded.difficulty, "
        "  tags = excluded.tags, pattern = excluded.pattern, lists = excluded.lists",
        [
            {
                "slug": e["slug"],
                "title": e["title"],
                "url": e["url"],
                "difficulty": e["difficulty"],
                "tags": json.dumps(e.get("tags", [])),
                "pattern": e.get("pattern"),
                "lists": json.dumps(e.get("lists", [name])),
            }
            for e in entries
        ],
    )
    return len(entries)


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
