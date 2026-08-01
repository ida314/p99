"""The morning queue (spec §15 Phase 2, item 9).

What to do today and why, chosen by `core.queues` rather than by a roll. The
`n` / setup path is untouched and still there: this screen is the scheduler's
opinion, not a replacement for picking problems yourself.

The queue is generated on first open rather than by a nightly job — Phase 3
adds the cron. Either way it is never empty and never a blank screen asking you
to press a key first (spec §10): that rule is the difference between a coach you
trust and one you stop opening.
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.screen import Screen
from textual.widgets import Footer, Static

from ... import queues
from ...render import queue_panel
from ..vim import MOTIONS, VimMotion


class QueueScreen(VimMotion, Screen[None]):
    # `ctrl+r` regenerates and `ctrl+s` starts, matching the setup screen's roll
    # and start rather than inventing a second vocabulary for the same two
    # actions. No `h`/`l`: there is nothing here to move sideways through, and
    # the motion rule says a key that moves must be able to move back.
    BINDINGS = [
        *MOTIONS,
        Binding("escape", "back", "back"),
        Binding("q", "back", "back", show=False),
        Binding("enter", "start", "start run"),
        Binding("ctrl+s", "start", "start run", show=False),
        Binding("ctrl+r", "regenerate", "regenerate"),
    ]

    #: One scrolling pane, so motions work without anything being focused.
    VIM_TARGET = "#queue-body"

    def __init__(self) -> None:
        super().__init__()
        self.queue: queues.Queue | None = None

    def compose(self) -> ComposeResult:
        yield Static(id="queue-title", classes="section-title")
        with VerticalScroll(id="queue-body"):
            yield Static(id="queue-content")
        yield Static(
            "  enter start run    ctrl+r regenerate    q back",
            classes="hint-bar",
        )
        yield Footer()

    def on_mount(self) -> None:
        self.load(regenerate=False)

    def load(self, *, regenerate: bool) -> None:
        app = self.app  # type: ignore[attr-defined]
        self.queue = queues.ensure(
            app.conn,
            n=app.config.session.planned_n,
            active_list=app.config.session.active_list,
            weights=app.weights,
            regenerate=regenerate,
        )
        self.query_one("#queue-title", Static).update(f"  today's queue  ·  {self.queue.date}")
        self.query_one("#queue-content", Static).update(queue_panel(self.queue))

    # --- actions ----------------------------------------------------------

    def action_regenerate(self) -> None:
        self.load(regenerate=True)

    def action_start(self) -> None:
        if not self.queue or not self.queue.items:
            self.bell()
            return
        # Hands off to exactly the path the setup screen's callback uses, so a
        # run started here is indistinguishable from any other run.
        self.app.start_run(self.queue.slugs)  # type: ignore[attr-defined]

    def action_back(self) -> None:
        self.dismiss(None)
