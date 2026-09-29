"""Run setup: pick the problems.

Phase 1 selection is random-from-list or manual pick (spec §15). Both live in
one screen: roll a random N, then edit the selection by hand if you want to.
Phase 2 replaces the roll with the FSRS-driven queue; the manual path stays.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import Screen
from textual.widgets import Footer, Input, SelectionList, Static
from textual.widgets.selection_list import Selection

from rich.text import Text

from ... import catalog, srs
from ...catalog import Problem
from ...render import DIFFICULTY_STYLE, mastered_prefix
from ..vim import MOTIONS, VimMotion

DIFFICULTY_MARK = {"easy": "E", "medium": "M", "hard": "H"}


@dataclass(frozen=True)
class RunPlan:
    """What this screen decides: which problems, and whether to record.

    A record rather than a widened tuple, because the next thing anyone decides
    before a run starts belongs here too and a tuple would have to be unpacked
    at every call site again.
    """

    slugs: list[str] = field(default_factory=list)
    speech_mode: bool = False


class SetupScreen(VimMotion, Screen[RunPlan | None]):
    """Returns the plan for the run, or None if cancelled."""

    # This is the one screen with a text field, so it is the one screen with
    # modes. `/` or `i` puts you in the filter box, `escape` takes you back out
    # to the list, and `escape` from the list leaves. The `Input` swallows
    # `j`/`k` while it has focus, so the two modes never fight over a key.
    BINDINGS = [
        *MOTIONS,
        Binding("escape", "cancel", "back"),
        Binding("slash", "filter", "filter", show=False),
        Binding("i", "filter", "filter", show=False),
        # The count box used to be reachable only with a mouse, in an app whose
        # whole premise is that it isn't one. `c` for count, and `escape` comes
        # back out to the list exactly as it does from the filter.
        Binding("c", "count", "count"),
        Binding("number_sign", "count", "count", show=False),
        Binding("ctrl+r", "roll", "roll random"),
        Binding("ctrl+e", "select_all", "select all"),
        Binding("ctrl+x", "clear", "clear"),
        Binding("ctrl+s", "start", "start run"),
        # A chord, like the other two run-shaping keys: `a` is a character you
        # type into the filter box, and this one turns a microphone on.
        Binding("ctrl+a", "toggle_speech", "speech mode"),
        Binding("f5", "roll", "roll random", show=False),
        # Across the lists, from the problem list. In either text box these are
        # letters you typed: the `Input` swallows them, which is the same
        # insert-mode switch `j` and `k` already rely on.
        Binding("h", "step_list(-1)", "list", show=False),
        Binding("l", "step_list(1)", "list", show=False),
    ]

    VIM_TARGET = "#problem-list"

    def __init__(self, active_list: str, planned_n: int, speech_mode: bool = False):
        super().__init__()
        self.active_list = active_list
        self.planned_n = planned_n
        # Seeded from the setting and overridable for this run only. Nothing is
        # written back: `ctrl+a` is a decision about tonight, not a new default.
        self.speech_mode = speech_mode
        self.problems: list[Problem] = []
        #: Every list there is to step to, and where every problem sits in the
        #: catalog -- the second so a run picked across two lists still starts
        #: in one order. Both settled in `on_mount`.
        self.lists: tuple[str, ...] = (active_list,)
        self.order: dict[str, int] = {}
        self.attempted: set[str] = set()
        self.mastered: set[str] = set()
        # The chosen set lives here, not in the widget: SelectionList only knows
        # about currently-visible options, so filtering would silently drop
        # every pick that scrolled out of the filter.
        self.chosen: set[str] = set()
        self._syncing = False

    def compose(self) -> ComposeResult:
        yield Static("  new run", classes="section-title")
        yield Horizontal(
            Input(placeholder="filter by title, tag or pattern…", id="filter"),
            Input(value=str(self.planned_n), id="count", type="integer"),
            id="setup-controls",
        )
        yield SelectionList(id="problem-list")
        yield Static(id="setup-status")
        yield Vertical(Static(id="setup-hint", classes="hint-bar"))
        yield Footer()

    def on_mount(self) -> None:
        conn = self.app.conn  # type: ignore[attr-defined]
        everything = catalog.all_problems(conn)
        self.order = {p.slug: i for i, p in enumerate(everything)}
        # The list you opened on, and every other that has something in it. A
        # set whose file did not load is not a place worth stepping to.
        named = getattr(getattr(self.app, "config", None), "lists", ())
        self.lists = tuple(
            name
            for name in dict.fromkeys((*named, self.active_list))
            if name == self.active_list or any(name in p.lists for p in everything)
        )
        self.attempted = {
            r["slug"] for r in conn.execute("SELECT DISTINCT slug FROM attempts").fetchall()
        }
        # Read once here rather than per row: this list is 150 long and gets
        # redrawn on every keystroke in the filter box.
        self.mastered = {r["slug"] for r in srs.mastered_cards(conn)}
        self.query_one("#setup-hint", Static).update(
            ("  h/l list    " if len(self.lists) > 1 else "  ")
            + "/ filter    c count    space pick    ctrl+r roll"
            "    ctrl+e all    ctrl+x none    ctrl+a speech    ctrl+s start"
        )
        self._load_list()
        self.action_roll()
        # Land in the list, not the filter box: a set has already been rolled,
        # so the first thing you do is look at it, not type.
        self.query_one("#problem-list", SelectionList).focus()

    def _load_list(self) -> None:
        """Read the list on screen out of the catalog, and say which it is."""
        conn = self.app.conn  # type: ignore[attr-defined]
        self.problems = catalog.all_problems(conn, self.active_list)
        self.query_one("#problem-list", SelectionList).border_title = (
            f"{self.active_list}  ·  {len(self.problems)} problems"
        )

    # --- list ------------------------------------------------------------

    def _matches(self, p: Problem, needle: str) -> bool:
        if not needle:
            return True
        haystack = " ".join([p.title, p.slug, p.pattern or "", *p.tags, p.difficulty]).lower()
        return all(token in haystack for token in needle.lower().split())

    def _label(self, p: Problem) -> Text:
        """The row, as `Text` rather than a string.

        Textual parses a prompt for console markup, so `[E]` was read as a tag
        and the difficulty marker rendered as nothing at all. A `Text` is taken
        literally, which is the only way to print a bracket here.
        """
        mark = DIFFICULTY_MARK.get(p.difficulty, "?")
        seen = "·" if p.slug in self.attempted else " "
        line = Text(f"{seen} ")
        line.append(f"[{mark}]", style=DIFFICULTY_STYLE.get(p.difficulty, ""))
        line.append(" ")
        # The star goes in its own column, not glued to the title, so the names
        # still line up down the list. The `·` beside it already means "seen";
        # this is the stronger claim, and picking a starred problem by hand is
        # still allowed -- it just will not be picked *for* you.
        line.append(mastered_prefix(p.slug in self.mastered))
        line.append(p.title)
        return line

    def _populate(self, needle: str) -> None:
        widget = self.query_one("#problem-list", SelectionList)
        self._syncing = True
        try:
            widget.clear_options()
            widget.add_options(
                [
                    Selection(self._label(p), p.slug, p.slug in self.chosen)
                    for p in self.problems
                    if self._matches(p, needle)
                ]
            )
        finally:
            self._syncing = False
        self._update_status()

    def selected(self) -> list[str]:
        return sorted(self.chosen)

    def _update_status(self) -> None:
        n = self._count()
        # Picks made on a list you have since stepped away from are still picks.
        # Counted apart from the ones the filter is hiding, because the two are
        # undone in different places and the line should say which.
        elsewhere = len(self.chosen - {p.slug for p in self.problems})
        hidden = (
            len(self.chosen)
            - elsewhere
            - len(self.query_one("#problem-list", SelectionList).selected)
        )
        note = ""
        if hidden > 0:
            note = f"   ({hidden} hidden by the filter)"
        elif elsewhere:
            note = f"   ({elsewhere} on another list)"
        elif len(self.chosen) != n:
            note = f"   (ctrl+r rolls {n} — the run uses what's selected)"
        elif n != self.planned_n:
            # Say out loud that the box is a decision about tonight. It has
            # never written back to the settings, but a number you typed into a
            # box has no way of telling you that on its own.
            note = f"   (count {n}, this run only)"
        speech = "on" if self.speech_mode else "off"
        self.query_one("#setup-status", Static).update(
            f"  {len(self.chosen)} selected{note}   ·   speech mode: {speech}"
        )

    def _count(self) -> int:
        try:
            return max(1, int(self.query_one("#count", Input).value or self.planned_n))
        except ValueError:
            return self.planned_n

    # --- events ----------------------------------------------------------

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "filter":
            self._populate(event.value)
        else:
            self._update_status()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        # Enter in the count box means "give me that many", not "go" — you came
        # here to change the number, and starting the run instead would leave
        # the change you just typed unused.
        if event.input.id == "count":
            self.action_roll()
            self.query_one("#problem-list", SelectionList).focus()
            return
        self.action_start()

    def on_selection_list_selected_changed(self, event: SelectionList.SelectedChanged) -> None:
        if self._syncing:
            return  # our own repopulate, not a user click
        widget = event.selection_list
        visible = {widget.get_option_at_index(i).value for i in range(widget.option_count)}
        self.chosen = (self.chosen - visible) | set(widget.selected)
        self._update_status()

    # --- actions ---------------------------------------------------------

    def action_roll(self) -> None:
        """Random-from-list, preferring problems you have never attempted."""
        conn = self.app.conn  # type: ignore[attr-defined]
        picks = catalog.pick_random(
            conn,
            self._count(),
            active_list=self.active_list,
            rng=random.Random(),
        )
        self.chosen = {p.slug for p in picks}
        # Clear the filter so a roll is never partly invisible.
        filter_input = self.query_one("#filter", Input)
        if filter_input.value:
            filter_input.value = ""  # triggers Changed -> _populate
        else:
            self._populate("")

    def action_clear(self) -> None:
        self.chosen = set()
        self._populate(self.query_one("#filter", Input).value)

    def action_select_all(self) -> None:
        """Everything the filter is showing — narrow first, then take the lot."""
        needle = self.query_one("#filter", Input).value
        self.chosen |= {p.slug for p in self.problems if self._matches(p, needle)}
        self._populate(needle)

    def action_step_list(self, delta: int) -> None:
        """The next list along. What you have picked comes with you.

        Clamped rather than wrapping, for the reason the queue screen's is: `h`
        has to undo `l`. Nothing is written to the settings -- this is which
        list you are picking from, not which one the app opens on.

        The picks are kept, so a run can hold a problem from each of two lists.
        That is deliberate: the run loop asks each problem what type it is, not
        the run, and a LeetCode warm-up in front of a design is a reasonable
        evening. `ctrl+r` still replaces the lot with a roll from the list on
        screen, which is the way back to a run drawn from one place.
        """
        if self.active_list not in self.lists:
            return
        index = self.lists.index(self.active_list) + delta
        if not 0 <= index < len(self.lists):
            return
        self.active_list = self.lists[index]
        self._load_list()
        self._populate(self.query_one("#filter", Input).value)
        # Park the cursor on the first row of the list you have arrived at. A
        # rebuilt list leaves it unset, and `space` on an unset cursor picks
        # nothing -- the queue screen parks its own for the same reason.
        listing = self.query_one("#problem-list", SelectionList)
        if listing.option_count:
            listing.highlighted = 0

    def action_toggle_speech(self) -> None:
        """Record this run, or don't. Never a surprise: the status line says which."""
        self.speech_mode = not self.speech_mode
        self._update_status()

    def action_start(self) -> None:
        chosen = self.selected()
        if not chosen:
            self.app.bell()
            self.query_one("#setup-status", Static).update(
                "  nothing selected — ctrl+r rolls a random set"
            )
            return
        # Preserve catalog order so a run interleaves patterns rather than
        # marching through one group (spec §10 makes this a hard constraint).
        # The whole catalog's order, not the list's on screen: picks can come
        # from more than one list, and they all have to sort against something.
        self.dismiss(
            RunPlan(
                slugs=sorted(chosen, key=lambda s: self.order.get(s, 0)),
                speech_mode=self.speech_mode,
            )
        )

    def action_filter(self) -> None:
        """`/` — search, in the only place this app has anything to type into."""
        self.query_one("#filter", Input).focus()

    def action_count(self) -> None:
        """`c` — how many `ctrl+r` rolls. Never written back to the settings."""
        box = self.query_one("#count", Input)
        box.focus()
        box.action_end()

    def action_cancel(self) -> None:
        # From a text field, escape means "leave the field", not "abandon the
        # run I just spent a minute picking".
        if isinstance(self.focused, Input):
            self.query_one("#problem-list", SelectionList).focus()
            return
        self.dismiss(None)
