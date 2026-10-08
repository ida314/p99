"""The variants of one method: the same route, written better or worse.

A method is a route and so a big-O. A variant is one way of writing that route,
and the variants of a method differ only in what the big-O leaves out: deduping
the found words with a set at the end, or pruning them out of the trie as they
are found. Both are "trie-guided DFS with backtracking"; the second is better.

Ranked, best at the top and worst at the bottom, and labelled that way so the
order never has to be remembered. `K` and `J` move a row up and down the ranking.

Opened from the methods prompt after a solve, where `space` also marks the one
this pass wrote -- pass 1 the one you reached for, a Solve-again pass the one the
feedback pointed at -- and from the methods screen, where it only edits the list.

Nothing here touches a method's optimality and nothing here reaches `srs.rate`:
that column is a big-O claim, and a constant factor is not a harder review.
"""

from __future__ import annotations

from typing import Any

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, OptionList, Static
from textual.widgets.option_list import Option

from ... import methods
from ..vim import MOTIONS, VimMotion


class VariantsModal(VimMotion, ModalScreen[dict[str, Any] | None]):
    """One method's ranked variants. Dismisses with `{"variants", "variant"}`.

    `variants` is the whole list, best first; `variant` is the one you marked,
    or None. Dismisses with None on `esc`, which keeps everything as it was.
    """

    BINDINGS = [
        *MOTIONS,
        Binding("space", "pick", "wrote this one"),
        Binding("K", "move(-1)", "move up"),
        Binding("J", "move(1)", "move down"),
        Binding("d", "remove", "remove"),
        Binding("i", "focus_new", "add a variant", show=False),
        Binding("slash", "focus_new", "add a variant", show=False),
        Binding("ctrl+s", "save", "save"),
        Binding("escape", "back", "back"),
    ]

    VIM_TARGET = "#variants-list"

    def __init__(
        self,
        method: str,
        variants: list[str],
        picked: str | None = None,
        *,
        pick: bool = True,
    ):
        super().__init__()
        self.method = method
        #: Names, best first. The order *is* the ranking.
        self.names: list[str] = list(variants)
        self.picked = picked if pick else None
        #: False on the methods screen, where there is no pass to mark.
        self.pick = pick
        self._syncing = False

    def compose(self) -> ComposeResult:
        with Vertical(id="variants-box"):
            yield Static(self.method, classes="modal-title")
            yield Static(
                "variants — same big-O, written better or worse", classes="field-label"
            )
            yield Static("best", id="variants-best")
            yield OptionList(id="variants-list")
            yield Static("worst", id="variants-worst")
            yield Input(
                placeholder="another variant…  enter adds it", id="variants-new"
            )
            yield Static(
                ("  space  the one you wrote    " if self.pick else "  ")
                + "K/J  move up/down    d remove    i add    ctrl+s save    esc back",
                classes="hint-bar",
            )
            with Horizontal(id="confirm-buttons"):
                yield Button("save  (ctrl+s)", variant="primary", id="save")
                yield Button("back  (esc)", id="back")

    def on_mount(self) -> None:
        self._populate()
        self.query_one("#variants-list", OptionList).focus()

    # --- the list --------------------------------------------------------

    def _label(self, index: int, name: str) -> Text:
        """`Text`, never a string: a bracket in a variant name is not markup."""
        mine = self.pick and self.picked is not None and (
            methods.normalise(name) == methods.normalise(self.picked)
        )
        line = Text("  ")
        line.append("→ " if mine else "  ", style="bold green" if mine else "")
        line.append(f"{index}  ", style="bright_black")
        line.append(name)
        return line

    def _populate(self, focus: int | None = None) -> None:
        widget = self.query_one("#variants-list", OptionList)
        highlighted = widget.highlighted
        self._syncing = True
        try:
            widget.clear_options()
            if self.names:
                widget.add_options(
                    [
                        Option(self._label(i, name), id=str(i - 1))
                        for i, name in enumerate(self.names, 1)
                    ]
                )
            else:
                widget.add_option(
                    Option(
                        Text(
                            "  none yet — type how you wrote it below and press enter",
                            style="bright_black",
                        ),
                        id=None,
                        disabled=True,
                    )
                )
        finally:
            self._syncing = False
        # The ends only mean something once there are two rows between them.
        two = len(self.names) > 1
        self.query_one("#variants-best", Static).display = two
        self.query_one("#variants-worst", Static).display = two
        if not self.names:
            return
        if focus is not None:
            widget.highlighted = max(0, min(focus, len(self.names) - 1))
        elif highlighted is not None:
            widget.highlighted = min(highlighted, len(self.names) - 1)
        else:
            widget.highlighted = 0

    def _current(self) -> int | None:
        widget = self.query_one("#variants-list", OptionList)
        if widget.highlighted is None or not self.names:
            return None
        return widget.highlighted

    # --- actions ---------------------------------------------------------

    def action_pick(self) -> None:
        """Mark the row as the variant this pass wrote, or unmark it."""
        index = self._current()
        if not self.pick or index is None:
            return
        name = self.names[index]
        same = self.picked is not None and (
            methods.normalise(self.picked) == methods.normalise(name)
        )
        self.picked = None if same else name
        self._populate()

    def action_move(self, step: int) -> None:
        index = self._current()
        if index is None:
            return
        target = index + step
        if not 0 <= target < len(self.names):
            return
        self.names[index], self.names[target] = self.names[target], self.names[index]
        self._populate(focus=target)

    def action_remove(self) -> None:
        """Take a variant off the list. The passes that wrote it keep saying so."""
        index = self._current()
        if index is None:
            return
        name = self.names.pop(index)
        if self.picked is not None and methods.normalise(self.picked) == methods.normalise(name):
            self.picked = None
        self._populate(focus=index)

    def action_focus_new(self) -> None:
        self.query_one("#variants-new", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Enter adds a variant at the bottom, and marks it as this pass's.

        At the bottom because a variant you have only just named has not been
        ranked yet, and the bottom is where the list says "worst" -- `K` lifts it
        in one keystroke. Marked for the reason the methods prompt marks a method
        you type: the one you name seconds after a solve is the one you wrote.
        """
        named = methods.clean([event.value])
        if not named:
            self.query_one("#variants-list", OptionList).focus()
            return
        new = named[0]
        keys = [methods.normalise(n) for n in self.names]
        if new.key in keys:
            index = keys.index(new.key)
        else:
            self.names.append(new.name)
            index = len(self.names) - 1
        if self.pick:
            self.picked = self.names[index]
        self._syncing = True
        try:
            event.input.value = ""
        finally:
            self._syncing = False
        self._populate(focus=index)
        self.query_one("#variants-list", OptionList).focus()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        """`enter` on a row is the same as `space` on it."""
        if not self._syncing:
            self.action_pick()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "save":
            self.action_save()
        else:
            self.action_back()

    def action_save(self) -> None:
        self.dismiss({"variants": list(self.names), "variant": self.picked})

    def action_back(self) -> None:
        """Out of the text box first, then out of the screen, keeping nothing."""
        if getattr(self.focused, "id", None) == "variants-new":
            self.query_one("#variants-list", OptionList).focus()
            return
        self.dismiss(None)
