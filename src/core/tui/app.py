"""The Textual app: owns the connection, the engine, and screen transitions."""

from __future__ import annotations

import sqlite3
from contextlib import AbstractContextManager

from textual.app import App

from .. import branding, catalog, config as config_module, db, engine as engine_module, paths, scoring
from ..engine import RunEngine
from .screens import (
    FetchScreen,
    HistoryScreen,
    HomeScreen,
    QueueScreen,
    RunPlan,
    SettingsScreen,
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
        self.config = config_module.load(self.conn)
        self.weights = scoring.load_weights(self.config.scoring.weights)
        self.engine = RunEngine(self.conn)
        # Set by the summary screen so a hard quit still seals the run with the
        # end-of-run note the user already wrote.
        self.pending_session_note: str | None = None
        # Whether the run now in progress is recording. Resolved once, when the
        # run starts, so flipping the setting mid-run cannot start a microphone
        # under a problem that began without one.
        self.speech_mode: bool = False

    def on_mount(self) -> None:
        paths.ensure_dirs()
        config_module.write_default_config()
        if catalog.count(self.conn) == 0:
            catalog.seed(self.conn, name=self.config.session.active_list)
        self.push_screen(HomeScreen())

    def on_unmount(self) -> None:
        """Leave a run recoverable rather than sealing it behind you.

        A hard quit used to abandon whatever was on screen, which recorded a
        `gave_up` -- score 0 and an FSRS lapse -- for a problem you were in the
        middle of. Suspending instead costs nothing and offers the run back on
        the next launch. With no live attempt there is nothing to come back to,
        so the run is sealed the way it always was, note and all.
        """
        try:
            if self.engine.session is None:
                return
            if self.engine.attempt is not None and not self.engine.attempt.finished:
                self.engine.suspend_session()
            else:
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
            SetupScreen(
                self.config.session.active_list,
                self.config.session.planned_n,
                self.config.audio.speech_mode,
            ),
            self._start_planned_run,
        )

    def _start_planned_run(self, plan: RunPlan | None) -> None:
        if plan is None:
            return
        self.start_run(plan.slugs, speech_mode=plan.speech_mode)

    def action_queue(self) -> None:
        if self.engine.session is not None:
            self.bell()
            return
        self.push_screen(QueueScreen())

    def reload_config(self) -> None:
        """Re-read both config layers. Called after the settings screen writes.

        Every screen reads `app.config` rather than loading its own, so a knob
        changed mid-run takes effect on the next problem without a restart.
        """
        self.config = config_module.load(self.conn)
        self.weights = scoring.load_weights(self.config.scoring.weights)

    def start_run(self, slugs: list[str] | None, speech_mode: bool | None = None) -> None:
        """Begin a run over `slugs`. The one way in, from setup or the queue.

        `speech_mode` is the setup screen's per-run override; the queue passes
        nothing and gets the setting, which is what keeps the settings knob
        load-bearing rather than decorative.
        """
        if not slugs:
            return
        self.reload_config()
        self.speech_mode = (
            self.config.audio.speech_mode if speech_mode is None else speech_mode
        )
        self.engine.start_session(slugs, planned_n=len(slugs), speech_mode=self.speech_mode)
        self.push_screen(SolveScreen(self.engine))

    def suspended_run(self) -> engine_module.SuspendedRun | None:
        """The run waiting to be picked up, if any. None while one is live."""
        if self.engine.session is not None:
            return None
        return engine_module.suspended_run(self.conn)

    def action_resume_run(self) -> None:
        """Reopen the suspended run, on the problem it was left on."""
        run = self.suspended_run()
        if run is None:
            self.bell()
            return
        self.reload_config()
        # From the run, not from the setting: whether this run is recording was
        # decided when it started, and a knob flipped since is not a reason to
        # start a microphone halfway through a problem that began without one.
        self.speech_mode = run.speech_mode
        self.engine.resume_session(run.session_uuid)
        self.push_screen(SolveScreen(self.engine))

    def suspend_run(self) -> None:
        """Put the run down and land back on home, where `c` picks it up."""
        self.engine.suspend_session()
        self.finish_run()

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

    def action_settings(self) -> None:
        self.push_screen(SettingsScreen())

    def action_fetch(self) -> None:
        # Guarded like the queue rather than left open like stats: this one goes
        # to the network for minutes at a time, and a run is the one thing it
        # must never happen behind.
        if self.engine.session is not None:
            self.bell()
            return
        self.push_screen(FetchScreen())


def run(conn: sqlite3.Connection | None = None) -> None:
    CoreApp(conn).run()
