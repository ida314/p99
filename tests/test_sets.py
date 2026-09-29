"""Problem sets: lists you name in the config, and the type each one is."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

import pytest

from core import cache, catalog, cli, config, db, events, paths, queues, scoring, stats
from core.catalog import ProblemSet
from core.engine import RunEngine

WEIGHTS = scoring.load_weights()
NOW = datetime(2026, 7, 31, 9, 0, tzinfo=timezone.utc)

DESIGNS = ProblemSet("system-design", "system-design")


def _write_config(text: str) -> None:
    paths.config_file().parent.mkdir(parents=True, exist_ok=True)
    paths.config_file().write_text(text)


def _write_set(name: str, entries: list) -> str:
    """A set beside the config, the way the comment in config.toml suggests."""
    target = paths.config_dir() / "sets" / f"{name}.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(entries))
    return f"sets/{name}.json"


def _solve(conn, slug, verdict="solved_unaided", **kw):
    eng = RunEngine(conn)
    eng.start_session([slug])
    eng.start_problem(slug)
    eng.finish(verdict, **kw)
    eng.advance()
    eng.end_session()


# --- the config ------------------------------------------------------------


def test_the_bundled_set_is_there_before_the_config_names_any(conn):
    cfg = config.load(conn)
    assert cfg.sets == catalog.BUNDLED_SETS
    assert cfg.lists == ("neetcode150", "blind75")


def test_a_set_is_a_table_in_the_config(conn):
    _write_config(
        '[sets.system-design]\ntype = "system-design"\nqueue_n = 1\n\n'
        '[sets.mine]\ntype = "leetcode"\npath = "~/lists/mine.json"\n'
    )
    cfg = config.load(conn)
    assert [s.name for s in cfg.sets] == ["neetcode150", "system-design", "mine"]
    assert cfg.set_named("system-design") == ProblemSet(
        "system-design", "system-design", queue_n=1
    )
    assert cfg.set_named("mine").path == "~/lists/mine.json"
    assert cfg.lists == ("neetcode150", "blind75", "system-design", "mine")


def test_a_set_has_its_own_queue_size_or_follows_the_sessions(conn):
    _write_config(
        "[session]\nqueue_n = 4\nreviews_per_day = 2\n\n"
        '[sets.system-design]\ntype = "system-design"\nqueue_n = 1\nreviews_per_day = 0\n'
    )
    cfg = config.load(conn)
    assert cfg.queue_n_for("system-design") == 1
    assert cfg.reviews_per_day_for("system-design") == 0
    assert cfg.queue_n_for("neetcode150") == 4
    assert cfg.reviews_per_day_for("neetcode150") == 2
    # A list that is not a set has no table to have numbers in.
    assert cfg.queue_n_for("blind75") == 4


def test_a_set_that_makes_no_sense_is_skipped_not_raised_on(conn):
    """A broken config must never stop a run."""
    _write_config(
        '[sets]\nnonsense = 3\n\n[sets.ok]\ntype = "leetcode"\nqueue_n = "lots"\n'
    )
    cfg = config.load(conn)
    assert [s.name for s in cfg.sets] == ["neetcode150", "ok"]
    assert cfg.set_named("ok").queue_n is None


def test_the_bundled_set_can_be_pointed_at_your_own_copy(conn):
    _write_config('[sets.neetcode150]\npath = "sets/corrected.json"\n')
    cfg = config.load(conn)
    assert [s.name for s in cfg.sets] == ["neetcode150"]  # replaced in place
    assert cfg.sets[0].path == "sets/corrected.json"


def test_a_set_is_a_list_the_settings_screen_can_pick(conn):
    _write_config('[sets.system-design]\ntype = "system-design"\n')
    option = {o.key: o for o in config.options()}["session.active_list"]
    assert option.choices == ("neetcode150", "blind75", "system-design")

    config.set_option(conn, "session.active_list", "system-design")
    assert config.load(conn).session.active_list == "system-design"


def test_a_set_taken_out_of_the_config_stops_being_the_active_list(conn):
    """The settings table outlives any one version of the config."""
    _write_config('[sets.system-design]\ntype = "system-design"\n')
    config.set_option(conn, "session.active_list", "system-design")
    _write_config("")
    assert config.load(conn).session.active_list == "neetcode150"


def test_the_written_config_explains_sets(conn):
    config.write_default_config()
    text = paths.config_file().read_text()
    assert "[sets.system-design]" in text
    # Commented out: writing the file must not add a set nobody asked for.
    assert config.load().sets == catalog.BUNDLED_SETS


def test_the_offline_cache_covers_only_what_can_be_fetched(conn):
    _write_config('[sets.system-design]\ntype = "system-design"\n')
    cfg = config.load(conn)
    assert cfg.active_lists == ("neetcode150",)
    config.set_option(conn, "session.active_list", "system-design")
    # The active list has nothing to download, so it is not on the list at all.
    assert config.load(conn).active_lists == ("neetcode150",)


# --- seeding ---------------------------------------------------------------


def test_a_set_is_seeded_with_its_type(conn):
    report = catalog.sync(conn, [DESIGNS])[0]
    assert report.ok and report.problems == 12 and report.source == "bundled"
    problem = catalog.get(conn, "rate-limiter")
    assert problem.type == "system-design"
    assert problem.lists == ("system-design",)
    assert catalog.type_of(conn, "system-design") == "system-design"
    # And the ones that were already there are what they were.
    assert catalog.get(conn, "two-sum").type == "leetcode"
    assert catalog.type_of(conn, "blind75") == "leetcode"


def test_a_path_is_relative_to_the_config(conn):
    path = _write_set("mine", [{"title": "Design a parking lot", "difficulty": "easy"}])
    report = catalog.sync(conn, [ProblemSet("mine", "system-design", path)])[0]
    assert report.ok and report.problems == 1
    assert report.source == str(paths.config_dir() / "sets" / "mine.json")


def test_only_the_title_is_required(conn):
    path = _write_set("mine", [{"title": "  Design a   Parking Lot "}])
    catalog.sync(conn, [ProblemSet("mine", "system-design", path)])
    problem = catalog.get(conn, "design-a-parking-lot")
    assert problem.title == "Design a Parking Lot"
    assert problem.difficulty == "medium"
    assert problem.url == "" and problem.tags == () and problem.pattern is None
    assert problem.lists == ("mine",)


def test_a_url_with_no_scheme_is_a_file_beside_the_set(conn):
    path = _write_set(
        "mine",
        [
            {"title": "One", "url": "prompts/one.md"},
            {"title": "Two", "url": "https://example.com/two"},
            {"title": "Three", "url": "/somewhere/three.md"},
        ],
    )
    catalog.sync(conn, [ProblemSet("mine", "system-design", path)])
    assert catalog.get(conn, "one").url == str(paths.config_dir() / "sets/prompts/one.md")
    assert catalog.get(conn, "two").url == "https://example.com/two"
    assert catalog.get(conn, "three").url == "/somewhere/three.md"


def test_a_slug_two_types_both_claim_stays_with_the_first(conn):
    """`design-twitter` is already a LeetCode problem, with a history of its own."""
    _solve(conn, "design-twitter")
    path = _write_set("mine", [{"title": "Design Twitter"}, {"title": "Design a feed"}])
    reports = catalog.sync(
        conn, [*catalog.BUNDLED_SETS, ProblemSet("mine", "system-design", path)]
    )
    mine = reports[1]
    assert mine.problems == 1
    assert len(mine.skipped) == 1
    assert "design-twitter" in mine.skipped[0] and "neetcode150" in mine.skipped[0]

    kept = catalog.get(conn, "design-twitter")
    assert kept.type == "leetcode" and "mine" not in kept.lists
    assert conn.execute("SELECT COUNT(*) AS n FROM attempts").fetchone()["n"] == 1


def test_a_problem_in_two_sets_of_one_type_is_one_problem(conn):
    path = _write_set("mine", [{"slug": "two-sum", "title": "called something else"}])
    reports = catalog.sync(conn, [*catalog.BUNDLED_SETS, ProblemSet("mine", "leetcode", path)])
    assert all(r.ok for r in reports)
    assert catalog.count(conn) == 150
    problem = catalog.get(conn, "two-sum")
    assert problem.title == "Two Sum"  # the first set's wording stands
    assert problem.lists == ("neetcode150", "blind75", "mine")
    assert [p.slug for p in catalog.all_problems(conn, "mine")] == ["two-sum"]


def test_a_set_that_cannot_be_read_costs_only_itself(conn):
    path = _write_set("good", [{"title": "Fine"}])
    (paths.config_dir() / "sets" / "bad.json").write_text("{not json")
    reports = catalog.sync(
        conn,
        [
            ProblemSet("gone", "leetcode", "sets/nowhere.json"),
            ProblemSet("bad", "leetcode", "sets/bad.json"),
            ProblemSet("unbundled", "leetcode"),
            ProblemSet("good", "leetcode", path),
        ],
    )
    assert [bool(r.error) for r in reports] == [True, True, True, False]
    assert "could not be read" in reports[0].error
    assert "not valid JSON" in reports[1].error
    assert "give it a `path`" in reports[2].error
    assert catalog.get(conn, "fine") is not None


def test_an_entry_that_makes_no_sense_is_left_out_and_named(conn):
    path = _write_set(
        "mine", [{"title": ""}, {"slug": "Bad Slug", "title": "x"}, "a string", {"title": "Kept"}]
    )
    report = catalog.sync(conn, [ProblemSet("mine", "leetcode", path)])[0]
    assert report.problems == 1 and len(report.skipped) == 3
    assert not report.ok


def test_seeding_twice_changes_nothing(conn):
    catalog.sync(conn, [*catalog.BUNDLED_SETS, DESIGNS])
    before = [tuple(r) for r in conn.execute("SELECT * FROM problems ORDER BY slug")]
    catalog.sync(conn, [*catalog.BUNDLED_SETS, DESIGNS])
    assert [tuple(r) for r in conn.execute("SELECT * FROM problems ORDER BY slug")] == before


def test_what_went_wrong_is_said_in_words(conn):
    reports = catalog.sync(
        conn, [ProblemSet("gone", "nope", "sets/nowhere.json"), *catalog.BUNDLED_SETS]
    )
    lines = catalog.complaints(reports)
    assert any(line.startswith("set `gone`") for line in lines)
    assert any(line.startswith("type `nope`") for line in lines)
    assert catalog.complaints(catalog.sync(conn, catalog.BUNDLED_SETS)) == []


def test_a_database_from_before_types_gains_the_column(isolated_home):
    """`problems` is not a projection, so it is altered in place, not dropped."""
    target = paths.db_file()
    target.parent.mkdir(parents=True, exist_ok=True)
    old = sqlite3.connect(target)
    old.execute(
        "CREATE TABLE problems (slug TEXT PRIMARY KEY, title TEXT NOT NULL, "
        "url TEXT NOT NULL, difficulty TEXT NOT NULL, tags TEXT NOT NULL, "
        "pattern TEXT, lists TEXT NOT NULL)"
    )
    old.execute(
        "INSERT INTO problems VALUES('two-sum', 'Two Sum', 'u', 'easy', '[]', 'p', "
        "'[\"neetcode150\"]')"
    )
    old.commit()
    old.close()

    conn = db.open_db()
    assert catalog.get(conn, "two-sum").type == "leetcode"
    db.init(conn)  # and asking again is a no-op, not a second column
    assert catalog.count(conn) == 1


# --- the queue -------------------------------------------------------------


@pytest.fixture
def both(conn):
    catalog.sync(conn, [*catalog.BUNDLED_SETS, DESIGNS])
    return conn


def _queue(conn, active_list, n=3, now=NOW, **kw):
    return queues.ensure(conn, n=n, active_list=active_list, weights=WEIGHTS, now=now, **kw)


def test_each_list_has_its_own_queue_for_the_day(both):
    designs = _queue(both, "system-design", n=2)
    leetcode = _queue(both, "neetcode150")
    assert both.execute("SELECT COUNT(*) AS n FROM queues").fetchone()["n"] == 2

    # Drawing one up did not overwrite the other.
    assert _queue(both, "system-design", n=2).slugs == designs.slugs
    assert _queue(both, "neetcode150").slugs == leetcode.slugs
    assert designs.list_name == "system-design"
    assert both.execute(
        "SELECT COUNT(*) AS n FROM events WHERE type = 'queue_generated'"
    ).fetchone()["n"] == 2


def test_a_lists_queue_holds_only_its_own_problems(both):
    designs = {p.slug for p in catalog.all_problems(both, "system-design")}
    assert set(_queue(both, "system-design").slugs) <= designs
    assert not set(_queue(both, "neetcode150", n=6).slugs) & designs


def test_both_queues_survive_a_replay(both):
    designs = _queue(both, "system-design")
    leetcode = _queue(both, "neetcode150")
    events.replay(both)
    today = queues.today(NOW)
    assert queues.load(both, today, NOW, active_list="system-design").slugs == designs.slugs
    assert queues.load(both, today, NOW).slugs == leetcode.slugs


def test_a_queue_logged_before_lists_files_under_the_one_there_was(both):
    events.append(
        both,
        events.QUEUE_GENERATED,
        {"date": "2026-07-31", "slugs": ["two-sum"], "rationale": "", "generated_by": "x"},
    )
    assert queues.load(both, "2026-07-31", NOW).slugs == ["two-sum"]
    assert queues.load(both, "2026-07-31", NOW, active_list="system-design") is None


def test_a_due_design_leads_its_own_queue_and_not_the_other(both):
    _solve(both, "rate-limiter", self_confidence=3, answers={"requirements": "2/10"})
    later = datetime(2026, 12, 1, 9, 0, tzinfo=timezone.utc)
    designs = _queue(both, "system-design", now=later)
    assert designs.items[0].slug == "rate-limiter" and designs.items[0].is_review
    assert "rate-limiter" not in _queue(both, "neetcode150", n=6, now=later).slugs


def test_a_backlog_in_one_set_is_not_named_in_the_others_queue(both):
    for slug in ("rate-limiter", "url-shortener", "news-feed"):
        _solve(both, slug, verdict="gave_up")
    later = datetime(2026, 12, 1, 9, 0, tzinfo=timezone.utc)
    assert "deferred" in _queue(both, "system-design", now=later).rationale
    assert "deferred" not in _queue(both, "neetcode150", now=later).rationale


def test_the_weakest_pattern_is_ranked_within_the_type(both):
    for slug in ("rate-limiter", "unique-id-generator"):
        _solve(both, slug, verdict="gave_up")
    for slug in ("two-sum", "contains-duplicate"):
        _solve(both, slug)
    assert queues.weak_patterns(both, WEIGHTS, type="system-design") == ["coordination"]
    assert queues.weak_patterns(both, WEIGHTS, type="leetcode") == ["arrays-hashing"]
    assert set(queues.weak_patterns(both, WEIGHTS)) == {"coordination", "arrays-hashing"}


# --- the read models -------------------------------------------------------


def test_a_distribution_is_over_one_type(both):
    _solve(both, "two-sum", timing={"active_seconds": 600, "wall_seconds": 600, "paused_seconds": 0})
    _solve(
        both,
        "rate-limiter",
        timing={"active_seconds": 2400, "wall_seconds": 2400, "paused_seconds": 0},
    )
    leetcode = stats.distribution(both, difficulty="easy", days=None, type="leetcode")
    designs = stats.distribution(both, difficulty="easy", days=None, type="system-design")
    assert (leetcode.n, leetcode.p50) == (1, 600)
    assert (designs.n, designs.p50) == (1, 2400)
    # Par is the type's own, so "slow" means slow for that kind of problem.
    assert leetcode.par_seconds == 900 and designs.par_seconds == 1800
    assert stats.distribution(both, days=None).n == 2


def test_attempts_carry_their_type_and_their_answers(both):
    _solve(both, "rate-limiter", answers={"requirements": "7/10"})
    _solve(both, "two-sum")
    by_slug = {a["slug"]: a for a in stats.load_attempts(both)}
    assert by_slug["rate-limiter"]["type"] == "system-design"
    assert by_slug["rate-limiter"]["answers"] == {"requirements": "7/10"}
    assert by_slug["two-sum"]["type"] == "leetcode"
    assert by_slug["two-sum"]["answers"] == {}
    assert [a["slug"] for a in stats.load_attempts(both, type="system-design")] == [
        "rate-limiter"
    ]


def test_slicing_by_strategy_does_not_raise(conn):
    """It did, as soon as one had been named: `distribution` took no such slice."""
    # Timed, because a distribution counts the attempts that have a time to be
    # distributed.
    clock = {"active_seconds": 600, "wall_seconds": 600, "paused_seconds": 0}
    _solve(conn, "two-sum", strategies={"used": ["hash map"]}, timing=clock)
    _solve(conn, "3sum", strategies={"used": ["two pointers"]}, timing=clock)
    slices = stats.distributions_by(conn, "strategy", days=None)
    assert {d.label: d.n for d in slices} == {"HASH MAP": 1, "TWO POINTERS": 1}


# --- the offline cache -----------------------------------------------------


def test_only_a_type_with_somewhere_to_fetch_from_is_cached(both):
    lists = ("neetcode150", "system-design")
    assert len(cache.catalog_for(both, lists)) == 150
    assert cache.status(both, lists=lists, budget_bytes=1).total == 150
    assert all(p.type == "leetcode" for p in cache.priority(both, lists, now=NOW))


def test_offline_changes_nothing_for_a_problem_with_no_cache(both):
    design = catalog.get(both, "rate-limiter")
    assert not cache.cacheable(design)
    assert cache.target_for(design, offline=True) == ("", False)


# --- the command line ------------------------------------------------------


def test_seed_brings_in_every_set_in_the_config(isolated_home, capsys):
    path = _write_set("mine", [{"title": "Design a parking lot"}])
    _write_config(
        '[sets.system-design]\ntype = "system-design"\n\n'
        f'[sets.mine]\ntype = "system-design"\npath = "{path}"\n'
    )
    assert cli.main(["seed"]) == 0
    conn = db.open_db()
    assert catalog.count(conn) == 150 + 12 + 1
    assert catalog.get(conn, "design-a-parking-lot").type == "system-design"


def test_seed_says_so_when_a_set_did_not_load(isolated_home):
    _write_config('[sets.gone]\ntype = "leetcode"\npath = "sets/nowhere.json"\n')
    assert cli.main(["seed"]) == 1
    # And the set that was fine was seeded all the same.
    assert catalog.count(db.open_db()) == 150


def test_seed_by_hand_takes_a_path_from_where_you_are_standing(isolated_home, monkeypatch):
    here = isolated_home / "elsewhere"
    here.mkdir()
    (here / "mine.json").write_text(json.dumps([{"title": "Design a parking lot"}]))
    monkeypatch.chdir(here)
    assert cli.main(["seed", "--file", "mine.json", "--list", "mine", "--type", "system-design"]) == 0
    problem = catalog.get(db.open_db(), "design-a-parking-lot")
    assert problem.type == "system-design" and problem.lists == ("mine",)


def test_types_new_writes_a_copy_to_edit(isolated_home):
    from core import problemtypes

    assert cli.main(["types", "--new", "system-design"]) == 0
    written = problemtypes.user_path("system-design")
    assert written.read_text() == problemtypes.bundled_text("system-design")
    assert problemtypes.load("system-design").source == str(written)
    # Never over one that is already there.
    assert cli.main(["types", "--new", "system-design"]) == 1


def test_types_new_starts_a_new_name_from_whatever_you_say(isolated_home):
    from core import problemtypes

    assert cli.main(["types", "--new", "Behavioural", "--from", "system-design"]) == 0
    copied = problemtypes.load("behavioural")
    assert copied.field("requirements") is not None
    # Named for what it now is. Left calling itself "system design", there
    # would be two types on the stats screen that read the same.
    assert copied.title == "behavioural"
    assert problemtypes.load("system-design").title == "system design"
    # And nothing else about the file moved, the comments least of all.
    theirs = problemtypes.bundled_text("system-design").splitlines()
    mine = problemtypes.user_path("behavioural").read_text().splitlines()
    assert [a for a, b in zip(theirs, mine) if a != b] == ['label = "system design"']
    assert len(theirs) == len(mine)
    assert cli.main(["types", "--new", "other", "--from", "nope"]) == 1
    assert not problemtypes.user_path("other").exists()


def test_types_lists_a_type_a_set_names_but_nobody_wrote(isolated_home, capsys):
    _write_config('[sets.gone]\ntype = "nope"\npath = "sets/nowhere.json"\n')
    assert cli.main(["types"]) == 1
    out = capsys.readouterr().out
    assert "no type called `nope`" in out
    assert "requirements" in out  # and the ones that do exist, in full
