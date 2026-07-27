"""The Textual app: owns the connection, the engine, and screen transitions."""

from __future__ import annotations

import sqlite3
from contextlib import AbstractContextManager

from textual.app import App

from .. import branding, catalog, config as config_module, db, paths, scoring
from ..engine import RunEngine
from .screens import (
    HistoryScreen,
    HomeScreen,
    SetupScreen,
    SolveScreen,
    StatsScreen,
    SummaryScreen,
)


class CoreApp(App):
    CSS_PATH = "app.tcss"
    TITLE = branding.NAME
    SUB_TITLE = branding.TAGLINE

    BINDINGS = []

    def __init__(self, conn: sqlite3.Connection | None = None):
        super().__init__()
        self.conn = conn or db.open_db()
        self.config = config_module.load()
        self.weights = scoring.load_weights(self.config.scoring.weights)
        self.engine = RunEngine(self.conn)
        # Set by the summary screen so a hard quit still seals the run with the
        # end-of-run note the user already wrote.
        self.pending_session_note: str | None = None

    def on_mount(self) -> None:
        paths.ensure_dirs()
        config_module.write_default_config()
        if catalog.count(self.conn) == 0:
            catalog.seed(self.conn, name=self.config.session.active_list)
        self.push_screen(HomeScreen())

    def on_unmount(self) -> None:
        # A run left open by a hard quit is still a run: seal it rather than
        # leaving a session row with no ended_at.
        try:
            if self.engine.session is not None:
                self.engine.end_session(session_note=self.pending_session_note)
        except Exception:
            pass

    def editor_context(self) -> AbstractContextManager[None]:
        """Hand the terminal to `$EDITOR` for the duration of the block.

        Isolated here because it is the one place the app gives up the screen,
        and because tests need to drive the capture flow without a real TTY.
        """
        return self.suspend()

    # --- navigation -------------------------------------------------------

    def action_new_run(self) -> None:
        if self.engine.session is not None:
            self.bell()
            return
        self.push_screen(
            SetupScreen(self.config.session.active_list, self.config.session.planned_n),
            self._start_run,
        )

    def _start_run(self, slugs: list[str] | None) -> None:
        if not slugs:
            return
        self.config = config_module.load()
        self.weights = scoring.load_weights(self.config.scoring.weights)
        self.engine.start_session(slugs, planned_n=len(slugs))
        self.push_screen(SolveScreen(self.engine))

    def show_summary(self) -> None:
        self.push_screen(SummaryScreen(self.engine))

    def finish_run(self) -> None:
        """Pop the summary and the solve screen, landing back on home."""
        while len(self.screen_stack) > 2:
            self.pop_screen()

    def action_history(self) -> None:
        self.push_screen(HistoryScreen())

    def action_stats(self) -> None:
        self.push_screen(StatsScreen())


def run(conn: sqlite3.Connection | None = None) -> None:
    CoreApp(conn).run()
