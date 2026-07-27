"""The vim keymap, including the collisions it exists to prevent.

The motions themselves are cheap to get right and cheap to break — a screen
that forgets `VimMotion` still passes every other test in this suite. These
drive the keys the way fingers do.
"""

from __future__ import annotations

import pytest

from textual.widgets import Input, OptionList, SelectionList

from core import db, paths
from core.tui.app import CoreApp
from core.tui.screens import HistoryScreen, HomeScreen, SetupScreen, SolveScreen

NO_CAPTURE_CONFIG = """
[session]
planned_n = 2

[capture]
enabled = false
"""


@pytest.fixture
def app(isolated_home):
    paths.ensure_dirs()
    paths.config_file().write_text(NO_CAPTURE_CONFIG)
    return CoreApp(db.open_db())


async def open_setup(pilot):
    await pilot.press("n")
    await pilot.pause()
    return pilot.app.screen


# --- the collisions ---------------------------------------------------------


async def test_h_never_reveals_a_hint(app):
    """The reason the hint key moved. `h` is a motion; hints are irreversible."""
    async with app.run_test() as pilot:
        await open_setup(pilot)
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert isinstance(app.screen, SolveScreen)

        for key in ("h", "j", "k", "l", "g"):
            await pilot.press(key)
        await pilot.pause()
        assert app.engine.attempt.max_hint_tier == 0
        assert not app.engine.attempt.finished

        await pilot.press("question_mark")
        await pilot.pause()
        assert app.engine.attempt.max_hint_tier == 1


async def test_g_never_gives_up(app):
    """`g` is half of `gg`. Pressing it must not abandon the attempt."""
    async with app.run_test() as pilot:
        await open_setup(pilot)
        await pilot.press("ctrl+s")
        await pilot.pause()

        await pilot.press("g", "g")
        await pilot.pause()
        assert isinstance(app.screen, SolveScreen)  # no confirm modal
        assert not app.engine.attempt.finished


# --- motions ----------------------------------------------------------------


async def test_jk_move_the_selection_list(app):
    async with app.run_test() as pilot:
        screen = await open_setup(pilot)
        problems = screen.query_one("#problem-list", SelectionList)

        await pilot.press("j", "j", "j")
        assert problems.highlighted == 3
        await pilot.press("k")
        assert problems.highlighted == 2


async def test_gg_and_G_jump_to_the_ends(app):
    async with app.run_test() as pilot:
        screen = await open_setup(pilot)
        problems = screen.query_one("#problem-list", SelectionList)

        await pilot.press("G")
        assert problems.highlighted == problems.option_count - 1
        await pilot.press("g", "g")
        assert problems.highlighted == 0


async def test_a_lone_g_moves_nothing(app):
    """`g` is a prefix, not a command — it waits for its partner."""
    async with app.run_test() as pilot:
        screen = await open_setup(pilot)
        problems = screen.query_one("#problem-list", SelectionList)

        await pilot.press("j", "j")
        await pilot.press("g")
        await pilot.pause()
        assert problems.highlighted == 2


async def test_ctrl_d_moves_further_than_j(app):
    async with app.run_test() as pilot:
        screen = await open_setup(pilot)
        problems = screen.query_one("#problem-list", SelectionList)

        await pilot.press("ctrl+d")
        assert problems.highlighted > 1
        half = problems.highlighted
        await pilot.press("ctrl+u")
        assert problems.highlighted < half


# --- modes ------------------------------------------------------------------


async def test_slash_types_into_the_filter_instead_of_moving(app):
    """The Input has to swallow the motion keys, or the two modes fight.

    The assertion is on the filter's text rather than on the list's highlight:
    every keystroke here repopulates the list, so its highlight says nothing.
    If the motions were firing instead, the field would still be empty.
    """
    async with app.run_test() as pilot:
        screen = await open_setup(pilot)
        await pilot.press("/")
        await pilot.pause()
        assert isinstance(screen.focused, Input)

        await pilot.press("j", "k", "g", "G")
        await pilot.pause()
        assert screen.query_one("#filter", Input).value == "jkgG"


async def test_escape_leaves_the_filter_before_it_leaves_the_screen(app):
    """Escape out of a text field must not throw away the whole selection."""
    async with app.run_test() as pilot:
        screen = await open_setup(pilot)
        await pilot.press("/")
        await pilot.pause()

        await pilot.press("escape")
        await pilot.pause()
        assert isinstance(app.screen, SetupScreen)
        assert isinstance(screen.focused, SelectionList)

        await pilot.press("escape")
        await pilot.pause()
        assert isinstance(app.screen, HomeScreen)


# --- panes and menus --------------------------------------------------------


async def test_the_home_menu_is_navigable(app):
    async with app.run_test() as pilot:
        menu = app.screen.query_one("#menu", OptionList)
        assert menu.highlighted == 0

        await pilot.press("j")  # new run -> runs
        assert menu.highlighted == 1
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, HistoryScreen)


async def test_h_and_l_move_between_the_history_panes(app):
    async with app.run_test() as pilot:
        await pilot.press("r")
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, HistoryScreen)

        await pilot.press("l")
        await pilot.pause()
        assert screen.focused is screen.query_one("#detail-pane")

        await pilot.press("h")
        await pilot.pause()
        assert screen.focused is screen.query_one("#run-list")
