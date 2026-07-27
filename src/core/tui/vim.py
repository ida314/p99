"""Vim motions, shared by every screen.

The rule this file exists to hold up: **`h j k l` and `g` are motions on every
screen, and no screen may bind them to an action.** That is why the solve screen
reveals a hint on `?` and gives up on `x` — both used to sit on a motion key,
and a reflex `h` that irreversibly reveals a hint tier and costs you points is
not something a keymap should let you do.

Motions are bound on the *screen*, not on the widgets. A key event bubbles
outward from whatever has focus, so an `Input` swallows a literal `j` — typing
in the filter box moves the cursor, not the list behind it — while every other
widget lets the motion through to the screen. That bubbling is the whole
insert-mode/normal-mode switch, and it costs nothing to get.

Textual already binds the arrow keys, Home/End and PageUp/PageDown on its
scrollable widgets. Nothing here removes those; the vim keys are a second way in.
"""

from __future__ import annotations

from textual.binding import Binding
from textual.containers import ScrollableContainer
from textual.css.query import NoMatches
from textual.widget import Widget
from textual.widgets import OptionList, RadioSet

#: How long a bare `g` waits for its partner before it means nothing.
GOTO_TIMEOUT = 0.6

#: Add to a screen's own BINDINGS: `BINDINGS = [*MOTIONS, Binding(...)]`.
#: All hidden — the footer is for what a screen *does*, and motions are the same
#: everywhere, so listing them on every screen is noise.
MOTIONS = [
    Binding("j", "vim_down", "down", show=False),
    Binding("k", "vim_up", "up", show=False),
    Binding("ctrl+d", "vim_half_down", "half page down", show=False),
    Binding("ctrl+u", "vim_half_up", "half page up", show=False),
    Binding("ctrl+f", "vim_page_down", "page down", show=False),
    Binding("ctrl+b", "vim_page_up", "page up", show=False),
    Binding("g", "vim_goto", "top (gg)", show=False),
    Binding("G", "vim_bottom", "bottom", show=False),
]

# RadioSet is a VerticalScroll subclass and OptionList is a ScrollView, so the
# order of these branches matters everywhere below: most specific first.
NAVIGABLE = (OptionList, RadioSet, ScrollableContainer)


def _step(widget: Widget, amount: int) -> None:
    """Move `amount` rows down; negative is up."""
    if isinstance(widget, OptionList):
        count = widget.option_count
        if not count:
            return
        current = widget.highlighted or 0
        widget.highlighted = max(0, min(count - 1, current + amount))
    elif isinstance(widget, RadioSet):
        # One button per keypress whatever the amount: a half-page jump through
        # four radio buttons is not a motion anyone means.
        widget.action_next_button() if amount > 0 else widget.action_previous_button()
    else:
        widget.scroll_relative(y=amount, animate=False)


def _jump(widget: Widget, to_end: bool) -> None:
    if isinstance(widget, OptionList):
        widget.action_last() if to_end else widget.action_first()
    elif isinstance(widget, RadioSet):
        return  # no meaningful top/bottom in a four-item radio set
    else:
        widget.scroll_end(animate=False) if to_end else widget.scroll_home(animate=False)


class VimMotion:
    """Mixin for `Screen`. Motions act on whatever has focus.

    If the focused widget isn't something you can move through — a `Button`, an
    `Input`, nothing at all — they fall back to `VIM_TARGET`, so a screen whose
    content is one scrolling pane doesn't have to focus it to be scrollable.
    """

    #: CSS selector for the pane motions move when focus is elsewhere.
    VIM_TARGET: str | None = None

    # Class-level so the mixin needs no __init__ and screens keep their own.
    _vim_goto_pending = False

    def vim_target(self) -> Widget | None:
        focused = getattr(self, "focused", None)
        if isinstance(focused, NAVIGABLE):
            return focused
        if self.VIM_TARGET:
            try:
                return self.query_one(self.VIM_TARGET)
            except NoMatches:
                return None
        return None

    def _vim_page(self, fraction: float, sign: int) -> None:
        target = self.vim_target()
        if target is None:
            return
        rows = max(1, int(target.size.height * fraction))
        _step(target, rows * sign)

    # --- actions ----------------------------------------------------------

    def action_vim_down(self) -> None:
        if (target := self.vim_target()) is not None:
            _step(target, 1)

    def action_vim_up(self) -> None:
        if (target := self.vim_target()) is not None:
            _step(target, -1)

    def action_vim_half_down(self) -> None:
        self._vim_page(0.5, 1)

    def action_vim_half_up(self) -> None:
        self._vim_page(0.5, -1)

    def action_vim_page_down(self) -> None:
        self._vim_page(1.0, 1)

    def action_vim_page_up(self) -> None:
        self._vim_page(1.0, -1)

    def action_vim_goto(self) -> None:
        """`gg` — the second `g` within `GOTO_TIMEOUT`, exactly like vim.

        A lone `g` does nothing rather than guessing, which is what keeps `g`
        usable as a prefix if there is ever a `gt`/`gd` to add.
        """
        if self._vim_goto_pending:
            self._vim_goto_pending = False
            self.action_vim_top()
            return
        self._vim_goto_pending = True
        self.set_timer(GOTO_TIMEOUT, self._vim_goto_expire)

    def _vim_goto_expire(self) -> None:
        self._vim_goto_pending = False

    def action_vim_top(self) -> None:
        if (target := self.vim_target()) is not None:
            _jump(target, to_end=False)

    def action_vim_bottom(self) -> None:
        if (target := self.vim_target()) is not None:
            _jump(target, to_end=True)
