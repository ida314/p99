"""The run summary — a roguelike death screen (spec §5).

Styled as a death screen, not actually roguelike mechanics. The run is over by
the time you get here; the only thing left is the optional end-of-run note,
which is written before the session is sealed with a `session_ended` event.
"""

from __future__ import annotations

from datetime import datetime, timezone

from rich.console import Group
from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.screen import Screen
from textual.widgets import Footer, Static

from ... import capture, scoring, stats
from ...engine import RunEngine
from ...render import attempt_rows, death_screen, stat_line
from ..vim import MOTIONS, VimMotion


class SummaryScreen(VimMotion, Screen[None]):
    BINDINGS = [
        *MOTIONS,
        Binding("e", "write_note", "end-of-run note"),
        Binding("enter", "done", "done"),
        Binding("q", "done", "done", show=False),
        Binding("escape", "done", "done", show=False),
    ]

    VIM_TARGET = "#summary-body"

    def __init__(self, engine: RunEngine):
        super().__init__()
        self.engine = engine
        self.session_id = engine.session.id if engine.session else None
        self.session_note: str | None = None
        self.toast_note = ""

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="summary-body"):
            yield Static(id="death-screen", classes="panel")
            yield Static("  the problems", classes="section-title")
            yield Static(id="stat-lines", classes="panel")
            yield Static(id="summary-hint", classes="panel")
        yield Footer()

    def on_mount(self) -> None:
        self.refresh_summary()

    def refresh_summary(self) -> None:
        conn = self.app.conn  # type: ignore[attr-defined]
        weights = self.app.weights  # type: ignore[attr-defined]
        runs = stats.load_runs(conn, weights=weights)
        run = next((r for r in runs if r.session_id == self.session_id), None)

        if run is None:
            self.query_one("#death-screen", Static).update(
                Text("  Nothing was recorded in that run.", style="bright_black")
            )
            self.query_one("#stat-lines", Static).update("")
            return

        # The session isn't sealed until you leave this screen, so show the end
        # time as now rather than a blank.
        if run.ended_at is None:
            run.ended_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        run.session_note = self.session_note

        standing = stats.standing(runs, self.session_id)
        self.query_one("#death-screen", Static).update(death_screen(run, standing, runs))

        blocks = []
        for attempt in run.attempts:
            if not attempt.get("ended_at"):
                continue
            score = scoring.score_attempt(attempt, attempt["difficulty"], weights)
            blocks.append(stat_line(
                attempt["title"],
                attempt["difficulty"],
                score,
                attempt.get("self_confidence"),
                attempt_rows(attempt),
            ))
            blocks.append(Text(""))
        self.query_one("#stat-lines", Static).update(
            Group(*blocks) if blocks else Text("  nothing finished", style="bright_black")
        )

        note_state = "written" if self.session_note else "not written"
        if self.toast_note:
            note_state += f", {self.toast_note}"
        self.query_one("#summary-hint", Static).update(
            Text(
                f"  e  end-of-run note ({note_state}) — energy, focus, anything environmental"
                f"\n  enter  done",
                style="bright_black",
            )
        )

    def action_write_note(self) -> None:
        """Open the end-of-run buffer. Never destructive.

        A skipped or failed second edit keeps whatever you already wrote —
        losing a note you have already typed is strictly worse than not
        offering the second edit at all.
        """
        written = None
        try:
            with self.app.editor_context():
                written = capture.capture_session_note()
        except Exception:
            self.toast_note = "the editor handoff failed — nothing was lost"
        else:
            self.toast_note = "" if written else "note unchanged"
        if written:
            self.session_note = written
            # Survive a hard quit from this screen: on_unmount seals the
            # session and would otherwise drop the note on the floor.
            self.app.pending_session_note = written
        self.refresh_summary()

    def action_done(self) -> None:
        self.engine.end_session(session_note=self.session_note)
        self.app.pending_session_note = None  # type: ignore[attr-defined]
        self.app.finish_run()  # type: ignore[attr-defined]
