"""The offline cache: whole active list, bounded by bytes, disposable.

Nothing here touches the network. `cache.fetch_question` and the image fetcher
are the only two doors out, and every test closes both.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from core import cache, catalog, paths, queues, scoring, srs
from core.engine import RunEngine

WEIGHTS = scoring.load_weights()
NOW = datetime(2026, 7, 31, 9, 0, tzinfo=timezone.utc)

LISTS = ("neetcode150",)


def _question(slug="two-sum", *, content=None, hints=None, images=0):
    body = content or f"<p>statement for {slug}</p>"
    for i in range(images):
        body += f'<p><img alt="fig" src="https://assets.leetcode.com/{slug}-{i}.png" /></p>'
    return {
        "questionId": "1",
        "title": slug,
        "difficulty": "Easy",
        "isPaidOnly": False,
        "content": body,
        "hints": hints if hints is not None else ["think about a hash map"],
        "exampleTestcases": "[2,7,11,15]\n9",
        "codeSnippets": [
            {"lang": "Python3", "langSlug": "python3", "code": "class Solution:\n    pass"},
            {"lang": "Go", "langSlug": "golang", "code": "func twoSum() {}"},
        ],
    }


@pytest.fixture
def offline(monkeypatch):
    """Stub both network doors. Returns the list of slugs actually requested."""
    requested: list[str] = []

    def fake_question(slug, **kw):
        requested.append(slug)
        return _question(slug)

    monkeypatch.setattr(cache, "fetch_question", fake_question)
    monkeypatch.setattr(cache, "_fetch_bytes", lambda url, **kw: b"\x89PNG-bytes")
    return requested


def _sync(conn, **kw):
    kw.setdefault("lists", LISTS)
    kw.setdefault("budget_bytes", 50 * 1024 * 1024)
    kw.setdefault("pause", 0)
    return cache.sync(conn, now=NOW, **kw)


# --- the page ---------------------------------------------------------------


def test_a_cached_page_needs_nothing_from_the_network(conn, offline):
    """The whole point: one file, no requests left to make."""
    problem = catalog.get(conn, "two-sum")
    page = cache.render_page(problem, _question("two-sum", images=2))

    assert page.count("data:image/png;base64,") == 2
    assert "assets.leetcode.com" not in page
    # The back-link is the one URL allowed to survive — it is for when you land.
    body, _, footer = page.partition("<footer>")
    assert "http" not in body
    assert problem.url in footer


def test_an_image_that_will_not_download_costs_only_the_image(conn, monkeypatch):
    """A broken figure beats a missing problem."""
    monkeypatch.setattr(
        cache, "_fetch_bytes", lambda url, **kw: (_ for _ in ()).throw(cache.FetchError("404"))
    )
    problem = catalog.get(conn, "two-sum")
    page = cache.render_page(problem, _question("two-sum", images=1))

    assert "statement for two-sum" in page
    assert "assets.leetcode.com" in page  # left exactly as it was


def test_the_page_carries_the_hints_and_your_language(conn, offline):
    problem = catalog.get(conn, "two-sum")
    page = cache.render_page(problem, _question(hints=["one", "two"]), language="go")

    assert page.count("<details>") == 2
    assert "func twoSum" in page
    assert "class Solution" not in page


def _payload(question, seen=None):
    def post(url, body, headers, timeout):
        if seen is not None:
            seen.append(headers)
        return {"data": {"question": question}}

    return post


def test_a_problem_with_no_statement_is_an_error_not_half_a_page(monkeypatch, isolated_home):
    monkeypatch.setattr(cache, "_post", _payload({"isPaidOnly": True, "content": None}))
    with pytest.raises(cache.FetchError, match="no session cookie"):
        cache.fetch_question("meeting-rooms")

    monkeypatch.setattr(cache, "_post", _payload({"isPaidOnly": False, "content": None}))
    with pytest.raises(cache.FetchError, match="no statement"):
        cache.fetch_question("two-sum")

    monkeypatch.setattr(cache, "_post", _payload(None))
    with pytest.raises(cache.FetchError, match="not in the public catalog"):
        cache.fetch_question("two-sum")


# --- the session cookie -----------------------------------------------------


def test_premium_is_only_a_wall_when_there_is_no_content(monkeypatch, isolated_home):
    """With a session, `isPaidOnly` comes back *with* a statement — cache it."""
    monkeypatch.setattr(
        cache, "_post", _payload({"isPaidOnly": True, "content": "<p>premium</p>"})
    )
    question = cache.fetch_question("meeting-rooms")
    assert question["content"] == "<p>premium</p>"


def test_the_cookie_is_sent_only_when_there_is_one(monkeypatch, isolated_home):
    seen: list[dict] = []
    monkeypatch.setattr(
        cache, "_post", _payload({"isPaidOnly": False, "content": "<p>x</p>"}, seen)
    )

    cache.fetch_question("two-sum")
    assert "Cookie" not in seen[-1]

    cache.write_session("a-jwt-value")
    cache.fetch_question("two-sum")
    assert seen[-1]["Cookie"] == "LEETCODE_SESSION=a-jwt-value"


def test_the_environment_beats_the_file(monkeypatch, isolated_home):
    cache.write_session("from-the-file")
    assert cache.session_cookie() == "from-the-file"

    monkeypatch.setenv(cache.ENV_SESSION, "from-the-environment")
    assert cache.session_cookie() == "from-the-environment"


def test_the_session_file_is_not_readable_by_anyone_else(isolated_home):
    """A credential written world-readable is a credential you have leaked."""
    path = cache.write_session("a-jwt-value")
    assert path.stat().st_mode & 0o777 == 0o600
    assert not cache.session_is_exposed()

    path.chmod(0o644)
    assert cache.session_is_exposed()


def test_an_expired_session_says_so_rather_than_blaming_the_paywall(
    monkeypatch, isolated_home
):
    cache.write_session("stale")
    monkeypatch.setattr(cache, "_post", _payload({"isPaidOnly": True, "content": None}))
    with pytest.raises(cache.FetchError, match="session expired or missing"):
        cache.fetch_question("meeting-rooms")


def test_no_session_means_no_session(isolated_home):
    assert cache.session_cookie() is None


# --- priority ---------------------------------------------------------------


def test_priority_covers_the_whole_list(conn):
    """No selection: every problem in the active lists is a target."""
    order = cache.priority(conn, LISTS)
    assert len(order) == len(catalog.all_problems(conn, "neetcode150"))
    assert len({p.slug for p in order}) == len(order)


def test_todays_queue_leads_the_priority_order(conn):
    queue = queues.ensure(conn, n=5, active_list="neetcode150", weights=WEIGHTS, now=NOW)
    order = [p.slug for p in cache.priority(conn, LISTS, now=NOW)]
    assert order[: len(queue.slugs)] == queue.slugs


def test_warming_the_cache_never_generates_a_queue(conn):
    """A cache warm must not write history — `queues.ensure` appends an event."""
    before = conn.execute("SELECT COUNT(*) AS n FROM events").fetchone()["n"]
    cache.priority(conn, LISTS)
    after = conn.execute("SELECT COUNT(*) AS n FROM events").fetchone()["n"]
    assert after == before
    assert conn.execute("SELECT COUNT(*) AS n FROM queues").fetchone()["n"] == 0


def test_cards_outrank_problems_never_seen(conn):
    eng = RunEngine(conn)
    eng.start_session(["valid-anagram"])
    eng.start_problem("valid-anagram")
    eng.finish("accepted")
    eng.advance()
    eng.end_session()

    order = [p.slug for p in cache.priority(conn, LISTS, now=NOW)]
    card_slugs = {row["slug"] for row in srs.cards_by_due(conn)}
    assert card_slugs
    assert order.index("valid-anagram") < min(
        i for i, slug in enumerate(order) if slug not in card_slugs
    )


# --- the sweep --------------------------------------------------------------


def test_sync_caches_the_whole_list_and_is_idempotent(conn, offline):
    total = len(catalog.all_problems(conn, "neetcode150"))
    first = _sync(conn)

    assert len(first.fetched) == total
    assert not first.failures and not first.skipped
    assert len(list(paths.cache_dir().glob("*.html"))) == total

    offline.clear()
    second = _sync(conn)
    assert offline == []  # not one request
    assert len(second.kept) == total
    assert not second.fetched


def test_refresh_refetches_what_is_already_there(conn, offline):
    _sync(conn)
    offline.clear()
    report = _sync(conn, refresh=True)
    assert len(report.fetched) == len(offline) > 0


def test_one_dead_problem_does_not_cost_the_other_149(conn, monkeypatch):
    def fake_question(slug, **kw):
        if slug == "two-sum":
            raise cache.FetchError("premium only")
        return _question(slug)

    monkeypatch.setattr(cache, "fetch_question", fake_question)
    monkeypatch.setattr(cache, "_fetch_bytes", lambda url, **kw: b"png")

    report = _sync(conn)
    total = len(catalog.all_problems(conn, "neetcode150"))
    assert report.failures == {"two-sum": "premium only"}
    assert len(report.fetched) == total - 1
    assert cache.local_path("two-sum") is None
    assert cache.load_manifest().failures == {"two-sum": "premium only"}


def test_the_budget_truncates_in_priority_order_instead_of_failing(conn, offline):
    queue = queues.ensure(conn, n=3, active_list="neetcode150", weights=WEIGHTS, now=NOW)
    report = _sync(conn, budget_bytes=4096)

    assert report.fetched  # the front of the queue landed
    assert report.skipped  # the tail did not
    assert report.total_bytes <= 4096
    # What survived is what mattered most, not whatever came first alphabetically.
    assert queue.slugs[0] in report.fetched


def test_a_shrunk_budget_prunes_rather_than_accumulates(conn, offline):
    _sync(conn)
    assert len(list(paths.cache_dir().glob("*.html"))) > 10

    report = _sync(conn, budget_bytes=4096)
    assert report.pruned
    on_disk = {p.stem for p in paths.cache_dir().glob("*.html")}
    assert on_disk == set(report.kept) | set(report.fetched)


def test_the_cache_is_disposable(conn, offline):
    """Deleting the directory is a complete reset: no stale rows anywhere."""
    _sync(conn)
    for path in paths.cache_dir().glob("*"):
        path.unlink()

    assert cache.load_manifest().problems == {}
    status = cache.status(conn, lists=LISTS, budget_bytes=1024)
    assert status.cached == 0
    assert not status.complete


def test_a_corrupt_manifest_is_not_an_error(conn, offline):
    _sync(conn)
    paths.cache_manifest().write_text("{ not json")

    assert cache.load_manifest().problems == {}
    # The files are the truth, so the status is still right.
    assert cache.status(conn, lists=LISTS, budget_bytes=1024).cached > 0


# --- resolution -------------------------------------------------------------


def test_online_o_still_opens_leetcode(conn, offline):
    _sync(conn)
    problem = catalog.get(conn, "two-sum")
    target, is_local = cache.target_for(problem, offline=False)
    assert (target, is_local) == (problem.url, False)


def test_offline_o_opens_the_cached_file(conn, offline):
    _sync(conn)
    problem = catalog.get(conn, "two-sum")
    target, is_local = cache.target_for(problem, offline=True)

    assert is_local
    assert target.startswith("file://")
    assert target.endswith("two-sum.html")


def test_offline_with_nothing_cached_falls_back_to_the_url(conn):
    problem = catalog.get(conn, "two-sum")
    assert cache.target_for(problem, offline=True) == (problem.url, False)
