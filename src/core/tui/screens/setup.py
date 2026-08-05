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

from ... import catalog
from ...catalog import Problem
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
        Binding("ctrl+r", "roll", "roll random"),
        Binding("ctrl+x", "clear", "clear"),
        Binding("ctrl+s", "start", "start run"),
        # A chord, like the other two run-shaping keys: `a` is a character you
        # type into the filter box, and this one turns a microphone on.
        Binding("ctrl+a", "toggle_speech", "speech mode"),
        Binding("f5", "roll", "roll random", show=False),
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
        self.attempted: set[str] = set()
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
        yield Vertical(
            Static(
                "  / filter    j/k move    space pick    ctrl+r roll"
                "    ctrl+a speech    ctrl+s start",
                classes="hint-bar",
            )
        )
        yield Footer()

    def on_mount(self) -> None:
        conn = self.app.conn  # type: ignore[attr-defined]
        self.problems = catalog.all_problems(conn, self.active_list)
        self.attempted = {
            r["slug"] for r in conn.execute("SELECT DISTINCT slug FROM attempts").fetchall()
        }
        self.query_one("#problem-list", SelectionList).border_title = (
            f"{self.active_list}  ·  {len(self.problems)} problems"
        )
        self._populate("")
        self.action_roll()
        # Land in the list, not the filter box: a set has already been rolled,
        # so the first thing you do is look at it, not type.
        self.query_one("#problem-list", SelectionList).focus()

    # --- list ------------------------------------------------------------

    def _matches(self, p: Problem, needle: str) -> bool:
        if not needle:
            return True
        haystack = " ".join([p.title, p.slug, p.pattern or "", *p.tags, p.difficulty]).lower()
        return all(token in haystack for token in needle.lower().split())

    def _label(self, p: Problem) -> str:
        mark = DIFFICULTY_MARK.get(p.difficulty, "?")
        seen = "·" if p.slug in self.attempted else " "
        return f"{seen} [{mark}] {p.title}"

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
        hidden = len(self.chosen) - len(self.query_one("#problem-list", SelectionList).selected)
        note = ""
        if hidden > 0:
            note = f"   ({hidden} hidden by the filter)"
        elif len(self.chosen) != n:
            note = f"   (count says {n} — the run uses what's selected)"
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
        order = {p.slug: i for i, p in enumerate(self.problems)}
        self.dismiss(
            RunPlan(
                slugs=sorted(chosen, key=lambda s: order.get(s, 0)),
                speech_mode=self.speech_mode,
            )
        )

    def action_filter(self) -> None:
        """`/` — search, in the only place this app has anything to type into."""
        self.query_one("#filter", Input).focus()

    def action_cancel(self) -> None:
        # From a text field, escape means "leave the field", not "abandon the
        # run I just spent a minute picking".
        if isinstance(self.focused, Input):
            self.query_one("#problem-list", SelectionList).focus()
            return
        self.dismiss(None)
