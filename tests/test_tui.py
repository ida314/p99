"""End-to-end smoke tests through the real Textual app.

These drive the run loop the way a keyboard does. Capture is disabled via
config so no editor is ever spawned.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from textual.widgets import Input, OptionList, Static

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
        assert "\"I'd get there\"" in shown  # self_confidence 3, as you rated it
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


async def test_the_confidence_knob_asks_about_recall_not_about_now(app):
    """The reworded post-solve knob, on screen.

    Same four values it always stored, so history keeps its reading — but asked
    as a question about a month from now rather than about how it feels while
    the answer is still up, which is when self-assessment is least reliable.
    """
    from textual.widgets import RadioButton, RadioSet

    from core.tui.screens.finish import CONFIDENCE_OPTIONS

    async with app.run_test() as pilot:
        app.start_run(["two-sum"])
        await pilot.pause()
        await pilot.press("f")
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, FinishModal)

        labels = [_plain(s) for s in screen.query(Static)]
        assert "if this came up cold in a month?" in labels
        assert "how well will this stick?" not in labels

        buttons = screen.query_one("#confidence", RadioSet).query(RadioButton)
        shown = [b.label.plain for b in buttons]
        assert shown == list(CONFIDENCE_OPTIONS)
        # Apostrophes survive the trip; nothing was swallowed as markup.
        assert "I'd nail it" in shown[3]


async def test_the_finish_prompt_asks_what_the_solution_cost(app):
    """The typed complexity and the optimality answer, from keystrokes to row."""
    from textual.widgets import RadioButton, RadioSet

    from core.tui.screens.finish import OPTIMALITY_OPTIONS

    async with app.run_test() as pilot:
        app.start_run(["two-sum"])
        await pilot.pause()
        await pilot.press("f")
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, FinishModal)

        labels = [_plain(s) for s in screen.query(Static)]
        assert "your time complexity — optional" in labels
        assert "was it the optimal algorithm?" in labels

        buttons = screen.query_one("#optimality", RadioSet).query(RadioButton)
        assert [b.label.plain for b in buttons] == [label for _, label in OPTIMALITY_OPTIONS]

        screen.query_one("#complexity", Input).value = "O(n log n)"
        # `k` off the default highlights "not optimal"; `space` presses it. The
        # motion moves the cursor and nothing else, which is the point of the
        # rule in `vim.py` — a key that moves must never also commit.
        screen.query_one("#optimality", RadioSet).focus()
        await pilot.press("k")
        await pilot.press("space")
        await pilot.pause()
        await pilot.press("ctrl+s")
        await pilot.pause()

    row = app.conn.execute("SELECT * FROM attempts").fetchone()
    assert row["claimed_complexity"] == "O(n log n)"
    assert row["optimality"] == "suboptimal"


async def test_optimality_defaults_to_not_sure(app):
    """The flattering answer is never the default — same rule as the verdict."""
    async with app.run_test() as pilot:
        app.start_run(["two-sum"])
        await pilot.pause()
        await pilot.press("f")
        await pilot.pause()
        await pilot.press("ctrl+s")
        await pilot.pause()

    row = app.conn.execute("SELECT * FROM attempts").fetchone()
    assert row["optimality"] == "unsure"
    # An untouched complexity field stores nothing rather than an empty string.
    assert row["claimed_complexity"] is None


async def test_what_you_claimed_is_not_shown_back_on_the_next_attempt(app):
    """The past-attempts panel must not hand you the target complexity.

    Its whole contract is that checking your record on a problem cannot spoil
    it, and "last time: O(n log n) · not optimal" is the answer's shape.
    """
    async with app.run_test() as pilot:
        app.start_run(["two-sum"])
        await pilot.pause()
        await pilot.press("f")
        await pilot.pause()
        app.screen.query_one("#complexity", Input).value = "O(n log n)"
        await pilot.press("ctrl+s")
        await pilot.pause()
        await pilot.pause()
        # Seal the first run before opening a second one on the same problem.
        await pilot.press("enter")
        await pilot.pause()

        app.start_run(["two-sum"])
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, SolveScreen)
        assert screen._past_attempts, "the earlier attempt should be on the record"

        await pilot.press("r")
        await pilot.pause()
        shown = _plain(screen.query_one("#past-attempts", Static))
        shown += _plain(screen.query_one("#last-attempt", Static))
        assert "O(n log n)" not in shown
        assert "optimal" not in shown


async def test_history_shows_the_approach_after_the_fact(app):
    """What the solve screen withholds, history shows — that is the split."""
    async with app.run_test() as pilot:
        app.start_run(["two-sum"])
        await pilot.pause()
        await pilot.press("f")
        await pilot.pause()
        app.screen.query_one("#complexity", Input).value = "O(n)"
        await pilot.press("ctrl+s")
        await pilot.pause()
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()

        await pilot.press("r")
        await pilot.pause()
        assert isinstance(app.screen, HistoryScreen)
        shown = _plain(app.screen.query_one("#run-detail", Static))
        assert "approach" in shown
        assert "O(n)" in shown
        assert "not sure" in shown


async def test_the_radio_cursor_starts_on_the_shown_default(app):
    """Moving off a default must start from the default you can see.

    Textual parks the navigation cursor on the first button regardless of which
    one is pressed, and every default on this screen is deliberately not the
    first: one `j` off "not sure" has to land on "not optimal", not on the
    second entry of a list you were never looking at.
    """
    from textual.widgets import RadioSet

    async with app.run_test() as pilot:
        app.start_run(["two-sum"])
        await pilot.pause()
        await pilot.press("f")
        await pilot.pause()
        screen = app.screen

        for radio_id in ("#verdict", "#confidence", "#optimality"):
            radio = screen.query_one(radio_id, RadioSet)
            assert radio._selected == radio.pressed_index, radio_id

        screen.query_one("#optimality", RadioSet).focus()
        await pilot.pause()
        await pilot.press("j")
        await pilot.press("space")
        await pilot.pause()
        await pilot.press("ctrl+s")
        await pilot.pause()

    # "not sure" is last, so one step down wraps to the top: "optimal".
    assert app.conn.execute("SELECT * FROM attempts").fetchone()["optimality"] == "optimal"


SPEECH_CONFIG = """
[session]
planned_n = 1

[capture]
enabled = false
language = "python"

[audio]
speech_mode = true
bitrate_kbps = 12
input_format = "lavfi"
device = "anullsrc"
"""

# Same fake as tests/test_audio.py: writes to the last argument, then waits for
# the polite `q`. Keeps the suite off the microphone.
FAKE_FFMPEG = """#!/bin/sh
for arg in "$@"; do dest="$arg"; done
printf 'segment' > "$dest"
read -r line
exit 0
"""


@pytest.fixture
def speaking_app(isolated_home, monkeypatch):
    """An app with speech mode on and a fake recorder behind it."""
    paths.ensure_dirs()
    paths.config_file().write_text(SPEECH_CONFIG)

    recorder = isolated_home / "fake-ffmpeg"
    recorder.write_text(FAKE_FFMPEG)
    recorder.chmod(0o755)
    monkeypatch.setenv(branding.env("FFMPEG"), str(recorder))
    return CoreApp(db.open_db())


async def test_speech_mode_records_a_problem_and_hangs_it_on_the_attempt(speaking_app):
    from pathlib import Path

    app = speaking_app
    async with app.run_test() as pilot:
        await pilot.press("n")
        await pilot.pause()
        assert isinstance(app.screen, SetupScreen)
        assert "speech mode: on" in _plain(app.screen.query_one("#setup-status", Static))

        await pilot.press("ctrl+s")
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, SolveScreen)
        assert screen._recorder is not None and screen._recorder.recording
        # Never recording invisibly: the marker sits on the clock line.
        assert "● REC" in _plain(screen.query_one("#timer", Static))

        await pilot.press("f")
        await pilot.pause()
        await pilot.press("ctrl+s")
        await pilot.pause()

    attempt = app.conn.execute("SELECT * FROM attempts").fetchone()
    assert attempt["audio_path"]
    path = Path(attempt["audio_path"])
    assert path.exists()
    assert path == paths.audio_path(attempt["slug"], attempt["id"])


async def test_pausing_the_problem_pauses_the_recording(speaking_app):
    app = speaking_app
    async with app.run_test() as pilot:
        await pilot.press("n")
        await pilot.pause()
        await pilot.press("ctrl+s")
        await pilot.pause()
        screen = app.screen

        await pilot.press("p")
        await pilot.pause()
        assert app.engine.attempt.paused
        assert screen._recorder.recording is False
        assert "● REC PAUSED" in _plain(screen.query_one("#timer", Static))

        await pilot.press("p")
        await pilot.pause()
        assert not app.engine.attempt.paused
        assert screen._recorder.recording is True


async def test_escaping_the_finish_prompt_keeps_recording(speaking_app):
    """`esc` hands back a live problem, so the microphone has to come back too."""
    app = speaking_app
    async with app.run_test() as pilot:
        await pilot.press("n")
        await pilot.pause()
        await pilot.press("ctrl+s")
        await pilot.pause()
        screen = app.screen

        await pilot.press("f")
        await pilot.pause()
        assert isinstance(app.screen, FinishModal)
        # Paused while the prompt is up: what you say filling in a verdict is
        # not part of the solve.
        assert screen._recorder.recording is False

        await pilot.press("escape")
        await pilot.pause()
        assert isinstance(app.screen, SolveScreen)
        assert screen._recorder is not None and screen._recorder.recording


async def test_throwing_an_attempt_away_takes_its_recording_with_it(speaking_app):
    app = speaking_app
    async with app.run_test() as pilot:
        await pilot.press("n")
        await pilot.pause()
        await pilot.press("ctrl+s")
        await pilot.pause()
        screen = app.screen
        slug = app.engine.attempt.problem.slug
        attempt_id = app.engine.attempt.id
        segments = screen._recorder.segment_dir

        await pilot.press("f")
        await pilot.pause()
        await pilot.press("ctrl+x")
        await pilot.pause()
        await pilot.press("y")
        await pilot.pause()

    assert app.conn.execute("SELECT COUNT(*) AS n FROM attempts").fetchone()["n"] == 0
    assert not paths.audio_path(slug, attempt_id).exists()
    assert not segments.exists()


async def test_speech_mode_off_records_nothing(app):
    async with app.run_test() as pilot:
        await pilot.press("n")
        await pilot.pause()
        assert "speech mode: off" in _plain(app.screen.query_one("#setup-status", Static))
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert app.screen._recorder is None
        assert "REC" not in _plain(app.screen.query_one("#timer", Static))

        await pilot.press("f")
        await pilot.pause()
        await pilot.press("ctrl+s")
        await pilot.pause()

    assert app.conn.execute("SELECT * FROM attempts").fetchone()["audio_path"] is None
    assert not paths.audio_dir().exists() or not any(paths.audio_dir().iterdir())


async def test_the_setup_screen_overrides_the_setting_for_one_run(speaking_app):
    """`ctrl+a` is a decision about tonight, never a new default."""
    app = speaking_app
    async with app.run_test() as pilot:
        await pilot.press("n")
        await pilot.pause()
        await pilot.press("ctrl+a")
        await pilot.pause()
        assert "speech mode: off" in _plain(app.screen.query_one("#setup-status", Static))

        await pilot.press("ctrl+s")
        await pilot.pause()
        assert app.speech_mode is False
        assert app.screen._recorder is None

    # The setting itself is untouched — nothing was written back.
    from core import config as config_module

    assert config_module.overrides(app.conn) == {}
    assert app.config.audio.speech_mode is True


async def test_a_missing_recorder_never_costs_the_attempt(speaking_app, monkeypatch):
    app = speaking_app
    monkeypatch.setenv(branding.env("FFMPEG"), "/nonexistent/ffmpeg")
    async with app.run_test() as pilot:
        await pilot.press("n")
        await pilot.pause()
        await pilot.press("ctrl+s")
        await pilot.pause()
        screen = app.screen
        assert screen._recorder is None
        assert "ffmpeg" in _plain(screen.query_one("#toast", Static))

        await pilot.press("f")
        await pilot.pause()
        await pilot.press("ctrl+s")
        await pilot.pause()

    attempt = app.conn.execute("SELECT * FROM attempts").fetchone()
    assert attempt["verdict"] == "solved_unaided"
    assert attempt["audio_path"] is None


# --- suspend and resume ----------------------------------------------------


async def test_z_suspends_the_run_without_grading_the_problem(app):
    async with app.run_test() as pilot:
        await pilot.press("n")
        await pilot.pause()
        await pilot.press("ctrl+s")
        await pilot.pause()

        await pilot.press("question_mark")
        await pilot.press("s")
        await pilot.pause()

        await pilot.press("z")
        await pilot.pause()
        assert isinstance(app.screen, HomeScreen)
        assert app.engine.session is None and app.engine.attempt is None

        # The way back in is on screen the moment you land, at the top.
        menu = app.screen.query_one(OptionList)
        assert menu.get_option_at_index(0).id == "resume_run"
        assert "problem 1 of 2" in menu.get_option_at_index(0).prompt.plain

    attempt = app.conn.execute("SELECT * FROM attempts").fetchone()
    assert attempt["verdict"] is None            # not a gave_up
    assert attempt["ended_at"] is None
    assert attempt["max_hint_tier"] == 1
    session = app.conn.execute("SELECT * FROM sessions").fetchone()
    assert session["ended_at"] is None
    assert session["suspended_at"] is not None


async def test_a_hard_quit_suspends_the_run_rather_than_abandoning_it(app):
    """ctrl+c mid-problem used to score a 0 for a problem you never gave up on."""
    async with app.run_test() as pilot:
        await pilot.press("n")
        await pilot.pause()
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert isinstance(app.screen, SolveScreen)
        # No key here: leaving the block is the hard quit.

    attempt = app.conn.execute("SELECT * FROM attempts").fetchone()
    assert attempt["verdict"] is None
    assert app.conn.execute("SELECT COUNT(*) AS n FROM fsrs_cards").fetchone()["n"] == 0
    session = app.conn.execute("SELECT * FROM sessions").fetchone()
    assert session["suspended_at"] is not None and session["ended_at"] is None


async def test_a_suspended_run_is_picked_back_up_by_a_later_process(app):
    async with app.run_test() as pilot:
        await pilot.press("n")
        await pilot.pause()
        await pilot.press("ctrl+s")
        await pilot.pause()
        await pilot.press("question_mark")
        await pilot.press("s")
        await pilot.pause()
        slug = app.engine.attempt.problem.slug
        await pilot.press("z")
        await pilot.pause()

    # A different app on the same database — the next time you open it.
    later = CoreApp(db.open_db())
    async with later.run_test() as pilot:
        await pilot.press("c")
        await pilot.pause()
        assert isinstance(later.screen, SolveScreen)

        attempt = later.engine.attempt
        assert attempt is not None
        assert attempt.problem.slug == slug
        assert attempt.max_hint_tier == 1
        assert attempt.submissions == 1
        # Back on the problem you left, with the clock stopped until you say so.
        assert attempt.paused
        assert "PAUSED" in _plain(later.screen.query_one("#timer", Static))
        assert "resumed" in _plain(later.screen.query_one("#toast", Static))

        # And it is still the same run, not a new one.
        assert later.engine.session.index == 0
        assert len(later.engine.session.slugs) == 2

    assert later.conn.execute("SELECT COUNT(*) AS n FROM sessions").fetchone()["n"] == 1


async def test_a_resumed_run_can_be_finished_normally(app):
    async with app.run_test() as pilot:
        await pilot.press("n")
        await pilot.pause()
        await pilot.press("ctrl+s")
        await pilot.pause()
        await pilot.press("z")
        await pilot.pause()

    later = CoreApp(db.open_db())
    async with later.run_test() as pilot:
        await pilot.press("c")
        await pilot.pause()
        for _ in range(2):
            await pilot.press("f")
            await pilot.pause()
            await pilot.press("ctrl+s")
            await pilot.pause()

        assert isinstance(later.screen, SummaryScreen)
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(later.screen, HomeScreen)
        # Nothing left to resume, so the entry is gone again.
        assert later.screen.query_one(OptionList).get_option_at_index(0).id == "new_run"

    session = later.conn.execute("SELECT * FROM sessions").fetchone()
    assert session["outcome"] == "completed"
    assert session["suspended_at"] is None
    attempts = later.conn.execute("SELECT * FROM attempts ORDER BY id").fetchall()
    assert len(attempts) == 2
    assert all(a["verdict"] == "solved_unaided" for a in attempts)
    assert attempts[0]["suspends"] == 1


async def test_resume_bells_when_there_is_nothing_to_pick_up(app):
    async with app.run_test() as pilot:
        await pilot.press("c")
        await pilot.pause()
        assert isinstance(app.screen, HomeScreen)


async def test_a_suspended_recording_is_continued_rather_than_cut_short(speaking_app):
    from pathlib import Path

    app = speaking_app
    async with app.run_test() as pilot:
        await pilot.press("n")
        await pilot.pause()
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert app.screen._recorder.recording
        segments = app.screen._recorder.segment_dir

        await pilot.press("z")
        await pilot.pause()
        # Closed, but nothing joined: the attempt is not over.
        assert len(list(segments.glob("*.opus"))) == 1
        assert app.conn.execute("SELECT audio_path FROM attempts").fetchone()[0] is None

    later = CoreApp(db.open_db())
    async with later.run_test() as pilot:
        await pilot.press("c")
        await pilot.pause()
        screen = later.screen
        # Speech mode is read off the run, and the microphone comes back paused
        # with the clock.
        assert later.speech_mode is True
        assert screen._recorder is not None and screen._recorder.paused
        assert "● REC PAUSED" in _plain(screen.query_one("#timer", Static))

        await pilot.press("p")      # start the clock, and the microphone with it
        await pilot.pause()
        assert screen._recorder.recording

        await pilot.press("f")
        await pilot.pause()
        await pilot.press("ctrl+s")
        await pilot.pause()

    attempt = later.conn.execute("SELECT * FROM attempts ORDER BY id").fetchone()
    # One file for the whole solve, both halves of it.
    assert attempt["audio_path"] is not None
    assert Path(attempt["audio_path"]).exists()
    assert not segments.exists()
