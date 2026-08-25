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
from core.tui.screens.home import MENU

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
    """`j` moves and enter opens whatever it landed on, whatever that is.

    Indexed off `MENU` rather than hard-coded, so adding an entry moves the
    menu without silently changing what this asserts.
    """
    async with app.run_test() as pilot:
        menu = app.screen.query_one("#menu", OptionList)
        assert menu.highlighted == 0

        target = [action for _, action, _ in MENU].index("history")
        await pilot.press(*["j"] * target)
        assert menu.highlighted == target
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, HistoryScreen)


async def test_settings_values_move_sideways_and_come_back(app):
    """`h`/`l` may change a value only because `h` puts back what `l` took."""
    async with app.run_test() as pilot:
        await pilot.press("s")
        await pilot.pause()
        before = app.config.session.planned_n

        await pilot.press("j", "j", "j")  # problems per run
        await pilot.press("l")
        await pilot.pause()
        assert app.config.session.planned_n != before

        await pilot.press("h")
        await pilot.pause()
        assert app.config.session.planned_n == before


async def test_pure_motions_never_change_a_setting(app):
    async with app.run_test() as pilot:
        await pilot.press("s")
        await pilot.pause()
        before = app.config

        for key in ("j", "k", "g", "g", "G", "ctrl+d", "ctrl+u"):
            await pilot.press(key)
        await pilot.pause()
        assert app.config == before


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


async def test_the_home_menu_is_one_list_in_three_widgets(app):
    """`h`/`l` cross the columns, and exactly one cursor is ever on screen.

    Three `OptionList`s draw what reads as one menu, so the invariant worth
    asserting is not where the cursor is — it is that there is only one.
    """
    from textual.widgets import OptionList as OL

    async with app.run_test() as pilot:
        screen = app.screen
        lists = [screen.query_one(s, OL) for s in ("#menu-resume", "#menu", "#menu-right")]

        def cursors():
            return [i for i, w in enumerate(lists) if w.highlighted is not None]

        assert cursors() == [1]  # nothing to resume, so the left column leads

        await pilot.press("l")
        assert cursors() == [2]
        await pilot.press("l")  # clamped: `h` has to undo `l`, so no wrap
        assert cursors() == [2]
        await pilot.press("h")
        assert cursors() == [1]
        await pilot.press("h")
        assert cursors() == [1]


async def test_the_columns_split_the_menu_in_written_order(app):
    """Column-major, so `j` still walks `MENU` the way it is written."""
    from textual.widgets import OptionList as OL

    async with app.run_test() as pilot:
        left = app.screen.query_one("#menu", OL)
        right = app.screen.query_one("#menu-right", OL)
        ids = [o.id for o in left._options] + [o.id for o in right._options]
        assert ids == [action for _, action, _ in MENU]
        # The left column takes the extra row when the count is odd.
        assert left.option_count >= right.option_count
        await pilot.pause()


# --- the strategy prompt ----------------------------------------------------

STRATEGY_CONFIG = """
[session]
planned_n = 1

[capture]
enabled = false

[strategy]
enabled = true
"""


@pytest.fixture
def strategy_app(isolated_home):
    paths.ensure_dirs()
    paths.config_file().write_text(STRATEGY_CONFIG)
    return CoreApp(db.open_db())


async def _open_strategy_prompt(app, pilot):
    from core.tui.screens import StrategyModal

    await pilot.press("n")
    await pilot.pause()
    await pilot.press("ctrl+s")
    await pilot.pause()
    await pilot.press("f")
    await pilot.pause()
    await pilot.press("ctrl+s")
    await pilot.pause()
    assert isinstance(app.screen, StrategyModal)
    return app.screen


async def test_motions_never_pick_a_strategy(strategy_app):
    """`w` is the only letter this screen binds. Every motion has to stay one.

    A reflex `j` that quietly marks the highlighted approach as the one you
    wrote would put a claim in the log that you never made — and the claim moves
    a review, so it is not a cosmetic mistake.
    """
    app = strategy_app
    async with app.run_test() as pilot:
        screen = await _open_strategy_prompt(app, pilot)
        # Seed the list, so the motions have something to land on.
        await pilot.press("i")
        screen.query_one("#strategy-new", Input).value = "binary search"
        await pilot.press("enter")
        await pilot.pause()
        screen.roles.clear()
        screen._populate()

        for key in ("j", "k", "g", "g", "G", "ctrl+d", "ctrl+u", "ctrl+f", "ctrl+b", "h", "l"):
            await pilot.press(key)
            await pilot.pause()
        assert screen.roles == {}


async def test_escape_leaves_the_strategy_box_before_it_leaves_the_screen(strategy_app):
    """The way out of insert mode is never also the way out of the prompt.

    Two presses, two different meanings, and the order is the whole rule: the
    first leaves the text box for the list, and only the second steps the
    screen back to the verdict.
    """
    from core.tui.screens import FinishModal, StrategyModal

    app = strategy_app
    async with app.run_test() as pilot:
        await _open_strategy_prompt(app, pilot)
        await pilot.press("i")
        assert app.screen.focused.id == "strategy-new"

        await pilot.press("escape")
        await pilot.pause()
        assert isinstance(app.screen, StrategyModal)
        assert app.screen.focused.id == "strategy-list"

        await pilot.press("escape")
        await pilot.pause()
        assert isinstance(app.screen, FinishModal)


async def test_w_types_into_the_box_instead_of_flagging(strategy_app):
    """With focus in the text box, `w` is a letter. It is only a key outside it."""
    app = strategy_app
    async with app.run_test() as pilot:
        screen = await _open_strategy_prompt(app, pilot)
        await pilot.press("i")
        await pilot.press("w")
        await pilot.pause()
        assert screen.query_one("#strategy-new", Input).value == "w"
        assert screen.roles == {}
