"""Mastered problems: what has left the rotation, and what it took.

The scheduler's one invisible act. A problem that has been recalled across its
whole ladder stops being offered — no event, no screen, it just never comes up
again. That is the right behaviour and the wrong amount of feedback: a queue
that quietly narrows for reasons you cannot inspect is a queue you start second
guessing. So the masteries get a page of their own.

Read-only on purpose. Un-mastering by hand would be a fact about how you felt
today overwriting a fact the log already records; the way back in is to meet one
in a mixed run and lose it, which puts it on the failed ladder the same as any
other lapse.
"""

from __future__ import annotations

from datetime import datetime, timezone

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.screen import Screen
from textual.widgets import Footer, Static

from ... import catalog, srs
from ...render import mastered_table
from ..vim import MOTIONS, VimMotion


class MasteredScreen(VimMotion, Screen[None]):
    BINDINGS = [
        *MOTIONS,
        Binding("escape", "back", "back"),
        Binding("q", "back", "back", show=False),
    ]

    #: One scrolling pane, so motions work without anything being focused —
    #: same shape as the stats screen, and for the same reason.
    VIM_TARGET = "#mastered-body"

    def compose(self) -> ComposeResult:
        yield Static(id="mastered-title", classes="section-title")
        with VerticalScroll(id="mastered-body"):
            yield Static(id="mastered-content")
        yield Footer()

    def on_mount(self) -> None:
        self.refresh_mastered()

    def on_screen_resume(self) -> None:
        # A run can master something while this screen sits on the stack behind
        # it, so the list is rebuilt on the way back rather than only on mount.
        self.refresh_mastered()

    def refresh_mastered(self) -> None:
        conn = self.app.conn  # type: ignore[attr-defined]
        cfg = self.app.config  # type: ignore[attr-defined]
        rows = srs.mastered_cards(conn)
        # The active list's size, not the whole catalog's: "12 of 150" has to
        # count the list you are actually working through, and the two diverge
        # the moment a second list is seeded.
        size = len(catalog.all_problems(conn, cfg.session.active_list))

        self.query_one("#mastered-title", Static).update("  mastered problems")
        self.query_one("#mastered-content", Static).update(
            mastered_table(rows, size, datetime.now(timezone.utc))
        )

    def action_back(self) -> None:
        self.app.pop_screen()
