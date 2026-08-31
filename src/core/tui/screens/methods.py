"""The methods prompt: the ways this problem can be solved.

Runs after the strategy prompt and before the `$EDITOR` handoff. Where that one
asks about you — which patterns did you reach for — this one asks about the
*problem*, and the difference is the whole reason it is a second screen.

A method is a whole route through this one problem: "sort, then two pointers from
both ends". It is usually built out of several of the patterns you just named,
and it says so in its own name rather than in a link — the two lists never
reference each other, because a strategy means the same thing on every problem
and a method means nothing away from its own. See `methods`.

A problem admits the methods it admits. "There is an O(n log n) route through this
and I wrote the O(n²)" was true before you sat down and is true after, and it
stays true on every future attempt. That cannot live on an attempt row without
being re-asked and re-answered every time, so it lives on the problem: one row
per method, each carrying whether it is optimal, and each able to hold code.

Both optimal and not, and the not-optimal ones are not clutter. "The O(n²) DP is
the one I can always produce and the O(n log n) patience-sort is the one I have
to think about" is the shape of what you know about this problem, and a list that
only kept the best answer would throw away the half you actually reach for under
pressure.

The list is **cumulative**. It opens holding every method you have ever recorded
for this problem. A normal night is marking the one you wrote, agreeing with what
the list already says, and pressing `ctrl+s`.

`space` marks the method you wrote tonight. That mark is not decoration: it tags
the file you are about to archive, and it is what `saw_better` compares the
problem's optimal methods against.

`o` cycles a row through optimal → not optimal → not sure → unclaimed. Unclaimed
is where a row starts and it is not the same as "not sure": one is a question you
have not been asked, the other is an answer. That is also why only a *claim*
carries over from the verdict prompt -- its default is "not sure", and copying
that here would turn a question you skipped into an answer this problem holds
forever. Same distinction `attempts.optimality` was preserved for.

This screen is what `srs.rate` reads for `saw_better`. Marking a method optimal
that is not the one you wrote is the modern form of "I saw the better approach" —
and unlike the retired `worth_learning` role it can be recorded two months later,
from the browsable copy of this same list, and still be true.

`esc` steps back to the strategy prompt with everything intact, which steps back
to the verdict prompt in turn. Nothing is written until all three are done.
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

from ... import methods, scoring
from ...render import QUALITY_LABELS, method_row, quality_reason
from .finish import SIGNAL_BACK
from ..vim import MOTIONS, VimMotion

#: `o` walks this, ending back at None. Unclaimed is a stop on the cycle and not
#: a special case: a row you marked by accident has to be un-markable, and there
#: is no other key on this screen to do it with.
OPTIMALITY_CYCLE = (
    methods.OPTIMAL,
    methods.SUBOPTIMAL,
    methods.UNSURE,
    None,
)


class MethodsModal(VimMotion, ModalScreen[list[dict[str, Any]] | None]):
    """The problem's methods, editable. Dismisses with a `methods` payload block.

    Returns None when the list is empty and you changed nothing — a screen you
    walked past records nothing, the same bargain every other prompt here makes.
    `{SIGNAL_BACK: True}` reopens the strategy prompt.
    """

    BINDINGS = [
        *MOTIONS,
        Binding("space", "toggle_used", "wrote this one"),
        Binding("o", "cycle_optimality", "optimal / not"),
        Binding("i", "focus_new", "add a method", show=False),
        Binding("slash", "focus_new", "add a method", show=False),
        Binding("ctrl+s", "save", "save"),
        Binding("escape", "back", "back"),
    ]

    VIM_TARGET = "#methods-list"

    def __init__(
        self,
        title: str,
        slug: str,
        attempt: dict | None = None,
        picked: list[dict[str, Any]] | None = None,
    ):
        super().__init__()
        self.problem_title = title
        self.slug = slug
        #: The verdict prompt's answers, so the quality line can be derived live.
        #: A plain dict rather than a row: this screen runs before anything is
        #: written, and the attempt it describes does not exist yet.
        self.attempt = dict(attempt or {})
        #: `{key: name}` and `{key: optimality}` for every row on screen.
        self.names: dict[str, str] = {}
        self.optimality: dict[str, str | None] = {}
        #: The methods you wrote tonight. Held on the screen rather than in the
        #: widget, like `StrategyModal.chosen` -- an OptionList only knows the
        #: rows it is currently showing, so filtering would silently drop a mark
        #: that scrolled out of view.
        self.used: set[str] = set()
        #: Rows that came from the database, so `_changed` can tell an edit from
        #: a list you only read. Keys, and the claim each one arrived with.
        self._stored: dict[str, str | None] = {}
        #: What the rows say beyond what you can edit — the code, the attempt,
        #: the complexity typed at some past finish prompt.
        self._detail: dict[str, methods.Method] = {}
        #: Keys whose claim came from the verdict prompt rather than from you.
        #: Tracked so that unmarking a row takes the carried claim back off it:
        #: "not optimal" was an answer about the route you *wrote*, and a row you
        #: have just said you did not write has no business keeping it. A claim
        #: you made yourself with `o` leaves this set and stays.
        self._carried: set[str] = set()
        self._order: list[str] = []
        self._syncing = False
        self._restore = list(picked or [])

    # --- composition -----------------------------------------------------

    def compose(self) -> ComposeResult:
        with Vertical(id="methods-box"):
            yield Static(self.problem_title, classes="modal-title")
            yield Static(
                "ways this problem can be solved — optional", classes="field-label"
            )
            yield OptionList(id="methods-list")
            yield Input(
                placeholder="another way to solve it…  enter adds it", id="methods-new"
            )
            yield Static(id="methods-quality", classes="panel")
            yield Static(
                "  space  the one you wrote    o  optimal / not optimal / not sure"
                "    i add one    ctrl+s save    esc back",
                classes="hint-bar",
            )
            with Horizontal(id="confirm-buttons"):
                yield Button("save  (ctrl+s)", variant="primary", id="save")
                yield Button("back  (esc)", id="back")

    def on_mount(self) -> None:
        conn = self.app.conn  # type: ignore[attr-defined]
        for row in methods.for_problem(conn, self.slug):
            self.names[row.key] = row.name
            self.optimality[row.key] = row.optimality
            self._stored[row.key] = row.optimality
            self._detail[row.key] = row

        # What was marked before a step back, including a method that only exists
        # because you typed it: it is in `_restore` and not in the database yet,
        # so it has to be put back on the list as well as back in the marks.
        for entry in self._restore:
            named = methods.clean([str(entry.get("name") or "")])
            if not named:
                continue
            key = named[0].key
            self.names.setdefault(key, named[0].name)
            self.optimality[key] = entry.get("optimality")
            if entry.get("used"):
                self.used.add(key)

        self._resort()
        self._populate()
        self._refresh_quality()
        self.query_one("#methods-list", OptionList).focus()

    def _resort(self) -> None:
        """Alphabetical, always. A list that reorders itself as you edit it is a
        list you have to re-read after every keystroke."""
        self._order = sorted(self.names, key=lambda k: self.names[k].lower())

    # --- the list --------------------------------------------------------

    def _label(self, key: str) -> Text:
        """One row, as `Text` and never a plain string.

        Textual reads a prompt for console markup, so a bracketed marker would
        be parsed as a tag and vanish. Same trap `StrategyModal._label`
        documents; it fails silently both times.
        """
        detail = self._detail.get(key)
        return method_row(
            name=self.names.get(key, key),
            optimality=self.optimality.get(key),
            wrote_it=key in self.used,
            complexity=(
                self.attempt.get("claimed_complexity")
                if key in self.used
                else detail.complexity if detail else None
            ),
            written=bool(detail and detail.written),
        )

    def _visible(self) -> list[str]:
        needle = self.query_one("#methods-new", Input).value.strip().lower()
        if not needle:
            return list(self._order)
        return [k for k in self._order if needle in self.names.get(k, k).lower()]

    def _populate(self, focus_key: str | None = None) -> None:
        """Redraw. `focus_key` parks the cursor on one row — see StrategyModal."""
        widget = self.query_one("#methods-list", OptionList)
        highlighted = widget.highlighted
        self._syncing = True
        try:
            widget.clear_options()
            keys = self._visible()
            if keys:
                widget.add_options([Option(self._label(k), id=k) for k in keys])
            else:
                widget.add_options(
                    [
                        Option(
                            Text(
                                "  nothing recorded yet — type the way you solved "
                                "it below and press enter",
                                style="bright_black",
                            ),
                            id=None,
                            disabled=True,
                        )
                    ]
                )
        finally:
            self._syncing = False
        if not widget.option_count:
            return
        if focus_key is not None and focus_key in keys:
            widget.highlighted = keys.index(focus_key)
        elif highlighted is not None:
            widget.highlighted = min(highlighted, widget.option_count - 1)
        else:
            widget.highlighted = 0

    def _current_key(self) -> str | None:
        widget = self.query_one("#methods-list", OptionList)
        if widget.highlighted is None or not widget.option_count:
            return None
        try:
            return widget.get_option_at_index(widget.highlighted).id
        except Exception:
            return None

    # --- the derived line ------------------------------------------------

    def _refresh_quality(self) -> None:
        """What the answers so far come to, and what decided it.

        Live, and on this screen rather than the one before it, because this is
        where the last input lands: `saw_better` is "an optimal method is
        recorded here that is not the one I wrote", which is a question about the
        list on screen. A derived value nobody sees is a value nobody can catch
        being wrong.
        """
        facts = {
            **self.attempt,
            "methods_used": sorted(
                self.names[k] for k in self.used if k in self.names
            ),
            "saw_better": any(
                self.optimality.get(k) == methods.OPTIMAL
                for k in self.names
                if k not in self.used
            ),
        }
        quality = scoring.solution_quality(facts)
        facts["solution_quality"] = quality

        body = Text()
        body.append("  quality   ", style="bright_black")
        if quality is None:
            body.append("not claimed", style="bright_black")
            body.append("\n            you answered 'not sure' on time", style="bright_black")
        else:
            body.append(QUALITY_LABELS.get(quality, quality))
            reason = quality_reason(facts)
            if reason:
                body.append(f"\n            {reason}", style="bright_black")
        self.query_one("#methods-quality", Static).update(body)

    # --- actions ---------------------------------------------------------

    def action_toggle_used(self) -> None:
        """Mark the row as the method you wrote tonight, or unmark it.

        Marking it also prices it, once: the time optimality you answered one
        screen ago was an answer about the route you took, and this row is that
        route. Only when the row is unclaimed — a claim you made deliberately is
        not overwritten by tonight's default — and only a *claim* carries, never
        the "not sure" that is the verdict prompt's own default.
        """
        key = self._current_key()
        if key is None:
            return
        self.used.symmetric_difference_update({key})
        if key in self.used:
            if self.optimality.get(key) is None:
                claimed = self.attempt.get("time_optimality")
                if claimed in (methods.OPTIMAL, methods.SUBOPTIMAL):
                    self.optimality[key] = claimed
                    self._carried.add(key)
        elif key in self._carried:
            self.optimality[key] = None
            self._carried.discard(key)
        self._populate()
        self._refresh_quality()

    def action_cycle_optimality(self) -> None:
        key = self._current_key()
        if key is None:
            return
        current = self.optimality.get(key)
        index = OPTIMALITY_CYCLE.index(current) if current in OPTIMALITY_CYCLE else -1
        self.optimality[key] = OPTIMALITY_CYCLE[(index + 1) % len(OPTIMALITY_CYCLE)]
        # Yours now, whatever it was before: `o` is you answering the question,
        # and an answer you gave is not taken back off the row by a `space`.
        self._carried.discard(key)
        self._populate()
        self._refresh_quality()

    def action_focus_new(self) -> None:
        self.query_one("#methods-new", Input).focus()

    def on_input_changed(self) -> None:
        if not self._syncing:
            self._populate()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Enter names another way to solve this problem, and marks it written.

        Marked, not merely added: you typed it on the screen that runs seconds
        after you solved the problem, so the route you are naming is
        overwhelmingly the one you just took. `space` on the row undoes it in one
        keystroke, which is the right way round -- the same bargain
        `StrategyModal` makes with the vocabulary.
        """
        typed = event.value.strip()
        widget = event.input
        if not typed:
            self.query_one("#methods-list", OptionList).focus()
            return
        entry = methods.clean([typed])
        if not entry:
            return
        new = entry[0]
        self.names.setdefault(new.key, new.name)
        self.optimality.setdefault(new.key, None)
        self._resort()
        self._syncing = True
        try:
            widget.value = ""
        finally:
            self._syncing = False
        self._populate(focus_key=new.key)
        if new.key not in self.used:
            self.action_toggle_used()
        self._refresh_quality()
        self.query_one("#methods-list", OptionList).focus()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        """`enter` on a row is the same as `space` on it."""
        if not self._syncing:
            self.action_toggle_used()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "save":
            self.action_save()
        else:
            self.action_back()

    def _entries(self) -> list[dict[str, Any]]:
        return [
            {
                "name": self.names[key],
                "optimality": self.optimality.get(key),
                "used": key in self.used,
            }
            for key in self._order
        ]

    def _changed(self) -> bool:
        """Did this screen learn anything the database does not already hold?

        A row whose claim you did not touch is not a change, and neither is the
        whole list on a night you read it and moved on. Without this, every solve
        would append a `methods` block restating what was already true, and the
        log would grow a paragraph a night saying nothing.

        Marking the method you wrote is always a change: it is this attempt's own
        answer, and no previous night can have recorded it.
        """
        if self.used:
            return True
        for key in self._order:
            if key not in self._stored or self._stored[key] != self.optimality.get(key):
                return True
        return False

    def action_save(self) -> None:
        self.dismiss(self._entries() if self._changed() else None)

    def action_back(self) -> None:
        """Step back to the strategy prompt, keeping everything.

        Not an undo and not a cancel: nothing about this attempt has been written
        yet, so the strategy prompt reopens as the live screen it was, and one
        more `esc` reaches the verdict.

        `escape` inside the text box goes back to the list instead, so the way
        out of insert mode is never also the way out of the screen.
        """
        if getattr(self.focused, "id", None) == "methods-new":
            self.query_one("#methods-list", OptionList).focus()
            return
        self.dismiss({SIGNAL_BACK: True, "picked": self._entries()})
