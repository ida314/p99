"""History: run rankings, and the stat lines behind any one of them.

You vs. your past self (spec §1). The data model is sync-ready for friends
later; nothing here assumes there's only one person.
"""

from __future__ import annotations

from rich.console import Group
from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.screen import Screen
from textual.widgets import Footer, OptionList, Static
from textual.widgets.option_list import Option

from ... import events, scoring, stats
from ...render import approach_rows, death_screen, empty_state, history_table, stat_line
from ...scoring import fmt_duration
from ..vim import MOTIONS, VimMotion
from .finish import ConfirmModal


class HistoryScreen(VimMotion, Screen[None]):
    # Two panes, so `h` and `l` do what they do in a vim split: move between
    # them. `j`/`k` then act on whichever one has focus — the run list or the
    # stat lines for the highlighted run.
    BINDINGS = [
        *MOTIONS,
        Binding("escape", "back", "back"),
        Binding("q", "back", "back", show=False),
        Binding("t", "toggle_view", "table / detail"),
        Binding("d", "delete_run", "delete run"),
        Binding("h", "focus_list", "runs", show=False),
        Binding("l", "focus_detail", "detail", show=False),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.runs: list[stats.Run] = []
        self.show_table = False

    def compose(self) -> ComposeResult:
        yield Static("  history", classes="section-title")
        with Horizontal(id="history-body"):
            yield OptionList(id="run-list")
            with VerticalScroll(id="detail-pane"):
                yield Static(id="run-detail")
        yield Footer()

    def on_mount(self) -> None:
        self.load()

    def load(self) -> None:
        conn = self.app.conn  # type: ignore[attr-defined]
        self.runs = stats.load_runs(conn, weights=self.app.weights)  # type: ignore[attr-defined]
        option_list = self.query_one("#run-list", OptionList)
        option_list.clear_options()
        option_list.border_title = f"{len(self.runs)} runs"

        if not self.runs:
            self.query_one("#run-detail", Static).update(
                empty_state(
                    "No runs logged yet.",
                    "Press n on the home screen to start one.",
                )
            )
            return

        for i, run in enumerate(reversed(self.runs), start=1):
            number = len(self.runs) - i + 1
            label = Text()
            label.append(f"#{number:<4}", style="bold")
            label.append(f"{run.local_date}  ", style="bright_black")
            label.append(f"{run.score:>4}", style="cyan")
            option_list.add_option(Option(label, id=str(run.session_id)))

        option_list.highlighted = 0
        option_list.focus()
        if self.show_table:
            self.query_one("#run-detail", Static).update(history_table(self.runs))
        else:
            self._show_run(self.runs[-1].session_id)

    def on_option_list_option_highlighted(self, event: OptionList.OptionHighlighted) -> None:
        if event.option.id and not self.show_table:
            self._show_run(int(event.option.id))

    def _show_run(self, session_id: int) -> None:
        weights = self.app.weights  # type: ignore[attr-defined]
        run = next((r for r in self.runs if r.session_id == session_id), None)
        if run is None:
            return
        standing = stats.standing(self.runs, session_id)
        blocks = [death_screen(run, standing, self.runs), Text("")]

        if run.suspended:
            # Said plainly rather than left as a blank end time: the score below
            # is a partial one, and a run you can still finish should not read
            # like a run that ended badly.
            blocks.append(
                Text(
                    f"  suspended {stats.fmt_ago(run.suspended_at)}"
                    " — c on the home screen picks it back up\n",
                    style="yellow",
                )
            )

        for attempt in run.attempts:
            if not attempt.get("ended_at"):
                continue
            score = scoring.score_attempt(attempt, attempt["difficulty"], weights)
            blocks.append(
                stat_line(
                    attempt["title"],
                    attempt["difficulty"],
                    score,
                    attempt.get("self_confidence"),
                    approach_rows(attempt),
                )
            )
            blocks.append(self._artifacts(attempt))
            blocks.append(Text(""))

        if run.session_note:
            blocks.append(Text("  end-of-run note", style="bright_black"))
            blocks.append(Text("  " + run.session_note.replace("\n", "\n  "), style="italic"))

        self.query_one("#run-detail", Static).update(Group(*blocks))

    @staticmethod
    def _artifacts(attempt: dict) -> Text:
        """Where this attempt's code, note and recording live on disk."""
        line = Text("  ")
        keys = (("code", "code_path"), ("note", "note_path"), ("audio", "audio_path"))
        for label, key in keys:
            path = attempt.get(key)
            # The recording only exists when speech mode was on, and a run of
            # em-dashes for a feature you don't use is noise on every line.
            if key == "audio_path" and not path:
                continue
            line.append(f"{label} ", style="bright_black")
            line.append(f"{path if path else '—'}   ", style="" if path else "bright_black")
        paused = int(attempt.get("paused_seconds") or 0)
        if paused:
            line.append(f"paused {fmt_duration(paused)}   ", style="bright_black")
        # Kept apart from the pause it is not: four minutes at the kettle and an
        # overnight break are different facts about how the solve went.
        away = int(attempt.get("suspended_seconds") or 0)
        suspends = int(attempt.get("suspends") or 0)
        if away or suspends:
            times = f" ({suspends}×)" if suspends > 1 else ""
            line.append(f"suspended {fmt_duration(away)}{times}", style="bright_black")
        return line

    def action_focus_list(self) -> None:
        self.query_one("#run-list", OptionList).focus()

    def action_focus_detail(self) -> None:
        self.query_one("#detail-pane", VerticalScroll).focus()

    def action_toggle_view(self) -> None:
        self.show_table = not self.show_table
        if self.show_table:
            self.query_one("#run-detail", Static).update(history_table(self.runs))
        else:
            option_list = self.query_one("#run-list", OptionList)
            highlighted = option_list.highlighted
            if highlighted is not None:
                option = option_list.get_option_at_index(highlighted)
                if option.id:
                    self._show_run(int(option.id))

    def action_delete_run(self) -> None:
        if self.runs:
            self.run_worker(self._do_delete(), exclusive=True)

    async def _do_delete(self) -> None:
        """Delete the highlighted run, then rebuild every projection from the log.

        The replay is not optional. `run_deleted` only empties the tables it can
        see; the review schedule this run's attempts wrote is spread across
        `fsrs_cards`, and the only way to get cards that never saw the run is to
        rebuild them from a log that skips it (`events.tombstoned`).
        """
        run = self._highlighted_run()
        if run is None:
            return
        number = stats.run_number(self.runs, run.session_id)
        ok = await self.app.push_screen_wait(
            ConfirmModal(
                f"Delete run #{number}?",
                f"{len(run.attempts)} attempt"
                f"{'s' if len(run.attempts) != 1 else ''} and their reviews are "
                "unwound. Archived code and notes stay on disk. Later runs "
                "renumber to close the gap.",
                yes_label="delete it",
                no_label="keep it",
            )
        )
        if not ok:
            return
        conn = self.app.conn  # type: ignore[attr-defined]
        events.append(conn, events.RUN_DELETED, {"session_uuid": run.session_uuid})
        events.replay(conn)
        self.load()
        self.notify(f"run #{number} deleted")

    def _highlighted_run(self) -> stats.Run | None:
        option_list = self.query_one("#run-list", OptionList)
        index = option_list.highlighted
        if index is None:
            return None
        option = option_list.get_option_at_index(index)
        if not option.id:
            return None
        session_id = int(option.id)
        return next((r for r in self.runs if r.session_id == session_id), None)

    def action_back(self) -> None:
        self.app.pop_screen()
