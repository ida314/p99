"""Problem types through the real app: a second kind of problem, end to end.

Driven the way a keyboard does, like `test_tui`. What is being tested is that a
type is a TOML file and nothing else — every screen here is the one LeetCode
uses, asked a different set of questions.
"""

from __future__ import annotations

import contextlib
import json

import pytest

from textual.widgets import Button, Input, RadioButton, RadioSet, SelectionList, Static

from core import db, paths, problemtypes
from core.tui.app import CoreApp
from core.tui.screens import (
    ConfirmModal,
    FinishModal,
    MethodsModal,
    QueueScreen,
    SetupScreen,
    SolveScreen,
    StatsScreen,
    StrategyModal,
    SummaryScreen,
)

SETS = """
[sets.system-design]
type = "system-design"
queue_n = 2
"""

QUIET = """
[session]
planned_n = 2
active_list = "neetcode150"

[capture]
enabled = false
language = "python"

[strategy]
enabled = false
"""

ASKING = QUIET.replace("[strategy]\nenabled = false", "[strategy]\nenabled = true")


def _app(config: str) -> CoreApp:
    paths.ensure_dirs()
    paths.config_file().write_text(config)
    return CoreApp(db.open_db())


@pytest.fixture
def app(isolated_home):
    """Two sets, no capture, no prompts after the verdict."""
    return _app(QUIET + SETS)


@pytest.fixture
def asking_app(isolated_home):
    """Two sets, and the prompts after the verdict are on."""
    return _app(ASKING + SETS)


def _plain(widget: Static) -> str:
    visual = widget.visual
    if hasattr(visual, "plain"):
        return visual.plain
    from rich.console import Console

    console = Console(width=120, no_color=True)
    with console.capture() as capture:
        console.print(visual._renderable)
    return capture.get()


def _labels(screen) -> list[str]:
    return [_plain(s) for s in screen.query(Static)]


async def _finish(pilot, app) -> FinishModal:
    await pilot.press("f")
    await pilot.pause()
    assert isinstance(app.screen, FinishModal), app.screen
    return app.screen


# --- the catalog -----------------------------------------------------------


async def test_a_set_named_in_the_config_is_there_on_launch(app):
    async with app.run_test() as pilot:
        await pilot.pause()
        rows = app.conn.execute(
            "SELECT type, COUNT(*) AS n FROM problems GROUP BY type ORDER BY type"
        ).fetchall()
        assert [(r["type"], r["n"]) for r in rows] == [("leetcode", 150), ("system-design", 12)]
        assert all(r.ok for r in app.catalog_reports)


async def test_a_set_that_did_not_load_does_not_stop_the_app(isolated_home):
    app = _app(QUIET + '\n[sets.gone]\ntype = "nope"\npath = "sets/nowhere.json"\n')
    async with app.run_test() as pilot:
        await pilot.pause()
        assert [r.name for r in app.catalog_reports if r.error] == ["gone"]
        # And everything that was fine is there to be run.
        app.start_run(["two-sum"])
        await pilot.pause()
        assert isinstance(app.screen, SolveScreen)


# --- the verdict prompt ----------------------------------------------------


async def test_a_design_is_asked_about_coverage_not_cost(app):
    async with app.run_test() as pilot:
        app.start_run(["rate-limiter"])
        await pilot.pause()
        screen = await _finish(pilot, app)

        labels = _labels(screen)
        assert "what you covered — optional" in labels
        assert "how did it come out?" in labels
        assert "design" in labels and "trade-offs" in labels and "delivery" in labels
        # Nothing of LeetCode's is on this form.
        assert "what your solution costs — optional" not in labels
        assert "leetcode percentiles — optional" not in labels
        assert not screen.query("#claimed-complexity")
        assert not screen.query("#time-optimality")

        # The verdict and the recall question are asked of everything.
        verdicts = screen.query_one("#verdict", RadioSet).query(RadioButton)
        assert len(verdicts) == len(problemtypes.load("system-design").verdicts)
        assert "SOLVED AFTER PSEUDOCODE" not in [b.label.plain for b in verdicts]
        assert screen.query_one("#confidence", RadioSet)
        # No judge, so no count of submits to one.
        assert not any("failed submit" in label for label in labels)


async def test_a_designs_answers_go_from_keystrokes_to_the_row(app):
    async with app.run_test() as pilot:
        app.start_run(["rate-limiter"])
        await pilot.pause()
        screen = await _finish(pilot, app)

        screen.query_one("#requirements", Input).value = "7/10"
        screen.query_one("#design", RadioSet).focus()
        await pilot.press("k")      # off "not sure", onto "flawed"
        await pilot.press("space")
        await pilot.press("l")      # across to trade-offs, which is left alone
        await pilot.pause()
        assert app.screen.focused.id == "tradeoffs"
        await pilot.press("h")
        await pilot.pause()
        assert app.screen.focused.id == "design"
        await pilot.press("ctrl+s")
        await pilot.pause()

    row = app.conn.execute("SELECT * FROM attempts").fetchone()
    assert json.loads(row["answers"]) == {
        "requirements": "7/10",
        "design": "flawed",
        "tradeoffs": "unsure",
        "delivery": "unsure",
    }
    # An unanswered field stores nothing, and nothing of LeetCode's was asked.
    assert "bottlenecks" not in json.loads(row["answers"])
    assert row["time_optimality"] is None and row["claimed_complexity"] is None
    assert row["language"] == "markdown"


async def test_what_was_answered_moves_the_review(app):
    """One flawed design and one that covered the lot, otherwise identical."""
    from core import srs

    async with app.run_test() as pilot:
        app.start_run(["rate-limiter", "url-shortener"])
        await pilot.pause()

        screen = await _finish(pilot, app)
        screen.query_one("#requirements", Input).value = "2/10"
        await pilot.press("ctrl+s")
        await pilot.pause()

        screen = await _finish(pilot, app)
        screen.query_one("#requirements", Input).value = "8/10"
        await pilot.press("ctrl+s")
        await pilot.pause()

    missed = srs.card_row(app.conn, "rate-limiter")
    covered = srs.card_row(app.conn, "url-shortener")
    assert missed["due"] < covered["due"]
    assert missed["rungs_left"] == 4  # the failed ladder


async def test_stepping_back_to_the_verdict_keeps_a_designs_answers(asking_app):
    app = asking_app
    async with app.run_test() as pilot:
        app.start_run(["rate-limiter"])
        await pilot.pause()
        screen = await _finish(pilot, app)
        screen.query_one("#requirements", Input).value = "7/10"
        screen.query_one("#delivery", RadioSet).focus()
        await pilot.press("k")
        await pilot.press("space")
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert isinstance(app.screen, StrategyModal)

        await pilot.press("escape")
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, FinishModal)
        assert screen.query_one("#requirements", Input).value == "7/10"
        rambling = problemtypes.load("system-design").field("delivery").values.index("rambling")
        assert screen.query_one("#delivery", RadioSet).pressed_index == rambling


async def test_offline_does_not_ungrade_a_problem_that_never_had_a_judge(app):
    from core import config

    config.set_option(app.conn, "cache.offline", True)
    async with app.run_test() as pilot:
        app.start_run(["rate-limiter", "two-sum"])
        await pilot.pause()
        screen = await _finish(pilot, app)
        pressed = screen.query_one("#verdict", RadioSet).pressed_index
        assert screen.verdicts[pressed] == "solved_unaided"
        await pilot.press("ctrl+s")
        await pilot.pause()

        # The LeetCode problem behind it still does: there, there was one.
        screen = await _finish(pilot, app)
        pressed = screen.query_one("#verdict", RadioSet).pressed_index
        assert screen.verdicts[pressed] == "ungraded"


async def test_ctrl_y_copies_the_types_own_prompt(app, monkeypatch):
    from core import clipboard

    copied: list[str] = []
    monkeypatch.setattr(clipboard, "copy", lambda text: copied.append(text) or True)
    async with app.run_test() as pilot:
        app.start_run(["rate-limiter"])
        await pilot.pause()
        screen = await _finish(pilot, app)
        await pilot.press("ctrl+y")
        await pilot.pause()
        assert copied == [problemtypes.load("system-design").ai_prompt]
        assert copied[0] != app.config.ai.post_solve_prompt
        screen.query_one("#copy-prompt", Button).press()
        await pilot.pause()
        assert len(copied) == 2


async def test_a_type_with_nothing_to_copy_offers_no_copy(isolated_home, monkeypatch):
    from core import clipboard

    paths.ensure_dirs()
    problemtypes.user_path("system-design").write_text(
        '[[fields]]\nkey = "requirements"\nkind = "fraction"\n'
    )
    copied: list[str] = []
    monkeypatch.setattr(clipboard, "copy", lambda text: copied.append(text) or True)
    app = _app(QUIET + SETS)
    async with app.run_test() as pilot:
        app.start_run(["rate-limiter"])
        await pilot.pause()
        screen = await _finish(pilot, app)
        assert not screen.query("#copy-prompt")
        await pilot.press("ctrl+y")
        await pilot.pause()
        assert copied == []
        # The one field it does ask is asked, under its own name.
        assert "requirements" in _labels(screen)


# --- the solve screen ------------------------------------------------------


async def test_there_is_no_failed_submit_without_a_judge(app):
    async with app.run_test() as pilot:
        app.start_run(["rate-limiter", "two-sum"])
        await pilot.pause()
        assert isinstance(app.screen, SolveScreen)
        assert "failed submits" not in _plain(app.screen.query_one("#attempt-state", Static))
        assert "whiteboard" in _plain(app.screen.query_one("#toast", Static))

        await pilot.press("s")
        await pilot.pause()
        assert isinstance(app.screen, SolveScreen)
        assert app.engine.attempt.submissions == 0
        assert app.conn.execute("SELECT COUNT(*) AS n FROM submissions").fetchone()["n"] == 0

        await _finish(pilot, app)
        await pilot.press("ctrl+s")
        await pilot.pause()

        # The next problem is a LeetCode one, and `s` is back with it.
        assert app.engine.attempt.problem.slug == "two-sum"
        assert "failed submits" in _plain(app.screen.query_one("#attempt-state", Static))
        await pilot.press("s")
        await pilot.pause()
        assert app.engine.attempt.submissions == 1


async def test_the_clock_is_measured_against_the_types_par(app):
    async with app.run_test() as pilot:
        app.start_run(["rate-limiter"])  # easy: 30:00 for a design, 15:00 for LeetCode
        await pilot.pause()
        assert "par 30:00" in _plain(app.screen.query_one("#timer", Static))


async def test_a_problem_with_no_link_says_so(app):
    async with app.run_test() as pilot:
        app.start_run(["rate-limiter"])
        await pilot.pause()
        assert "nothing to open" in _plain(app.screen.query_one("#problem-url", Static))
        await pilot.press("o")
        await pilot.pause()
        assert "nothing to open" in _plain(app.screen.query_one("#toast", Static))


# --- the prompts after the verdict ------------------------------------------


async def test_a_design_is_asked_its_patterns_and_not_its_methods(asking_app):
    app = asking_app
    async with app.run_test() as pilot:
        app.start_run(["rate-limiter", "url-shortener"])
        await pilot.pause()
        await _finish(pilot, app)
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert isinstance(app.screen, StrategyModal)

        app.screen.query_one("#strategy-new", Input).focus()
        await pilot.pause()
        await pilot.press(*"token bucket", "enter")
        await pilot.press("ctrl+s")
        await pilot.pause()

        # Straight on to the next problem: no methods page, no offer of a
        # second pass.
        assert isinstance(app.screen, SolveScreen), app.screen
        assert app.engine.attempt.problem.slug == "url-shortener"

    assert [r["key"] for r in app.conn.execute("SELECT key FROM problem_strategies")] == [
        "token-bucket"
    ]
    assert app.conn.execute("SELECT COUNT(*) AS n FROM problem_methods").fetchone()["n"] == 0


async def test_leetcode_is_still_asked_all_of_it(asking_app):
    app = asking_app
    async with app.run_test() as pilot:
        app.start_run(["two-sum"])
        await pilot.pause()
        await _finish(pilot, app)
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert isinstance(app.screen, StrategyModal)
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert isinstance(app.screen, MethodsModal)
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert isinstance(app.screen, ConfirmModal)  # solve it again?


async def test_back_from_methods_skips_a_prompt_the_type_turned_off(isolated_home):
    """One screen back among the ones that ran.

    With patterns off, `esc` on the methods page used to have nowhere to go but
    a prompt that answers itself and sends you forward again.
    """
    paths.ensure_dirs()
    problemtypes.user_path("system-design").write_text(
        '[screens]\npatterns = false\nmethods = true\nagain = false\n'
    )
    app = _app(ASKING + SETS)
    async with app.run_test() as pilot:
        app.start_run(["rate-limiter"])
        await pilot.pause()
        await _finish(pilot, app)
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert isinstance(app.screen, MethodsModal)
        # No time answer on this type, so no quality to read off one.
        assert _plain(app.screen.query_one("#methods-quality", Static)).strip() == ""

        await pilot.press("escape")
        await pilot.pause()
        assert isinstance(app.screen, FinishModal)
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert isinstance(app.screen, MethodsModal)
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert isinstance(app.screen, SummaryScreen)


async def test_changing_the_verdict_to_gave_up_drops_the_patterns(asking_app):
    app = asking_app
    async with app.run_test() as pilot:
        app.start_run(["rate-limiter"])
        await pilot.pause()
        await _finish(pilot, app)
        await pilot.press("ctrl+s")
        await pilot.pause()
        app.screen.query_one("#strategy-new", Input).focus()
        await pilot.pause()
        await pilot.press(*"token bucket", "enter")
        await pilot.press("escape")
        await pilot.pause()

        screen = app.screen
        assert isinstance(screen, FinishModal)
        gave_up = screen.verdicts.index("gave_up")
        screen.query_one("#verdict", RadioSet).query(RadioButton)[gave_up].value = True
        await pilot.press("ctrl+s")
        await pilot.pause()

    assert app.conn.execute("SELECT verdict FROM attempts").fetchone()["verdict"] == "gave_up"
    assert app.conn.execute("SELECT COUNT(*) AS n FROM problem_strategies").fetchone()["n"] == 0


# --- capture ---------------------------------------------------------------


@pytest.fixture
def writing_app(isolated_home, monkeypatch, env_editor):
    """An app whose `$EDITOR` writes a line, with a headless-safe handoff."""
    editor = isolated_home / "fake-editor"
    editor.write_text("#!/bin/sh\nprintf 'a token bucket per key\\n' >> \"$1\"\n")
    editor.chmod(0o755)
    monkeypatch.setenv(env_editor, str(editor))
    app = _app(QUIET.replace("enabled = false\nlanguage", "enabled = true\nlanguage") + SETS)
    monkeypatch.setattr(app, "editor_context", contextlib.nullcontext)
    return app


async def test_a_design_is_written_up_in_markdown(writing_app):
    app = writing_app
    async with app.run_test() as pilot:
        app.start_run(["rate-limiter", "two-sum"])
        await pilot.pause()
        await _finish(pilot, app)
        await pilot.press("ctrl+s")
        await pilot.pause()
        # No second pass is offered for a design; the LeetCode problem is up.
        assert app.engine.attempt.problem.slug == "two-sum"
        await _finish(pilot, app)
        await pilot.press("ctrl+s")
        await pilot.pause()

    rows = {
        r["slug"]: r for r in app.conn.execute("SELECT * FROM attempts").fetchall()
    }
    design, leetcode = rows["rate-limiter"], rows["two-sum"]
    assert design["code_path"].endswith(f"code/rate-limiter/{design['id']}.md")
    assert leetcode["code_path"].endswith(f"code/two-sum/{leetcode['id']}.py")
    written = open(design["code_path"]).read()
    assert written.startswith("<!--") and "write the design up below." in written
    assert "a token bucket per key" in written
    assert design["note_path"] and leetcode["note_path"]


async def test_a_type_can_turn_the_write_up_off_and_keep_the_note(
    isolated_home, monkeypatch, env_editor
):
    paths.ensure_dirs()
    problemtypes.user_path("system-design").write_text(
        "[screens]\nsolution = false\nagain = false\n"
    )
    editor = isolated_home / "fake-editor"
    editor.write_text("#!/bin/sh\nprintf 'should have sized it first\\n' >> \"$1\"\n")
    editor.chmod(0o755)
    monkeypatch.setenv(env_editor, str(editor))
    app = _app(QUIET.replace("enabled = false\nlanguage", "enabled = true\nlanguage") + SETS)
    monkeypatch.setattr(app, "editor_context", contextlib.nullcontext)

    async with app.run_test() as pilot:
        app.start_run(["rate-limiter", "url-shortener"])
        await pilot.pause()
        await _finish(pilot, app)
        await pilot.press("ctrl+s")
        await pilot.pause()
        # On to the next problem, with nothing said about code it never asked for.
        assert app.engine.attempt.problem.slug == "url-shortener"
        assert "code" not in _plain(app.screen.query_one("#toast", Static))

    row = app.conn.execute("SELECT * FROM attempts WHERE slug = 'rate-limiter'").fetchone()
    assert row["code_path"] is None
    assert row["note_path"].endswith(f"notes/rate-limiter/{row['id']}.md")


# --- the summary -----------------------------------------------------------


async def test_the_summary_says_what_the_type_asked(app):
    async with app.run_test() as pilot:
        app.start_run(["rate-limiter"])
        await pilot.pause()
        screen = await _finish(pilot, app)
        screen.query_one("#requirements", Input).value = "9/10"
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert isinstance(app.screen, SummaryScreen)
        text = _plain(app.screen.query_one("#stat-lines", Static))
        assert "coverage" in text and "9/10  ·  90%" in text
        assert "par 30:00" in text


# --- moving between the lists -----------------------------------------------


async def test_the_queue_steps_across_the_lists(app):
    async with app.run_test() as pilot:
        await pilot.press("q")
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, QueueScreen)
        assert screen.lists == ("neetcode150", "blind75", "system-design")
        assert "neetcode150" in _plain(screen.query_one("#queue-title", Static))
        assert "h/l list" in _plain(screen.query_one("#queue-hint", Static))
        leetcode = list(screen.queue.slugs)

        await pilot.press("l", "l")
        await pilot.pause()
        assert screen.active_list == "system-design"
        assert "system-design" in _plain(screen.query_one("#queue-title", Static))
        # The set's own size, not the session's.
        assert len(screen.queue.items) == 2
        designs = {r["slug"] for r in app.conn.execute(
            "SELECT slug FROM problems WHERE type = 'system-design'"
        )}
        assert set(screen.queue.slugs) <= designs

        # Clamped: there is nothing past the last list, and `h` undoes `l`.
        await pilot.press("l")
        await pilot.pause()
        assert screen.active_list == "system-design"
        await pilot.press("h", "h")
        await pilot.pause()
        assert screen.active_list == "neetcode150"
        assert screen.queue.slugs == leetcode

        # Stepping is a way of looking. The setting is where it was.
        assert app.config.session.active_list == "neetcode150"
        assert app.conn.execute("SELECT COUNT(*) AS n FROM settings").fetchone()["n"] == 0


async def test_a_run_starts_from_whichever_queue_is_on_screen(app):
    async with app.run_test() as pilot:
        await pilot.press("q")
        await pilot.pause()
        await pilot.press("l", "l")
        await pilot.pause()
        expected = list(app.screen.queue.slugs)
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, SolveScreen)
        assert app.engine.session.slugs == expected
        assert app.engine.attempt.problem.type == "system-design"


async def test_a_queue_with_one_list_has_nothing_to_step_to(isolated_home):
    app = _app(QUIET)
    async with app.run_test() as pilot:
        await pilot.press("q")
        await pilot.pause()
        # `blind75` is a list too, and always was.
        assert app.screen.lists == ("neetcode150", "blind75")


async def test_a_list_with_nothing_in_it_is_not_a_place_to_step_to(isolated_home):
    app = _app(QUIET + '\n[sets.gone]\ntype = "leetcode"\npath = "sets/nowhere.json"\n')
    async with app.run_test() as pilot:
        await pilot.press("q")
        await pilot.pause()
        assert "gone" not in app.screen.lists


async def test_the_setup_screen_steps_across_the_lists_and_keeps_the_picks(app):
    async with app.run_test() as pilot:
        await pilot.press("n")
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, SetupScreen)
        listing = screen.query_one("#problem-list", SelectionList)
        assert listing.border_title.startswith("neetcode150")

        await pilot.press("ctrl+x")
        await pilot.pause()
        listing.highlighted = 0
        await pilot.press("space")  # the first LeetCode problem
        await pilot.pause()
        first = screen.selected()
        assert len(first) == 1

        await pilot.press("l", "l")
        await pilot.pause()
        assert listing.border_title.startswith("system-design  ·  12 problems")
        assert "1 on another list" in _plain(screen.query_one("#setup-status", Static))
        # The cursor is parked on arrival, so the first keypress does something.
        assert listing.highlighted == 0
        await pilot.press("space")  # and the first design
        await pilot.pause()
        assert len(screen.selected()) == 2

        await pilot.press("ctrl+s")
        await pilot.pause()
        assert isinstance(app.screen, SolveScreen)
        types = [
            app.conn.execute("SELECT type FROM problems WHERE slug = ?", (slug,)).fetchone()["type"]
            for slug in app.engine.session.slugs
        ]
        # One run, two kinds of problem, in catalog order.
        assert types == ["leetcode", "system-design"]


async def test_h_and_l_are_letters_in_the_filter_box(app):
    async with app.run_test() as pilot:
        await pilot.press("n")
        await pilot.pause()
        screen = app.screen
        await pilot.press("slash")
        await pilot.press("h", "l")
        await pilot.pause()
        assert screen.query_one("#filter", Input).value == "hl"
        assert screen.active_list == "neetcode150"


# --- stats -----------------------------------------------------------------


async def test_stats_are_about_one_type_at_a_time(app):
    from core.engine import RunEngine

    clock = {"wall_seconds": 0, "paused_seconds": 0}
    for slug, seconds in (("two-sum", 600), ("rate-limiter", 2400)):
        eng = RunEngine(app.conn)
        # Seeded first: the app has not launched yet, so nothing has synced.
        from core import catalog, config

        catalog.sync(app.conn, config.load(app.conn).sets)
        eng.start_session([slug])
        eng.start_problem(slug)
        eng.finish("solved_unaided", timing={**clock, "active_seconds": seconds})
        eng.advance()
        eng.end_session()

    async with app.run_test() as pilot:
        await pilot.press("t")
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, StatsScreen)
        assert screen.types == ("leetcode", "system-design")
        assert "LeetCode" in _plain(screen.query_one("#stats-title", Static))
        text = _plain(screen.query_one("#stats-content", Static))
        assert "10:00" in text and "40:00" not in text

        await pilot.press("y")
        await pilot.pause()
        assert "system design" in _plain(screen.query_one("#stats-title", Static))
        text = _plain(screen.query_one("#stats-content", Static))
        assert "40:00" in text and "10:00" not in text


async def test_stats_offers_no_type_to_switch_to_when_there_is_one(isolated_home):
    app = _app(QUIET)
    async with app.run_test() as pilot:
        await pilot.press("t")
        await pilot.pause()
        screen = app.screen
        assert screen.types == ("leetcode",)
        assert screen.check_action("cycle_type", ()) is False
        assert "LeetCode" not in _plain(screen.query_one("#stats-title", Static))
