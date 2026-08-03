"""End-to-end smoke tests through the real Textual app.

These drive the run loop the way a keyboard does. Capture is disabled via
config so no editor is ever spawned.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from textual.widgets import Input, Static

from core import branding, db, paths, stats
from core.tui.app import CoreApp
from core.tui.screens import (
    FetchScreen,
    FinishModal,
    HistoryScreen,
    HomeScreen,
    QueueScreen,
    SettingsScreen,
    SetupScreen,
    SolveScreen,
    StatsScreen,
    SummaryScreen,
)

NO_CAPTURE_CONFIG = """
[session]
planned_n = 2
active_list = "neetcode150"

[capture]
enabled = false
language = "python"
"""


@pytest.fixture
def app(isolated_home):
    paths.ensure_dirs()
    paths.config_file().write_text(NO_CAPTURE_CONFIG)
    return CoreApp(db.open_db())


async def test_home_screen_boots_and_seeds_the_catalog(app):
    async with app.run_test() as pilot:
        assert isinstance(app.screen, HomeScreen)
        assert app.conn.execute("SELECT COUNT(*) AS n FROM problems").fetchone()["n"] == 150
        await pilot.pause()


async def test_stats_and_history_open_on_an_empty_database(app):
    async with app.run_test() as pilot:
        await pilot.press("t")
        assert isinstance(app.screen, StatsScreen)
        await pilot.press("escape")
        await pilot.press("r")
        assert isinstance(app.screen, HistoryScreen)
        await pilot.press("escape")
        assert isinstance(app.screen, HomeScreen)


async def test_a_full_run_is_recorded_end_to_end(app):
    async with app.run_test() as pilot:
        await pilot.press("n")
        await pilot.pause()
        assert isinstance(app.screen, SetupScreen)

        # The setup screen rolls a random selection on mount.
        assert app.screen.selected()
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert isinstance(app.screen, SolveScreen)

        # Work the first problem: a hint, a failed submit, then finish.
        await pilot.press("question_mark")
        await pilot.press("s")
        await pilot.pause()
        attempt = app.engine.attempt
        assert attempt.max_hint_tier == 1
        assert attempt.submissions == 1

        await pilot.press("f")
        await pilot.pause()
        assert isinstance(app.screen, FinishModal)
        await pilot.press("ctrl+s")
        await pilot.pause()

        # Capture is disabled, so it lands straight on the next problem.
        assert isinstance(app.screen, SolveScreen)

        await pilot.press("f")
        await pilot.pause()
        await pilot.press("ctrl+s")
        await pilot.pause()

        assert isinstance(app.screen, SummaryScreen)
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, HomeScreen)

    conn = app.conn
    session = conn.execute("SELECT * FROM sessions").fetchone()
    assert session["ended_at"] is not None
    assert session["outcome"] == "completed"

    attempts = conn.execute("SELECT * FROM attempts ORDER BY id").fetchall()
    assert len(attempts) == 2
    # Both took the modal's default. The first revealed a hint, so the cursor
    # started one rung down the ladder; the second did not.
    assert attempts[0]["verdict"] == "solved_with_hints"
    assert attempts[1]["verdict"] == "solved_unaided"
    assert all(a["active_seconds"] is not None for a in attempts)
    assert attempts[0]["max_hint_tier"] == 1
    assert attempts[0]["submissions"] == 1
    assert attempts[0]["self_confidence"] == 3


async def test_giving_up_records_the_attempt_and_ends_the_run(app):
    async with app.run_test() as pilot:
        await pilot.press("n")
        await pilot.pause()
        await pilot.press("ctrl+s")
        await pilot.pause()

        await pilot.press("x")
        await pilot.pause()
        await pilot.press("y")
        await pilot.pause()

        assert isinstance(app.screen, SolveScreen)
        await pilot.press("q")
        await pilot.pause()
        await pilot.press("y")
        await pilot.pause()
        assert isinstance(app.screen, SummaryScreen)
        await pilot.press("enter")
        await pilot.pause()

    verdicts = [r["verdict"] for r in app.conn.execute("SELECT verdict FROM attempts ORDER BY id")]
    assert verdicts == ["gave_up", "gave_up"]
    assert app.conn.execute("SELECT outcome FROM sessions").fetchone()["outcome"] == "completed"


async def test_cancelling_the_finish_modal_returns_to_the_problem(app):
    async with app.run_test() as pilot:
        await pilot.press("n")
        await pilot.pause()
        await pilot.press("ctrl+s")
        await pilot.pause()

        await pilot.press("f")
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()

        assert isinstance(app.screen, SolveScreen)
        assert app.engine.attempt is not None
        assert not app.engine.attempt.finished
        assert not app.engine.attempt.paused  # the timer resumes


async def test_pause_is_logged_not_hidden(app):
    async with app.run_test() as pilot:
        await pilot.press("n")
        await pilot.pause()
        await pilot.press("ctrl+s")
        await pilot.pause()

        await pilot.press("p")
        assert app.engine.attempt.paused
        await pilot.press("p")
        assert not app.engine.attempt.paused


async def test_history_shows_a_completed_run(app):
    async with app.run_test() as pilot:
        await pilot.press("n")
        await pilot.pause()
        await pilot.press("ctrl+s")
        await pilot.pause()
        await pilot.press("f")
        await pilot.pause()
        await pilot.press("ctrl+s")
        await pilot.pause()
        await pilot.press("f")
        await pilot.pause()
        await pilot.press("ctrl+s")
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()

        await pilot.press("r")
        await pilot.pause()
        assert isinstance(app.screen, HistoryScreen)
        assert len(app.screen.runs) == 1
        await pilot.press("t")  # table view
        await pilot.pause()
        await pilot.press("escape")

        await pilot.press("t")
        await pilot.pause()
        assert isinstance(app.screen, StatsScreen)
        await pilot.press("d")  # cycle slice dimension
        await pilot.press("w")  # cycle window
        await pilot.pause()


CAPTURE_CONFIG = """
[session]
planned_n = 1

[capture]
enabled = true
language = "python"
"""


@pytest.fixture
def capturing_app(isolated_home, monkeypatch, env_editor):
    """An app whose `$EDITOR` writes a line, with a headless-safe terminal handoff."""
    import contextlib

    paths.ensure_dirs()
    paths.config_file().write_text(CAPTURE_CONFIG)

    editor = isolated_home / "fake-editor"
    editor.write_text("#!/bin/sh\nprintf 'the shrink condition again\\n' >> \"$1\"\n")
    editor.chmod(0o755)
    monkeypatch.setenv(env_editor, str(editor))

    app = CoreApp(db.open_db())
    monkeypatch.setattr(type(app), "editor_context", lambda self: contextlib.nullcontext())
    return app


async def test_capture_archives_code_and_notes_to_disk(capturing_app):
    app = capturing_app
    async with app.run_test() as pilot:
        await pilot.press("n")
        await pilot.pause()
        await pilot.press("ctrl+s")
        await pilot.pause()
        await pilot.press("f")
        await pilot.pause()
        await pilot.press("ctrl+s")
        await pilot.pause()
        await pilot.pause()
        assert isinstance(app.screen, SummaryScreen)
        await pilot.press("enter")
        await pilot.pause()

    attempt = app.conn.execute("SELECT * FROM attempts").fetchone()
    assert attempt["code_path"] and attempt["note_path"]

    from pathlib import Path

    code, note = Path(attempt["code_path"]), Path(attempt["note_path"])
    assert code.exists() and note.exists()
    assert code.name == f"{attempt['id']}.py"
    assert note.name == f"{attempt['id']}.md"
    assert "shrink condition" in note.read_text()
    assert code.read_text().startswith(f"# {branding.NAME} | ")

    types = [r["type"] for r in app.conn.execute("SELECT type FROM events ORDER BY id")]
    assert "code_archived" in types
    assert "note_written" in types


async def test_a_broken_terminal_handoff_never_costs_the_attempt(isolated_home):
    """Headless `suspend()` raises. The attempt must still be fully recorded."""
    paths.ensure_dirs()
    paths.config_file().write_text(CAPTURE_CONFIG)
    app = CoreApp(db.open_db())

    async with app.run_test() as pilot:
        await pilot.press("n")
        await pilot.pause()
        await pilot.press("ctrl+s")
        await pilot.pause()
        await pilot.press("f")
        await pilot.pause()
        await pilot.press("ctrl+s")
        await pilot.pause()
        await pilot.pause()
        assert isinstance(app.screen, SummaryScreen)
        await pilot.press("enter")
        await pilot.pause()

    attempt = app.conn.execute("SELECT * FROM attempts").fetchone()
    assert attempt["verdict"] == "solved_unaided"
    assert attempt["active_seconds"] is not None
    # Capture failed — loudly on screen, but harmlessly to the record.
    assert attempt["code_path"] is None
    assert attempt["note_path"] is None


async def test_filtering_the_setup_list_keeps_the_rolled_selection(app):
    """Typing a filter must not silently discard picks that scroll out of view."""
    async with app.run_test() as pilot:
        await pilot.press("n")
        await pilot.pause()
        screen = app.screen
        rolled = set(screen.selected())
        assert rolled

        screen.query_one("#filter", Input).value = "trapping rain"
        await pilot.pause()
        assert set(screen.selected()) == rolled  # still chosen, just not visible

        screen.query_one("#filter", Input).value = ""
        await pilot.pause()
        assert set(screen.selected()) == rolled


async def test_declining_a_give_up_keeps_a_deliberate_pause(app):
    """You paused because you walked away. Saying "no" must not restart the clock."""
    async with app.run_test() as pilot:
        await pilot.press("n")
        await pilot.pause()
        await pilot.press("ctrl+s")
        await pilot.pause()

        await pilot.press("p")
        assert app.engine.attempt.paused

        await pilot.press("x")
        await pilot.pause()
        await pilot.press("n")
        await pilot.pause()

        assert isinstance(app.screen, SolveScreen)
        assert app.engine.attempt.paused


async def test_a_tier_four_hint_ends_the_attempt_from_the_ui(app):
    async with app.run_test() as pilot:
        await pilot.press("n")
        await pilot.pause()
        await pilot.press("ctrl+s")
        await pilot.pause()

        for _ in range(4):
            await pilot.press("question_mark")
        await pilot.pause()

        assert app.engine.attempt.finished
        await pilot.press("f")  # no verdict modal — straight to the write-up
        await pilot.pause()
        assert not isinstance(app.screen, FinishModal)

    row = app.conn.execute("SELECT verdict, max_hint_tier FROM attempts ORDER BY id").fetchone()
    assert row["verdict"] == "gave_up"
    assert row["max_hint_tier"] == 4


async def test_a_failed_submit_archives_the_code_behind_it(capturing_app):
    """`s` logs the submit, then hands you the buffer to paste the wrong answer."""
    app = capturing_app
    async with app.run_test() as pilot:
        await pilot.press("n")
        await pilot.pause()
        await pilot.press("ctrl+s")
        await pilot.pause()

        await pilot.press("s")
        await pilot.pause()
        await pilot.pause()
        assert isinstance(app.screen, SolveScreen)
        assert app.engine.attempt.submissions == 1
        assert not app.engine.attempt.paused  # the clock is running again

        await pilot.press("f")
        await pilot.pause()
        await pilot.press("ctrl+s")
        await pilot.pause()
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()

    row = app.conn.execute("SELECT * FROM submissions").fetchone()
    assert row["n"] == 1
    assert row["verdict"] == "wrong_answer"

    from pathlib import Path

    wrong = Path(row["code_path"])
    assert wrong.exists()
    assert wrong.name == f"{row['attempt_id']}-wrong1.py"
    assert "shrink condition" in wrong.read_text()

    # The solution it eventually became is a different file, still its own.
    attempt = app.conn.execute("SELECT * FROM attempts").fetchone()
    assert attempt["code_path"] != row["code_path"]
    assert Path(attempt["code_path"]).name == f"{attempt['id']}.py"


async def test_pasting_a_wrong_answer_is_not_billed_as_solve_time(capturing_app, monkeypatch):
    """The editor round-trip lands in paused_seconds, not in your percentile."""
    from core import capture as capture_module

    app = capturing_app
    clock = {"t": 1000.0}
    app.engine.clock = lambda: clock["t"]  # before the attempt starts

    def slow_editor(problem, row, attempt_id, n, language):
        clock["t"] += 45  # three quarters of a minute finding the buffer
        return capture_module.CaptureResult(False, reason="skipped")

    monkeypatch.setattr(capture_module, "capture_submission", slow_editor)

    async with app.run_test() as pilot:
        await pilot.press("n")
        await pilot.pause()
        await pilot.press("ctrl+s")
        await pilot.pause()

        attempt = app.engine.attempt
        clock["t"] += 120  # two minutes of actual solving
        await pilot.press("s")
        await pilot.pause()
        await pilot.pause()

        assert not attempt.paused  # the clock is running again
        assert attempt.total_paused_seconds == 45
        assert attempt.active_seconds == 120  # the handoff cost nothing


async def test_a_failed_submit_is_logged_even_with_capture_off(app):
    """Capture disabled: `s` is exactly what it was before, no editor, no prompt."""
    async with app.run_test() as pilot:
        await pilot.press("n")
        await pilot.pause()
        await pilot.press("ctrl+s")
        await pilot.pause()

        await pilot.press("s")
        await pilot.pause()
        assert isinstance(app.screen, SolveScreen)
        assert app.engine.attempt.submissions == 1

    row = app.conn.execute("SELECT * FROM submissions").fetchone()
    assert row["n"] == 1
    assert row["code_path"] is None


async def test_settings_opens_from_home_and_toggles_wrong_answer_capture(app):
    async with app.run_test() as pilot:
        await pilot.press("s")
        await pilot.pause()
        assert isinstance(app.screen, SettingsScreen)
        assert app.config.capture.on_failed_submit is True

        # Second row is capture.on_failed_submit; `l` cycles it to off.
        await pilot.press("j")
        await pilot.press("l")
        await pilot.pause()
        assert app.config.capture.on_failed_submit is False

        await pilot.press("escape")
        await pilot.pause()
        assert isinstance(app.screen, HomeScreen)

    # Persisted as an event, so it is still off on the next launch.
    from core import config as config_module

    assert config_module.overrides(app.conn) == {"capture.on_failed_submit": False}


async def test_a_settings_change_takes_effect_without_a_restart(capturing_app):
    """Turn wrong-answer capture off, then `s` must not open the editor."""
    app = capturing_app
    async with app.run_test() as pilot:
        await pilot.press("s")
        await pilot.pause()
        await pilot.press("j")
        await pilot.press("l")
        await pilot.pause()
        assert app.config.capture.on_failed_submit is False
        await pilot.press("escape")
        await pilot.pause()

        await pilot.press("n")
        await pilot.pause()
        await pilot.press("ctrl+s")
        await pilot.pause()
        await pilot.press("s")
        await pilot.pause()

    row = app.conn.execute("SELECT * FROM submissions").fetchone()
    assert row["code_path"] is None


async def test_x_hands_a_setting_back_to_the_config_file(app):
    async with app.run_test() as pilot:
        await pilot.press("s")
        await pilot.pause()
        await pilot.press("j")
        await pilot.press("l")
        await pilot.pause()
        assert app.config.capture.on_failed_submit is False

        await pilot.press("x")
        await pilot.pause()
        assert app.config.capture.on_failed_submit is True

    from core import config as config_module

    assert config_module.overrides(app.conn) == {}


async def test_a_skipped_second_note_edit_keeps_the_first(capturing_app, monkeypatch):
    app = capturing_app
    async with app.run_test() as pilot:
        await pilot.press("n")
        await pilot.pause()
        await pilot.press("ctrl+s")
        await pilot.pause()
        await pilot.press("f")
        await pilot.pause()
        await pilot.press("ctrl+s")
        await pilot.pause()
        await pilot.pause()
        assert isinstance(app.screen, SummaryScreen)

        await pilot.press("e")  # the fake editor writes a line
        await pilot.pause()
        first = app.screen.session_note
        assert first

        # Second edit, quit without saving.
        monkeypatch.setattr("core.capture.capture_session_note", lambda: None)
        await pilot.press("e")
        await pilot.pause()
        assert app.screen.session_note == first

        await pilot.press("enter")
        await pilot.pause()

    assert app.conn.execute("SELECT session_note FROM sessions").fetchone()["session_note"] == first


# --- the queue screen (spec §15 Phase 2, item 9) ---------------------------


async def test_d_opens_the_queue_and_it_is_never_empty(app):
    async with app.run_test() as pilot:
        await pilot.press("d")
        await pilot.pause()
        assert isinstance(app.screen, QueueScreen)
        # Generated on open: a morning queue that asks you to press a key first
        # is a morning queue you stop opening (spec §10).
        assert app.screen.queue is not None
        assert app.screen.queue.items
        await pilot.press("escape")
        await pilot.pause()
        assert isinstance(app.screen, HomeScreen)


async def test_the_queue_starts_a_run(app):
    async with app.run_test() as pilot:
        await pilot.press("d")
        await pilot.pause()
        slugs = app.screen.queue.slugs

        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, SolveScreen)
        assert app.engine.session is not None
        # The queue's list, in the queue's order — not a re-roll.
        assert app.engine.session.slugs == slugs


async def test_regenerating_keeps_one_row_per_day(app):
    async with app.run_test() as pilot:
        await pilot.press("d")
        await pilot.pause()
        await pilot.press("ctrl+r")
        await pilot.pause()
        rows = app.conn.execute("SELECT COUNT(*) AS n FROM queues").fetchone()["n"]
        assert rows == 1


async def test_the_queue_does_not_open_mid_run(app):
    """Same guard `n` has: one run at a time."""
    async with app.run_test() as pilot:
        await pilot.press("n")
        await pilot.pause()
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert isinstance(app.screen, SolveScreen)

        await pilot.press("d")
        await pilot.pause()
        assert isinstance(app.screen, SolveScreen)


async def test_motions_do_not_start_a_run_from_the_queue(app):
    """`j`/`k`/`g`/`G` are motions on every screen, this one included."""
    async with app.run_test() as pilot:
        await pilot.press("d")
        await pilot.pause()
        for key in ("j", "k", "g", "G", "ctrl+d", "ctrl+u"):
            await pilot.press(key)
            await pilot.pause()
        assert isinstance(app.screen, QueueScreen)
        assert app.engine.session is None


async def test_o_opens_the_cached_copy_in_offline_mode(app, monkeypatch):
    """The whole feature, through the keyboard: `o` on a plane."""
    opened: list[str] = []
    monkeypatch.setattr("core.capture.open_url", lambda url: opened.append(url) or True)

    def go_offline() -> None:
        app.config = replace(app.config, cache=replace(app.config.cache, offline=True))

    async with app.run_test() as pilot:
        await pilot.press("n")
        await pilot.pause()
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert isinstance(app.screen, SolveScreen)
        problem = app.engine.attempt.problem

        # Online: the real page, because that is where the submit button is.
        await pilot.press("o")
        await pilot.pause()
        assert opened == [problem.url]

        # Offline with nothing cached: it says so rather than opening a page
        # that cannot load.
        go_offline()
        await pilot.press("o")
        await pilot.pause()
        assert opened == [problem.url]  # unchanged

        # Offline, cached: the local file.
        paths.cache_path(problem.slug).write_text("<html>cached</html>")
        await pilot.press("o")
        await pilot.pause()
        assert opened[-1].startswith("file://")
        assert opened[-1].endswith(f"{problem.slug}.html")


async def test_the_finish_prompt_defaults_to_not_graded_when_offline(app):
    """A default of ACCEPTED is how a flight's unverified solves rot the stats."""
    async with app.run_test() as pilot:
        await pilot.press("n")
        await pilot.pause()
        await pilot.press("ctrl+s")
        await pilot.pause()

        app.config = replace(app.config, cache=replace(app.config.cache, offline=True))
        await pilot.press("f")
        await pilot.pause()
        assert isinstance(app.screen, FinishModal)
        await pilot.press("ctrl+s")
        await pilot.pause()

    verdict = app.conn.execute(
        "SELECT verdict FROM attempts ORDER BY id LIMIT 1"
    ).fetchone()["verdict"]
    assert verdict == "ungraded"


# --- offline cache ----------------------------------------------------------


@pytest.fixture
def no_network(monkeypatch):
    """Both of the cache's doors out, stubbed. Returns the slugs requested."""
    from core import cache

    requested: list[str] = []

    def fake_question(slug, **kw):
        requested.append(slug)
        if slug.startswith("meeting-rooms"):
            raise cache.FetchError("premium only — no session cookie")
        return {
            "title": slug,
            "difficulty": "Easy",
            "isPaidOnly": False,
            "content": f"<p>statement for {slug}</p>",
            "hints": ["a hint"],
            "exampleTestcases": "[]",
            "codeSnippets": [{"lang": "Python3", "langSlug": "python3", "code": "pass"}],
        }

    monkeypatch.setattr(cache, "fetch_question", fake_question)
    monkeypatch.setattr(cache, "_fetch_bytes", lambda url, **kw: b"png")
    monkeypatch.setattr(cache, "PAUSE_SECONDS", 0)
    return requested


async def test_f_warms_the_offline_cache_from_home(app, no_network):
    """The whole feature through the keyboard: the night before the flight."""
    async with app.run_test() as pilot:
        await pilot.press("f")
        await pilot.pause()
        assert isinstance(app.screen, FetchScreen)
        assert not list(paths.cache_dir().glob("*.html"))

        # Opening the screen fetches nothing — it waits to be told.
        assert no_network == []

        await pilot.press("enter")
        await app.workers.wait_for_complete()
        await pilot.pause()

        # The whole active list, minus the ones that cannot be fetched at all.
        on_disk = {p.stem for p in paths.cache_dir().glob("*.html")}
        assert len(on_disk) == 150 - 2
        assert "two-sum" in on_disk

        await pilot.press("escape")
        await pilot.pause()
        assert isinstance(app.screen, HomeScreen)


async def test_a_failed_problem_is_named_not_swallowed(app, no_network):
    async with app.run_test() as pilot:
        await pilot.press("f")
        await pilot.pause()
        await pilot.press("enter")
        await app.workers.wait_for_complete()
        await pilot.pause()

        panel = app.screen.query_one("#fetch-failures").render()
        assert "meeting-rooms" in str(panel)
        assert "premium only" in str(panel)


async def test_escape_stops_a_sweep_before_it_leaves_the_screen(app, no_network, monkeypatch):
    """Escape mid-sweep cancels; it does not abandon a thread behind a screen."""
    import threading

    from core import cache

    # Park the worker inside its first request, so escape lands during the
    # sweep rather than after a stubbed one has already raced to the end.
    gate = threading.Event()
    request = cache.fetch_question

    def gated(slug, **kw):
        gate.wait(5)
        return request(slug, **kw)

    monkeypatch.setattr(cache, "fetch_question", gated)

    async with app.run_test() as pilot:
        await pilot.press("f")
        await pilot.pause()
        await pilot.press("enter")

        screen = app.screen
        await pilot.press("escape")
        assert screen._stop_requested is True
        assert isinstance(app.screen, FetchScreen)  # still here, still stopping

        gate.set()
        await app.workers.wait_for_complete()
        await pilot.pause()

        # It stopped early, and what landed is a real cache: no half-written
        # page, and nothing pruned on the way out.
        assert screen._sweeping is False
        on_disk = list(paths.cache_dir().glob("*.html"))
        assert 0 < len(on_disk) < 150
        assert all(p.read_text().endswith("</html>") for p in on_disk)

        # Only now does escape mean leave.
        await pilot.press("escape")
        await pilot.pause()
        assert isinstance(app.screen, HomeScreen)


async def test_the_cache_screen_does_not_open_mid_run(app):
    async with app.run_test() as pilot:
        await pilot.press("n")
        await pilot.pause()
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert isinstance(app.screen, SolveScreen)

        app.action_fetch()
        await pilot.pause()
        assert isinstance(app.screen, SolveScreen)


# --- categories, throw-away, deletion --------------------------------------


async def test_categories_are_hidden_until_you_ask(app):
    """The pattern and the tags are an approach hint you never asked for."""
    async with app.run_test() as pilot:
        await pilot.press("n")
        await pilot.pause()
        await pilot.press("ctrl+s")
        await pilot.pause()

        meta = app.screen.query_one("#problem-meta")
        assert not meta.has_class("visible")
        assert not meta.display
        # The text is rendered all along — only the widget is hidden.
        assert str(meta.render()).strip()

        await pilot.press("c")
        await pilot.pause()
        assert meta.has_class("visible")
        assert meta.display

        await pilot.press("c")
        await pilot.pause()
        assert not meta.has_class("visible")


async def test_the_categories_toggle_resets_on_the_next_problem(app):
    """A decision made once per problem, not a mode left on for the run."""
    async with app.run_test() as pilot:
        await pilot.press("n")
        await pilot.pause()
        await pilot.press("ctrl+s")
        await pilot.pause()

        await pilot.press("c")
        await pilot.pause()
        assert app.screen.query_one("#problem-meta").has_class("visible")

        await pilot.press("f")
        await pilot.pause()
        await pilot.press("ctrl+s")
        await pilot.pause()

        assert isinstance(app.screen, SolveScreen)
        assert not app.screen.query_one("#problem-meta").has_class("visible")


async def test_throwing_an_attempt_away_records_nothing(app):
    async with app.run_test() as pilot:
        await pilot.press("n")
        await pilot.pause()
        await pilot.press("ctrl+s")
        await pilot.pause()
        await pilot.press("question_mark")   # a hint, to prove it goes too
        await pilot.pause()
        thrown = app.engine.attempt.uuid

        await pilot.press("f")
        await pilot.pause()
        assert isinstance(app.screen, FinishModal)
        await pilot.press("ctrl+x")
        await pilot.pause()
        await pilot.press("y")
        await pilot.pause()

        # Straight on to the second problem, with nothing behind us. The one
        # attempt row left is the new problem's, already open.
        assert isinstance(app.screen, SolveScreen)
        assert app.engine.session.index == 1
        rows = app.conn.execute("SELECT uuid, slug FROM attempts").fetchall()
        assert [r["uuid"] for r in rows] == [app.engine.attempt.uuid]
        assert thrown not in [r["uuid"] for r in rows]
        assert app.conn.execute("SELECT COUNT(*) AS n FROM fsrs_cards").fetchone()["n"] == 0


async def test_declining_the_throw_away_keeps_the_attempt_running(app):
    async with app.run_test() as pilot:
        await pilot.press("n")
        await pilot.pause()
        await pilot.press("ctrl+s")
        await pilot.pause()
        uuid = app.engine.attempt.uuid

        await pilot.press("f")
        await pilot.pause()
        await pilot.press("ctrl+x")
        await pilot.pause()
        await pilot.press("n")           # keep it
        await pilot.pause()

        assert isinstance(app.screen, SolveScreen)
        assert app.engine.attempt is not None
        assert app.engine.attempt.uuid == uuid
        assert not app.engine.attempt.finished


async def _play_one_run(pilot, app):
    """n → solve both problems on the defaults → back to home."""
    await pilot.press("n")
    await pilot.pause()
    await pilot.press("ctrl+s")
    await pilot.pause()
    for _ in range(2):
        await pilot.press("f")
        await pilot.pause()
        await pilot.press("ctrl+s")
        await pilot.pause()
    await pilot.press("enter")
    await pilot.pause()


async def test_deleting_a_run_closes_the_gap_in_the_numbering(app):
    async with app.run_test() as pilot:
        await _play_one_run(pilot, app)
        await _play_one_run(pilot, app)
        await _play_one_run(pilot, app)

        await pilot.press("r")
        await pilot.pause()
        assert isinstance(app.screen, HistoryScreen)
        assert len(app.screen.runs) == 3
        middle = app.screen.runs[1].session_uuid

        # The list is newest-first, so index 1 is run #2.
        app.screen.query_one("#run-list").highlighted = 1
        await pilot.pause()
        await pilot.press("d")
        await pilot.pause()
        await pilot.press("y")
        await pilot.pause()

        assert len(app.screen.runs) == 2
        assert middle not in [r.session_uuid for r in app.screen.runs]
        # What was #3 is now #2: the numbers are positional and close up.
        assert [stats.run_number(app.screen.runs, r.session_id) for r in app.screen.runs] == [1, 2]
        assert app.conn.execute("SELECT COUNT(*) AS n FROM sessions").fetchone()["n"] == 2


async def test_declining_a_run_deletion_changes_nothing(app):
    async with app.run_test() as pilot:
        await _play_one_run(pilot, app)
        await pilot.press("r")
        await pilot.pause()

        await pilot.press("d")
        await pilot.pause()
        await pilot.press("n")
        await pilot.pause()

        assert len(app.screen.runs) == 1
        assert app.conn.execute("SELECT COUNT(*) AS n FROM attempts").fetchone()["n"] == 2


async def test_the_finish_buttons_stay_on_screen_on_a_short_terminal(isolated_home):
    """The verdict ladder makes this the tallest thing in the app.

    Unbounded, the button row lands below the fold on an 80x24 terminal and
    "throw away" is invisible — a destructive action you cannot see is worse
    than one that does not exist.
    """
    paths.ensure_dirs()
    paths.config_file().write_text(NO_CAPTURE_CONFIG)
    app = CoreApp(db.open_db())

    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.press("n")
        await pilot.pause()
        await pilot.press("ctrl+s")
        await pilot.pause()
        await pilot.press("f")
        await pilot.pause()

        assert isinstance(app.screen, FinishModal)
        box = app.screen.query_one("#finish-box")
        assert box.region.bottom <= 24
        for button in app.screen.query("Button"):
            assert button.region.bottom <= 24, f"{button.id} is below the fold"
            assert button.region.right <= box.region.right, f"{button.id} overflows"


PAST_ATTEMPT_SLUG = "two-sum"


def _plain(widget: Static) -> str:
    """What a `Static` is actually showing, flattened to plain text."""
    visual = widget.visual
    if hasattr(visual, "plain"):  # a rich Text became a Content
        return visual.plain
    from rich.console import Console

    console = Console(width=120, no_color=True)
    with console.capture() as capture:
        console.print(visual._renderable)
    return capture.get()


def _seed_a_past_attempt(conn, *, days_ago: int) -> str:
    """A finished attempt at `PAST_ATTEMPT_SLUG`, with its code archived on disk."""
    from datetime import datetime, timedelta, timezone

    from core import events

    ts = (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat(timespec="seconds")
    session_uuid, attempt_uuid = events.new_uuid(), events.new_uuid()
    code_path = "/somewhere/only-you-know/two-sum.py"
    events.append(
        conn, events.SESSION_STARTED, {"session_uuid": session_uuid, "planned_n": 1}, ts=ts
    )
    events.append(
        conn,
        events.PROBLEM_STARTED,
        {"session_uuid": session_uuid, "attempt_uuid": attempt_uuid, "slug": PAST_ATTEMPT_SLUG},
        ts=ts,
    )
    events.append(conn, events.HINT_REVEALED, {"attempt_uuid": attempt_uuid, "tier": 1}, ts=ts)
    events.append(
        conn,
        events.PROBLEM_FINISHED,
        {
            "attempt_uuid": attempt_uuid,
            "verdict": "solved_with_hints",
            "active_seconds": 552,
            "wall_seconds": 552,
            "self_confidence": 3,
        },
        ts=ts,
    )
    events.append(
        conn,
        events.CODE_ARCHIVED,
        {"attempt_uuid": attempt_uuid, "slug": PAST_ATTEMPT_SLUG, "code_path": code_path},
        ts=ts,
    )
    events.append(
        conn, events.SESSION_ENDED, {"session_uuid": session_uuid, "outcome": "completed"}, ts=ts
    )
    return code_path


async def test_opening_a_problem_shows_when_you_last_attempted_it(app):
    """Your record on the problem, on the problem screen — never your old answer."""
    async with app.run_test() as pilot:
        code_path = _seed_a_past_attempt(app.conn, days_ago=12)
        app.start_run([PAST_ATTEMPT_SLUG])
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, SolveScreen)
        assert len(screen._past_attempts) == 1
        assert screen._past_attempts[0].ago == "12d ago"

        # The summary line is always up; the table is folded away until r.
        panel = screen.query_one("#past-attempts", Static)
        assert not panel.has_class("visible")
        summary = _plain(screen.query_one("#last-attempt", Static))
        assert "seen once before" in summary
        assert "12d ago" in summary
        assert "SOLVED WITH HINTS" in summary

        await pilot.press("r")
        await pilot.pause()
        assert panel.has_class("visible")
        shown = _plain(panel)
        assert "SOLVED WITH HINTS" in shown
        assert "09:12" in shown  # 552 seconds, the time it took
        assert '"good"' in shown  # how well you thought you knew it
        # The whole point of the panel: it can never hand you the answer back.
        assert code_path not in shown
        assert "only-you-know" not in shown

        await pilot.press("r")
        await pilot.pause()
        assert not panel.has_class("visible")


async def test_a_problem_you_have_never_seen_says_so(app):
    async with app.run_test() as pilot:
        app.start_run([PAST_ATTEMPT_SLUG])
        await pilot.pause()
        screen = app.screen
        assert screen._past_attempts == []
        assert "first time on this one" in _plain(screen.query_one("#last-attempt", Static))

        # r has nothing to open, and must not leave an empty panel behind.
        await pilot.press("r")
        await pilot.pause()
        assert not screen.query_one("#past-attempts", Static).has_class("visible")


async def test_the_past_attempts_panel_is_refolded_for_the_next_problem(app):
    async with app.run_test() as pilot:
        _seed_a_past_attempt(app.conn, days_ago=3)
        app.start_run([PAST_ATTEMPT_SLUG, "3sum"])
        await pilot.pause()
        screen = app.screen
        await pilot.press("r")
        await pilot.pause()
        assert screen.query_one("#past-attempts", Static).has_class("visible")

        await pilot.press("f")
        await pilot.pause()
        await pilot.press("ctrl+s")
        await pilot.pause()

        assert isinstance(app.screen, SolveScreen)
        assert app.screen._past_attempts == []
        assert not app.screen.query_one("#past-attempts", Static).has_class("visible")
