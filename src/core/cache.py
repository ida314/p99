"""Offline problem statements — the cache behind `<command> fetch`.

A LeetCode problem page is a React shell that fetches its own content, so
saving the HTML gets you an empty document. The content comes from the public
GraphQL endpoint, which answers unauthenticated given a `Referer` header, and
that is what this module downloads: one self-contained page per problem, images
inlined as `data:` URIs, no request left to make at 35,000 feet.

Three rules the rest of the design falls out of:

  * **The whole active list, never a subset.** The queue cannot tell you what
    you will need tomorrow -- `queues.generate` reads live card state, and cards
    move as you solve -- and `catalog.pick_random` can hand you anything in the
    list. A partial cache fails exactly when nothing can be done about it. The
    measured cost of not choosing is ~1.5 MB for all 150 problems.
  * **Bounded by bytes, not by count.** `[cache] max_mb` is the ceiling; the
    priority order below decides what survives it. Today it never binds. It is
    here so that pointing this at a 3,000-problem catalog degrades in a defined
    order instead of filling the disk.
  * **Nothing here is authoritative.** No events, no projections, no rows in the
    database. The manifest lives inside the cache directory, so `rm -rf` on it
    is a complete reset and `<command> replay` has nothing to say about any of
    this. The files on disk are the truth; the manifest is bookkeeping.
"""

from __future__ import annotations

import base64
import json
import mimetypes
import os
import re
import sqlite3
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from html import escape
from pathlib import Path
from typing import Any

from . import branding, catalog, paths, queues, srs
from .catalog import Problem

GRAPHQL_URL = "https://leetcode.com/graphql"
PROBLEM_URL = "https://leetcode.com/problems/{slug}/"

#: The endpoint 403s a bare urllib agent, and answers a browser-shaped one.
USER_AGENT = "Mozilla/5.0"
TIMEOUT = 20.0

#: Between requests. 150 sequential problems is a minute either way; there is no
#: reason to be the rudest client LeetCode sees today.
PAUSE_SECONDS = 0.35

QUESTION_QUERY = """
query offlineQuestion($titleSlug: String!) {
  question(titleSlug: $titleSlug) {
    questionId
    title
    difficulty
    isPaidOnly
    content
    hints
    exampleTestcases
    codeSnippets { lang langSlug code }
  }
}
"""

#: `config.EXT_BY_LANGUAGE` keys to LeetCode's `langSlug`, for picking the
#: starter snippet worth embedding. Anything unmapped falls back to whatever
#: the response lists first, which is better than no snippet.
LEETCODE_LANG = {
    "python": "python3",
    "go": "golang",
    "rust": "rust",
    "c": "c",
    "cpp": "cpp",
    "c++": "cpp",
    "java": "java",
    "javascript": "javascript",
    "typescript": "typescript",
    "ruby": "ruby",
    "kotlin": "kotlin",
    "swift": "swift",
    "scala": "scala",
    "csharp": "csharp",
    "sql": "mysql",
}

MANIFEST_VERSION = 1


class FetchError(RuntimeError):
    """One problem could not be downloaded. Never fatal to a sweep."""


# --- the session cookie (premium problems) ---------------------------------

ENV_SESSION = branding.env("LEETCODE_SESSION")


def session_cookie() -> str | None:
    """Your `LEETCODE_SESSION` cookie, if you have set one up.

    Without it, premium problems come back `isPaidOnly` with a null `content`
    and there is nothing to cache. With it they download like any other.

    Two sources, environment first: `<PREFIX>_LEETCODE_SESSION`, then the
    `session` file. Deliberately neither `config.toml` nor the settings table --
    see `paths.session_file`. The value is never logged, never printed and never
    written into a cached page; `doctor` reports only that one exists.
    """
    raw = os.environ.get(ENV_SESSION)
    if raw and raw.strip():
        return raw.strip()
    path = paths.session_file()
    try:
        stored = path.read_text().strip()
    except OSError:
        return None
    return stored or None


def session_is_exposed() -> bool:
    """True if the session file is readable by anyone but you."""
    path = paths.session_file()
    try:
        return bool(path.stat().st_mode & 0o077)
    except OSError:
        return False


def write_session(value: str) -> Path:
    """Store the cookie at 0600, creating the file with those bits from the start."""
    path = paths.session_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    # Opened by descriptor with the mode up front: writing then chmod-ing would
    # leave the value world-readable for the moment in between.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        handle.write(value.strip() + "\n")
    os.chmod(path, 0o600)
    return path


# --- fetching --------------------------------------------------------------


def _post(url: str, payload: dict[str, Any], headers: dict[str, str], timeout: float) -> Any:
    body = json.dumps(payload).encode()
    request = urllib.request.Request(url, body, {"Content-Type": "application/json", **headers})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read())


def fetch_question(slug: str, *, timeout: float = TIMEOUT) -> dict[str, Any]:
    """One problem's content from the GraphQL endpoint.

    Sends the session cookie when there is one, which is the whole difference
    between caching a premium problem and skipping it.

    Raises `FetchError` for anything that leaves us without a usable statement.
    Note the order: `isPaidOnly` is not itself a failure. With a valid session a
    premium problem returns full content, so the test that matters is whether
    there is a statement -- the paid flag only shapes the message.
    """
    headers = {"User-Agent": USER_AGENT, "Referer": PROBLEM_URL.format(slug=slug)}
    cookie = session_cookie()
    if cookie:
        headers["Cookie"] = f"LEETCODE_SESSION={cookie}"

    try:
        payload = _post(
            GRAPHQL_URL,
            {"query": QUESTION_QUERY, "variables": {"titleSlug": slug}},
            headers,
            timeout,
        )
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise FetchError(f"{type(exc).__name__}: {exc}") from exc

    question = (payload.get("data") or {}).get("question")
    if not question:
        raise FetchError("not in the public catalog")
    if not question.get("content"):
        if question.get("isPaidOnly"):
            raise FetchError(
                "premium only — session expired or missing"
                if cookie
                else "premium only — no session cookie"
            )
        raise FetchError("no statement returned")
    return question


def _fetch_bytes(url: str, *, timeout: float = TIMEOUT) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read()
    except (urllib.error.URLError, OSError) as exc:
        raise FetchError(f"{type(exc).__name__}: {exc}") from exc


IMG_SRC_RE = re.compile(r'(<img\b[^>]*?\bsrc=")([^"]+)(")', re.IGNORECASE)


def inline_images(html: str, *, timeout: float = TIMEOUT) -> str:
    """Rewrite every `<img src>` to a `data:` URI.

    About half the catalog embeds one to three images from `assets.leetcode.com`
    and they are the only thing standing between a cached page and a page that
    works. An image that will not download leaves its tag alone: a problem with
    a broken figure still beats no problem at all.
    """

    def replace(match: re.Match[str]) -> str:
        url = match.group(2)
        if url.startswith("data:"):
            return match.group(0)
        try:
            raw = _fetch_bytes(url, timeout=timeout)
        except FetchError:
            return match.group(0)
        kind = mimetypes.guess_type(url)[0] or "image/png"
        encoded = base64.b64encode(raw).decode("ascii")
        return f"{match.group(1)}data:{kind};base64,{encoded}{match.group(3)}"

    return IMG_SRC_RE.sub(replace, html)


# --- rendering -------------------------------------------------------------

PAGE_CSS = """
:root { color-scheme: light dark; }
* { box-sizing: border-box; }
body {
  margin: 0 auto; padding: 2.5rem 1.25rem 6rem; max-width: 46rem;
  font: 16px/1.65 -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
  background: #ffffff; color: #1b1b1f;
}
header { border-bottom: 1px solid #e3e3e8; padding-bottom: 1rem; margin-bottom: 1.75rem; }
h1 { font-size: 1.6rem; margin: 0 0 .5rem; }
.meta { font-size: .82rem; color: #6b6b76; display: flex; gap: .6rem; flex-wrap: wrap; }
.difficulty { font-weight: 600; text-transform: uppercase; letter-spacing: .04em; }
.easy { color: #1a7f37; } .medium { color: #9a6700; } .hard { color: #cf222e; }
pre {
  background: #f5f5f7; border: 1px solid #e3e3e8; border-radius: 6px;
  padding: .85rem 1rem; overflow-x: auto; font-size: .88rem; line-height: 1.5;
}
code { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: .92em; }
img { max-width: 100%; height: auto; border-radius: 4px; }
details { margin: .4rem 0; border: 1px solid #e3e3e8; border-radius: 6px; padding: .6rem .9rem; }
summary { cursor: pointer; font-weight: 600; font-size: .92rem; }
section { margin-top: 2.5rem; }
section > h2 { font-size: .78rem; text-transform: uppercase; letter-spacing: .08em;
               color: #6b6b76; margin: 0 0 .75rem; }
footer { margin-top: 3rem; padding-top: 1rem; border-top: 1px solid #e3e3e8;
         font-size: .8rem; color: #6b6b76; }
a { color: #0969da; }
@media (prefers-color-scheme: dark) {
  body { background: #16161a; color: #e6e6ea; }
  header, footer, pre, details { border-color: #2c2c33; }
  pre, details { background: #1d1d22; }
  .meta, section > h2, footer { color: #9a9aa5; }
  .easy { color: #3fb950; } .medium { color: #d29922; } .hard { color: #f85149; }
  a { color: #58a6ff; }
}
"""


def _snippet(question: dict[str, Any], language: str) -> tuple[str, str] | None:
    snippets = question.get("codeSnippets") or []
    if not snippets:
        return None
    wanted = LEETCODE_LANG.get((language or "").lower())
    for snippet in snippets:
        if snippet.get("langSlug") == wanted:
            return snippet.get("lang") or wanted, snippet.get("code") or ""
    first = snippets[0]
    return first.get("lang") or first.get("langSlug") or "", first.get("code") or ""


def render_page(problem: Problem, question: dict[str, Any], *, language: str = "python") -> str:
    """One self-contained HTML document. No request left to make.

    Everything the browser needs is in the file: the statement with its images
    inlined, the official hints folded away behind `<details>` so they are not
    read by accident, and the starter snippet for your configured language. The
    only URL is the back-link, which is there for when you land.
    """
    difficulty = (problem.difficulty or "").lower()
    parts = [
        "<!doctype html>",
        '<html lang="en"><head><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        f"<title>{escape(problem.title)}</title>",
        f"<style>{PAGE_CSS}</style>",
        "</head><body>",
        "<header>",
        f"<h1>{escape(problem.title)}</h1>",
        '<div class="meta">',
        f'<span class="difficulty {escape(difficulty)}">{escape(problem.difficulty_label)}</span>',
        f"<span>{escape(problem.pattern or '—')}</span>",
        f"<span>{escape(', '.join(problem.tags))}</span>",
        "</div></header>",
        inline_images(question["content"]),
    ]

    hints = [h for h in (question.get("hints") or []) if h]
    if hints:
        parts.append("<section><h2>hints</h2>")
        for index, hint in enumerate(hints, start=1):
            parts.append(
                f"<details><summary>hint {index}</summary><div>{hint}</div></details>"
            )
        parts.append("</section>")

    snippet = _snippet(question, language)
    if snippet and snippet[1]:
        name, code = snippet
        parts.append(
            f"<section><h2>starter — {escape(name)}</h2>"
            f"<pre><code>{escape(code)}</code></pre></section>"
        )

    examples = question.get("exampleTestcases")
    if examples:
        parts.append(
            f"<section><h2>example input</h2><pre><code>{escape(examples)}</code></pre></section>"
        )

    parts.append(
        f'<footer>cached offline · <a href="{escape(problem.url)}">{escape(problem.url)}</a>'
        "</footer></body></html>"
    )
    return "\n".join(parts)


# --- the manifest ----------------------------------------------------------


@dataclass(frozen=True)
class Entry:
    slug: str
    bytes: int
    fetched_at: str
    hints: int = 0


@dataclass
class Manifest:
    lists: tuple[str, ...] = ()
    fetched_at: str | None = None
    problems: dict[str, Entry] = field(default_factory=dict)
    failures: dict[str, str] = field(default_factory=dict)


def load_manifest() -> Manifest:
    """The manifest, or an empty one. A corrupt manifest is not an error.

    The files on disk carry the real state, so the worst a lost manifest costs
    is a `fetched_at` -- never a re-download of something already cached.
    """
    path = paths.cache_manifest()
    if not path.exists():
        return Manifest()
    try:
        raw = json.loads(path.read_text())
    except (OSError, ValueError):
        return Manifest()
    if not isinstance(raw, dict):
        return Manifest()
    problems = {}
    for slug, entry in (raw.get("problems") or {}).items():
        if isinstance(entry, dict):
            problems[slug] = Entry(
                slug=slug,
                bytes=int(entry.get("bytes") or 0),
                fetched_at=str(entry.get("fetched_at") or ""),
                hints=int(entry.get("hints") or 0),
            )
    return Manifest(
        lists=tuple(raw.get("lists") or ()),
        fetched_at=raw.get("fetched_at"),
        problems=problems,
        failures=dict(raw.get("failures") or {}),
    )


def save_manifest(manifest: Manifest) -> None:
    """Write atomically, so an interrupted sweep cannot leave half a manifest."""
    path = paths.cache_manifest()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": MANIFEST_VERSION,
        "lists": list(manifest.lists),
        "fetched_at": manifest.fetched_at,
        "problems": {
            slug: {"bytes": e.bytes, "fetched_at": e.fetched_at, "hints": e.hints}
            for slug, e in sorted(manifest.problems.items())
        },
        "failures": dict(sorted(manifest.failures.items())),
    }
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=False) + "\n")
    os.replace(tmp, path)


# --- what to cache, in what order ------------------------------------------


def catalog_for(conn: sqlite3.Connection, lists: Sequence[str]) -> dict[str, Problem]:
    """Every problem in the active lists, deduplicated, in catalog order."""
    problems: dict[str, Problem] = {}
    for name in lists:
        for problem in catalog.all_problems(conn, name):
            problems.setdefault(problem.slug, problem)
    return problems


def priority(
    conn: sqlite3.Connection, lists: Sequence[str], *, now: datetime | None = None
) -> list[Problem]:
    """Everything worth caching, most valuable first.

    Only matters when the budget binds, which today it does not -- but the order
    is what makes truncation defensible rather than arbitrary, so it is computed
    every time and tested.

    Reads today's queue if one already exists; deliberately does *not* call
    `queues.ensure`, because generating a queue appends a `queue_generated`
    event and warming a cache must never write history as a side effect.
    """
    problems = catalog_for(conn, lists)
    ordered: list[Problem] = []
    seen: set[str] = set()

    def add(slug: str) -> None:
        problem = problems.get(slug)
        if problem is None or slug in seen:
            return
        seen.add(slug)
        ordered.append(problem)

    queue = queues.load(conn, queues.today(now), now=now)
    if queue is not None:
        for slug in queue.slugs:
            add(slug)

    for row in srs.cards_by_due(conn):
        add(row["slug"])

    # Catalog order is nine arrays-hashing problems in a row, so a truncated
    # prefix of it would be a cache you cannot interleave out of. The spread is
    # the same one the queue's candidate pool uses, and for the same reason.
    for problem in queues.spread_by_pattern(list(problems.values())):
        add(problem.slug)

    return ordered


# --- the sweep -------------------------------------------------------------


@dataclass
class SyncReport:
    fetched: list[str] = field(default_factory=list)
    kept: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    pruned: list[str] = field(default_factory=list)
    failures: dict[str, str] = field(default_factory=dict)
    total_bytes: int = 0
    budget_bytes: int = 0
    cancelled: bool = False

    @property
    def over_budget(self) -> bool:
        return bool(self.skipped)


def sync(
    conn: sqlite3.Connection,
    *,
    lists: Sequence[str],
    budget_bytes: int,
    language: str = "python",
    refresh: bool = False,
    on_progress: Callable[[str, str], None] | None = None,
    stop: Callable[[], bool] | None = None,
    pause: float | None = None,
    now: datetime | None = None,
) -> SyncReport:
    """Bring the cache in line with the active lists. Idempotent and resumable.

    Thin wrapper: the database is read once, here, and everything after it is
    network and disk. That split is what lets the TUI compute the order on the
    thread that owns the connection and run the sweep itself on another --
    see `sync_problems`.
    """
    now = now or datetime.now(timezone.utc)
    return sync_problems(
        priority(conn, lists, now=now),
        lists=lists,
        budget_bytes=budget_bytes,
        language=language,
        refresh=refresh,
        on_progress=on_progress,
        stop=stop,
        pause=pause,
        now=now,
    )


def sync_problems(
    problems: Sequence[Problem],
    *,
    lists: Sequence[str],
    budget_bytes: int,
    language: str = "python",
    refresh: bool = False,
    on_progress: Callable[[str, str], None] | None = None,
    stop: Callable[[], bool] | None = None,
    pause: float | None = None,
    now: datetime | None = None,
) -> SyncReport:
    """The sweep itself, over an order someone else computed. No database.

    Idempotent and resumable. A problem already on disk is kept without a
    request unless `refresh`. A problem that will not download is recorded and
    the sweep continues -- one dead slug must not cost you the other 149. A
    problem that would breach the budget is skipped rather than ending the walk,
    so smaller problems further down the priority order still land.

    `stop` is polled once per problem and is how the TUI's escape key gets out
    of a sweep it cannot interrupt mid-request. A cancelled sweep keeps every
    page it managed to write and prunes nothing -- see the walk below.

    `pause` resolves at call time rather than in the signature, so a test can
    set `PAUSE_SECONDS` to zero and have it mean something.
    """
    now = now or datetime.now(timezone.utc)
    pause = PAUSE_SECONDS if pause is None else pause
    stamp = now.isoformat(timespec="seconds")
    report = SyncReport(budget_bytes=budget_bytes)
    manifest = load_manifest()

    paths.cache_dir().mkdir(parents=True, exist_ok=True)
    on_disk = {path.stem for path in paths.cache_dir().glob("*.html")}
    keeping: set[str] = set()

    def note(slug: str, state: str) -> None:
        if on_progress is not None:
            on_progress(slug, state)

    for problem in problems:
        if stop is not None and stop():
            report.cancelled = True
            break

        slug = problem.slug
        path = paths.cache_path(slug)

        if not refresh and path.exists():
            size = path.stat().st_size
            if report.total_bytes + size > budget_bytes:
                report.skipped.append(slug)
                note(slug, "over budget")
                continue
            report.total_bytes += size
            keeping.add(slug)
            report.kept.append(slug)
            note(slug, "cached")
            continue

        if report.total_bytes >= budget_bytes:
            report.skipped.append(slug)
            note(slug, "over budget")
            continue

        try:
            question = fetch_question(slug)
        except FetchError as exc:
            report.failures[slug] = str(exc)
            manifest.problems.pop(slug, None)
            note(slug, f"failed — {exc}")
            if pause:
                time.sleep(pause)
            continue

        page = render_page(problem, question, language=language).encode()
        if report.total_bytes + len(page) > budget_bytes:
            report.skipped.append(slug)
            note(slug, "over budget")
            if pause:
                time.sleep(pause)
            continue

        path.write_bytes(page)
        report.total_bytes += len(page)
        keeping.add(slug)
        report.fetched.append(slug)
        manifest.problems[slug] = Entry(
            slug=slug,
            bytes=len(page),
            fetched_at=stamp,
            hints=len([h for h in (question.get("hints") or []) if h]),
        )
        note(slug, "fetched")
        if pause:
            time.sleep(pause)

    # Anything on disk we are no longer keeping: a shrunk budget, a changed
    # list, a problem dropped from the catalog. The cache tracks the target set
    # rather than accumulating every problem ever fetched.
    #
    # Never after a cancel: `keeping` then holds only the problems the walk got
    # to, so pruning against it would read a stopped sweep as "the rest are
    # stale" and delete a cache the user was in the middle of filling. Same
    # reason the manifest keeps its old `fetched_at` and failures below --
    # a partial sweep is not a sweep, and only the pages actually written are
    # news.
    if not report.cancelled:
        for stale in sorted(on_disk - keeping):
            paths.cache_path(stale).unlink(missing_ok=True)
            manifest.problems.pop(stale, None)
            report.pruned.append(stale)
        manifest.lists = tuple(lists)
        manifest.fetched_at = stamp
        manifest.failures = dict(report.failures)
    save_manifest(manifest)
    return report


# --- reading the cache -----------------------------------------------------


@dataclass(frozen=True)
class Status:
    cached: int
    total: int
    bytes: int
    budget_bytes: int
    fetched_at: str | None
    failures: dict[str, str]

    @property
    def complete(self) -> bool:
        """Everything cacheable is cached.

        Failures count as done. Seven neetcode150 entries are premium-only and
        can never be fetched -- nor opened, so they are a catalog problem, not a
        cache one. A warning that can never be cleared is a warning you learn to
        ignore; the slugs are still named underneath.
        """
        return self.total > 0 and self.cached + len(self.failures) >= self.total


def status(conn: sqlite3.Connection, *, lists: Sequence[str], budget_bytes: int) -> Status:
    """Cache state for `doctor`. Counts files, not manifest rows."""
    manifest = load_manifest()
    slugs = catalog_for(conn, lists).keys()
    cached = 0
    size = 0
    for slug in slugs:
        path = paths.cache_path(slug)
        if path.exists():
            cached += 1
            size += path.stat().st_size
    return Status(
        cached=cached,
        total=len(slugs),
        bytes=size,
        budget_bytes=budget_bytes,
        fetched_at=manifest.fetched_at,
        failures=dict(manifest.failures),
    )


def local_path(slug: str) -> Path | None:
    path = paths.cache_path(slug)
    return path if path.exists() else None


def target_for(problem: Problem, *, offline: bool) -> tuple[str, bool]:
    """What `o` should open, and whether it resolved locally.

    Offline is an explicit setting, never a guess: a captive-portal network
    answers DNS and resolves nothing, so any auto-detection would be wrong at
    exactly the moment it mattered.
    """
    if offline:
        path = local_path(problem.slug)
        if path is not None:
            return path.resolve().as_uri(), True
    return problem.url, False


def fmt_bytes(count: int) -> str:
    if count < 1024:
        return f"{count} B"
    if count < 1024 * 1024:
        return f"{count / 1024:.0f} KB"
    return f"{count / 1024 / 1024:.1f} MB"
