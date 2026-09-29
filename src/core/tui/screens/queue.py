"""The morning queue (spec §15 Phase 2, item 9).

What to do today and why, chosen by `core.queues` rather than by a roll. The
`n` / setup path is untouched and still there: this screen is the scheduler's
opinion, not a replacement for picking problems yourself.

The queue is generated on first open rather than by a nightly job — Phase 3
adds the cron. Either way it is never empty and never a blank screen asking you
to press a key first (spec §10): that rule is the difference between a coach you
trust and one you stop opening.

Every row arrives checked, and unchecking one shortens tonight's run without
touching tonight's queue. The scheduler's opinion is a separate thing from how
much time you actually have, and making you regenerate until the queue happened
to be short enough conflated the two.

One queue per list. Each problem set is its own track -- LeetCode on one, system
design on another -- with its own plan for today, its own size and its own
review budget, and `h`/`l` step across them. The screen opens on
`session.active_list` and stepping changes nothing but what you are looking at.
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import Screen
from textual.widgets import Footer, SelectionList, Static
from textual.widgets.selection_list import Selection

from rich.text import Text

from ... import catalog, queues
from ...render import queue_row
from ..vim import MOTIONS, VimMotion


class QueueList(SelectionList):
    """The queue's rows, with `enter` still meaning "start".

    `OptionList` binds `enter` to select and `SelectionList` makes that a
    toggle, and a widget binding beats the screen's — so without this, adding
    checkboxes would quietly retire the key that has always started the run.
    `space` is left alone and stays the toggle, exactly as on the setup screen.
    """

    BINDINGS = [Binding("enter", "start_run", "start run", show=False)]

    def action_start_run(self) -> None:
        self.screen.action_start()  # type: ignore[attr-defined]


class QueueScreen(VimMotion, Screen[None]):
    # `ctrl+r` regenerates and `ctrl+s` starts, matching the setup screen's roll
    # and start rather than inventing a second vocabulary for the same two
    # actions — and `ctrl+e` / `ctrl+x` pick and unpick everything for the same
    # reason.
    #
    # `h`/`l` step across the lists. They were unbound while there was one list
    # and nothing sideways to move through; with a queue per list there is, and
    # it is the kind of move the motion rule allows -- nothing sticks, and `h`
    # puts back exactly what `l` took.
    BINDINGS = [
        *MOTIONS,
        Binding("h", "step_list(-1)", "list", show=False),
        Binding("l", "step_list(1)", "list", show=False),
        Binding("left", "step_list(-1)", "list", show=False),
        Binding("right", "step_list(1)", "list", show=False),
        Binding("escape", "back", "back"),
        Binding("q", "back", "back", show=False),
        Binding("enter", "start", "start run"),
        Binding("ctrl+s", "start", "start run", show=False),
        Binding("ctrl+r", "regenerate", "regenerate"),
        Binding("ctrl+e", "select_all", "select all"),
        Binding("ctrl+x", "clear", "clear"),
    ]

    #: The rows themselves, so motions move the cursor rather than a pane.
    VIM_TARGET = "#queue-list"

    def __init__(self) -> None:
        super().__init__()
        self.queue: queues.Queue | None = None
        #: The list on screen, and every list there is to step to. Settled in
        #: `on_mount`, once there is an app to ask.
        self.active_list = catalog.DEFAULT_LIST
        self.lists: tuple[str, ...] = (catalog.DEFAULT_LIST,)

    def compose(self) -> ComposeResult:
        yield Static(id="queue-title", classes="section-title")
        yield QueueList(id="queue-list")
        yield Static(id="queue-status")
        yield Static(id="queue-rationale")
        yield Static(id="queue-hint", classes="hint-bar")
        yield Footer()

    def on_mount(self) -> None:
        app = self.app  # type: ignore[attr-defined]
        self.active_list = app.config.session.active_list
        self.lists = self._lists()
        self.query_one("#queue-hint", Static).update(
            ("  h/l list    " if len(self.lists) > 1 else "  ")
            + "space pick    ctrl+e all    ctrl+x none    enter start"
            "    ctrl+r regenerate    q back"
        )
        self.load(regenerate=False)
        self.query_one("#queue-list", QueueList).focus()

    def _lists(self) -> tuple[str, ...]:
        """The lists worth stepping to: the one you opened on, and every other
        that has a problem in it.

        A set whose file did not load is a list with nothing in it, and stepping
        onto it would draw up an empty queue -- and log that it had -- every
        time you passed through. `doctor` is where a set that did not load is
        reported; here it is simply not a place to go.
        """
        app = self.app  # type: ignore[attr-defined]
        return tuple(
            name
            for name in app.config.lists
            if name == self.active_list or catalog.all_problems(app.conn, name)
        ) or (self.active_list,)

    def load(self, *, regenerate: bool) -> None:
        app = self.app  # type: ignore[attr-defined]
        self.queue = queues.ensure(
            app.conn,
            # The list's own numbers where its set gives them, and the session's
            # where it does not: three LeetCode problems is an evening, and
            # three system designs is not.
            n=app.config.queue_n_for(self.active_list),
            active_list=self.active_list,
            weights=app.weights,
            regenerate=regenerate,
            reviews_per_day=app.config.reviews_per_day_for(self.active_list),
        )
        self.query_one("#queue-title", Static).update(
            f"  today's queue  ·  {self.active_list}  ·  {self.queue.date}"
        )
        self._populate()

    # --- rendering --------------------------------------------------------

    def _populate(self) -> None:
        """Draw the rows, checked. The queue is the default; edits are yours.

        Except the ones you have already worked today, which arrive unchecked:
        the queue is a plan for the day and the day moves, so a row you solved
        this morning is still part of the plan and is not part of tonight's run.
        Left checked, `enter` would restart problems you just finished.
        """
        widget = self.query_one("#queue-list", QueueList)
        widget.clear_options()
        if self.queue is not None:
            widget.add_options(
                [
                    Selection(queue_row(n, item), item.slug, not item.done)
                    for n, item in enumerate(self.queue.items, 1)
                ]
            )
            # Park the cursor on the first row. An `OptionList` leaves it unset
            # until something moves it, and `space` on an unset cursor toggles
            # nothing — a picking screen whose first keypress does nothing at
            # all reads as broken.
            if widget.option_count:
                widget.highlighted = 0
        self._update_status()

    def selected(self) -> list[str]:
        """The checked slugs, in the queue's order.

        Order comes from the queue rather than from the widget because the run
        order is a scheduling decision (spec §10 interleaves patterns), not a
        record of which box you happened to click first.
        """
        checked = set(self.query_one("#queue-list", QueueList).selected)
        if self.queue is None:
            return []
        return [i.slug for i in self.queue.items if i.slug in checked]

    def _update_status(self) -> None:
        if self.queue is None:
            return
        chosen = set(self.selected())
        items = [i for i in self.queue.items if i.slug in chosen]
        reviews = sum(1 for i in items if i.is_review)
        line = Text(f"  {len(items)} of {len(self.queue.items)} selected", style="bold")
        line.append("   ", style="bright_black")
        line.append(f"{reviews} review", style="yellow")
        line.append(" · ", style="bright_black")
        line.append(f"{len(items) - reviews} new", style="bright_black")
        # Says why the count is short of the queue before you go looking for the
        # row you think you unchecked.
        done = sum(1 for i in self.queue.items if i.done)
        if done:
            line.append(" · ", style="bright_black")
            line.append(f"{done} done today", style="green")
        self.query_one("#queue-status", Static).update(line)
        self.query_one("#queue-rationale", Static).update(
            Text(f"  {self.queue.rationale}", style="bright_black italic")
            if self.queue.rationale
            else ""
        )

    # --- events -----------------------------------------------------------

    def on_selection_list_selected_changed(self, event: SelectionList.SelectedChanged) -> None:
        self._update_status()

    # --- actions ----------------------------------------------------------

    def action_step_list(self, delta: int) -> None:
        """The next list along, and its queue for today.

        Clamped rather than wrapping, like the home menu's columns: `h` has to
        undo `l`, and a wrap makes the pair a cycle instead of an axis.

        Nothing is written to the settings. Which list you are looking at is a
        fact about this screen; which one the app opens on is
        `session.active_list`, and changing that is what the settings screen is
        for. What you unchecked on the list you are leaving is dropped with it,
        for the reason it has never been saved anywhere: it was a fact about
        tonight.
        """
        if self.active_list not in self.lists:
            return
        index = self.lists.index(self.active_list) + delta
        if not 0 <= index < len(self.lists):
            return
        self.active_list = self.lists[index]
        self.load(regenerate=False)

    def action_regenerate(self) -> None:
        self.load(regenerate=True)

    def action_select_all(self) -> None:
        self.query_one("#queue-list", QueueList).select_all()

    def action_clear(self) -> None:
        self.query_one("#queue-list", QueueList).deselect_all()

    def action_start(self) -> None:
        slugs = self.selected()
        if not slugs:
            self.app.bell()
            self.query_one("#queue-status", Static).update(
                "  nothing selected — space picks a row, ctrl+e takes the lot"
            )
            return
        # Hands off to exactly the path the setup screen's callback uses, so a
        # run started here is indistinguishable from any other run. Today's
        # queue row is left as generated: what you unchecked is a fact about
        # tonight, not a correction to the schedule.
        self.app.start_run(slugs)  # type: ignore[attr-defined]

    def action_back(self) -> None:
        self.dismiss(None)
