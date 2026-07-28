"""End-to-end smoke tests through the real Textual app.

These drive the run loop the way a keyboard does. Capture is disabled via
config so no editor is ever spawned.
"""

from __future__ import annotations

import pytest

from textual.widgets import Input

from core import branding, db, paths
from core.tui.app import CoreApp
from core.tui.screens import (
    FinishModal,
    HistoryScreen,
    HomeScreen,
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
    assert all(a["verdict"] == "accepted" for a in attempts)
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
    assert attempt["verdict"] == "accepted"
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
