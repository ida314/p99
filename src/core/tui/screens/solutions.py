"""The solutions prompt: the ways this problem can be solved.

Runs after the strategy prompt and before the `$EDITOR` handoffs. Where that one
asks about you — which technique did you reach for — this one asks about the
*problem*, and the difference is the whole reason it is a second screen.

A problem admits the approaches it admits. "There is an O(n log n) route through
this and I wrote the O(n²)" was true before you sat down and is true after, and
it stays true on every future attempt. That cannot live on an attempt row without
being re-asked and re-answered every time, so it lives on the problem: one row
per way, each carrying whether it is optimal, and each able to hold code.

Both optimal and not, and the not-optimal ones are not clutter. "The O(n²) DP is
the one I can always produce and the O(n log n) patience-sort is the one I have
to think about" is the shape of what you know about this problem, and a list that
only kept the best answer would throw away the half you actually reach for under
pressure.

The list is **cumulative**. It opens holding every way you have ever recorded for
this problem, with the approach you just wrote already marked and already
carrying the cost you claimed at the verdict prompt. A normal night is reading
it, agreeing with it, and pressing `ctrl+s`.

`o` cycles a row through optimal → not optimal → not sure → unclaimed. Unclaimed
is where a row starts and it is not the same as "not sure": one is a question you
have not been asked, the other is an answer. That is also why only a *claim*
carries over from the verdict prompt -- its default is "not sure", and copying
that here would turn a question you skipped into an answer this problem holds
forever. Same distinction `attempts.optimality` was preserved for.

This screen is what `srs.rate` now reads for `saw_better`. Marking a route
optimal that is not the one you wrote is the modern form of "I saw the better
approach" — and unlike the retired `worth_learning` role it can be recorded two
months later, from the browsable copy of this same list, and still be true.

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

from ... import scoring, strategies
from ...render import QUALITY_LABELS, quality_reason, solution_row
from .finish import SIGNAL_BACK
from ..vim import MOTIONS, VimMotion

#: `o` walks this, ending back at None. Unclaimed is a stop on the cycle and not
#: a special case: a row you marked by accident has to be un-markable, and there
#: is no other key on this screen to do it with.
OPTIMALITY_CYCLE = (
    strategies.OPTIMAL,
    strategies.SUBOPTIMAL,
    strategies.UNSURE,
    None,
)


class SolutionsModal(VimMotion, ModalScreen[list[dict[str, Any]] | None]):
    """The problem's ways, editable. Dismisses with a `solutions` payload block.

    Returns None when the list is empty and you changed nothing — a screen you
    walked past records nothing, the same bargain every other prompt here makes.
    `{SIGNAL_BACK: True}` reopens the strategy prompt.
    """

    BINDINGS = [
        *MOTIONS,
        Binding("o", "cycle_optimality", "optimal / not"),
        Binding("i", "focus_new", "add a way", show=False),
        Binding("slash", "focus_new", "add a way", show=False),
        Binding("ctrl+s", "save", "save"),
        Binding("escape", "back", "back"),
    ]

    VIM_TARGET = "#solutions-list"

    def __init__(
        self,
        title: str,
        slug: str,
        attempt: dict | None = None,
        used: list[tuple[str, str]] | None = None,
        picked: list[dict[str, Any]] | None = None,
    ):
        super().__init__()
        self.problem_title = title
        self.slug = slug
        #: The verdict prompt's answers, so the quality line can be derived live.
        #: A plain dict rather than a row: this screen runs before anything is
        #: written, and the attempt it describes does not exist yet.
        self.attempt = dict(attempt or {})
        #: What the strategy prompt just said you wrote, as `(key, name)`. These
        #: are not in the database yet either, so they are merged in by hand.
        self.used = list(used or [])
        #: `{key: name}` and `{key: optimality}` for every row on screen.
        self.names: dict[str, str] = {}
        self.optimality: dict[str, str | None] = {}
        #: Rows that came from the database, so `_changed` can tell an edit from
        #: a list you only read. Keys, and the claim each one arrived with.
        self._stored: dict[str, str | None] = {}
        #: What the rows say beyond what you can edit — the code, the attempt,
        #: the complexity typed at some past finish prompt.
        self._detail: dict[str, strategies.Solution] = {}
        self._order: list[str] = []
        self._syncing = False
        self._restore = list(picked or [])

    # --- composition -----------------------------------------------------

    def compose(self) -> ComposeResult:
        with Vertical(id="solutions-box"):
            yield Static(self.problem_title, classes="modal-title")
            yield Static(
                "ways this problem can be solved — optional", classes="field-label"
            )
            yield OptionList(id="solutions-list")
            yield Input(placeholder="another way to solve it…  enter adds it", id="solutions-new")
            yield Static(id="solutions-quality", classes="panel")
            yield Static(
                "  o  optimal / not optimal / not sure    i add a way"
                "    ctrl+s save    esc back to the approach",
                classes="hint-bar",
            )
            with Horizontal(id="confirm-buttons"):
                yield Button("save  (ctrl+s)", variant="primary", id="save")
                yield Button("back  (esc)", id="back")

    def on_mount(self) -> None:
        conn = self.app.conn  # type: ignore[attr-defined]
        for row in strategies.solutions(conn, self.slug):
            self.names[row.key] = row.name
            self.optimality[row.key] = row.optimality
            self._stored[row.key] = row.optimality
            self._detail[row.key] = row

        # The approach you just named, which has no row yet. It arrives already
        # priced: you answered the time optimality one screen ago and this row is
        # the solution that answer was about, so asking again would be asking
        # twice. Only when the row is new or unclaimed — an existing claim you
        # made deliberately is not overwritten by tonight's default.
        for key, name in self.used:
            self.names.setdefault(key, name)
            self._detail.setdefault(
                key,
                strategies.Solution(
                    key=key,
                    name=name,
                    complexity=self.attempt.get("claimed_complexity"),
                    space_complexity=self.attempt.get("claimed_space_complexity"),
                ),
            )
            if self.optimality.get(key) is None:
                # Only a *claim* carries over. "Not sure" is the verdict
                # prompt's default and is documented as costing nothing --
                # copying it here would turn a question you skipped into an
                # answer this problem holds forever, which is exactly the
                # distinction the null in `problem_solutions.optimality` exists
                # to keep. See the DDL.
                claimed = self.attempt.get("time_optimality")
                self.optimality[key] = (
                    claimed
                    if claimed in (strategies.OPTIMAL, strategies.SUBOPTIMAL)
                    else None
                )

        for entry in self._restore:
            named = strategies.clean([str(entry.get("name") or "")])
            if not named:
                continue
            self.names.setdefault(named[0].key, named[0].name)
            self.optimality[named[0].key] = entry.get("optimality")

        self._resort()
        self._populate()
        self._refresh_quality()
        self.query_one("#solutions-list", OptionList).focus()

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
        return solution_row(
            name=self.names.get(key, key),
            optimality=self.optimality.get(key),
            wrote_it=key in {k for k, _ in self.used},
            complexity=detail.complexity if detail else None,
            written=bool(detail and detail.written),
        )

    def _visible(self) -> list[str]:
        needle = self.query_one("#solutions-new", Input).value.strip().lower()
        if not needle:
            return list(self._order)
        return [k for k in self._order if needle in self.names.get(k, k).lower()]

    def _populate(self, focus_key: str | None = None) -> None:
        """Redraw. `focus_key` parks the cursor on one row — see StrategyModal."""
        widget = self.query_one("#solutions-list", OptionList)
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
                                "  no ways recorded yet — type one below and press enter",
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
        widget = self.query_one("#solutions-list", OptionList)
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
        where the last input lands: `saw_better` is now "an optimal way is
        recorded here that is not the one I wrote", which is a question about the
        list on screen. A derived value nobody sees is a value nobody can catch
        being wrong.
        """
        used_keys = frozenset(k for k, _ in self.used)
        facts = {
            **self.attempt,
            "strategies_used": sorted(self.names[k] for k in used_keys if k in self.names),
            "used_keys": used_keys,
            "saw_better": any(
                self.optimality.get(k) == strategies.OPTIMAL
                for k in self.names
                if k not in used_keys
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
        self.query_one("#solutions-quality", Static).update(body)

    # --- actions ---------------------------------------------------------

    def action_cycle_optimality(self) -> None:
        key = self._current_key()
        if key is None:
            return
        current = self.optimality.get(key)
        index = OPTIMALITY_CYCLE.index(current) if current in OPTIMALITY_CYCLE else -1
        self.optimality[key] = OPTIMALITY_CYCLE[(index + 1) % len(OPTIMALITY_CYCLE)]
        self._populate()
        self._refresh_quality()

    def action_focus_new(self) -> None:
        self.query_one("#solutions-new", Input).focus()

    def on_input_changed(self) -> None:
        if not self._syncing:
            self._populate()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Enter names another way to solve this problem, unclaimed.

        Unclaimed rather than optimal: you have just said the route exists, which
        is worth recording on its own. Being made to price it before you may
        write it down is how a list stops getting written down, and `o` is one
        keystroke away on the row the cursor is already sitting on.
        """
        typed = event.value.strip()
        widget = event.input
        if not typed:
            self.query_one("#solutions-list", OptionList).focus()
            return
        entry = strategies.clean([typed])
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
        self._refresh_quality()
        self.query_one("#solutions-list", OptionList).focus()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        """`enter` on a row is the same as `o` on it."""
        if not self._syncing:
            self.action_cycle_optimality()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "save":
            self.action_save()
        else:
            self.action_back()

    def _entries(self) -> list[dict[str, Any]]:
        return [
            {"name": self.names[key], "optimality": self.optimality.get(key)}
            for key in self._order
        ]

    def _changed(self) -> bool:
        """Did this screen learn anything the database does not already hold?

        A row whose claim you did not touch is not a change, and neither is the
        whole list on a night you read it and moved on. Without this, every solve
        would append a `solutions` block restating what was already true, and the
        log would grow a paragraph a night saying nothing.

        The approach you just wrote is the exception the `or` covers: its row is
        new, or its claim was empty and tonight's verdict filled it in.
        """
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
        if getattr(self.focused, "id", None) == "solutions-new":
            self.query_one("#solutions-list", OptionList).focus()
            return
        self.dismiss({SIGNAL_BACK: True, "picked": self._entries()})
