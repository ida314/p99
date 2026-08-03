"""Warming the offline cache from inside the app — `f` on the home menu.

The sweep itself is `cache.sync_problems`, unchanged and shared with
`<command> fetch`; this screen is the keyboard in front of it. Three things
make it different from every other screen here:

  * **It is the only screen that touches the network.** So it never starts on
    its own: it opens showing what it *would* fetch and waits for a key. A
    screen that began downloading 150 problems because you pressed `f` on the
    way to something else would be a screen you learn not to visit.
  * **The work is blocking and minutes long**, so it runs in a thread worker.
    The database is read first, on this thread, because the connection belongs
    to it -- `cache.sync` splits exactly there for this reason.
  * **Escape has to mean escape.** A thread cannot be killed, so cancelling
    sets a flag the sweep polls between problems: up to one request of lag, and
    then out. Nothing already on disk is lost by stopping -- the whole thing is
    resumable, and pressing `f` again picks up where it left off.

Home is the right place for it precisely because you cannot get here mid-run.
This is packing, not solving: it belongs to the moment before the plane, next
to the offline switch it exists to serve.
"""

from __future__ import annotations

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.screen import Screen
from textual.widgets import Footer, Static

from ... import cache
from ...render import bar, rule
from ..vim import MOTIONS, VimMotion

#: Failures scroll, but not forever: the tail is the part you can act on.
MAX_FAILURE_LINES = 12


class FetchScreen(VimMotion, Screen[None]):
    BINDINGS = [
        *MOTIONS,
        Binding("enter", "start", "fetch"),
        Binding("f", "start", "fetch", show=False),
        Binding("escape", "close", "back"),
        Binding("q", "close", "back", show=False),
    ]

    VIM_TARGET = "#fetch-body"

    def __init__(self) -> None:
        super().__init__()
        # `_sweeping`, not `_running`: `MessagePump._running` is Textual's own
        # and is True from the moment this screen starts, so a flag by that name
        # is born set and every key that checks it does nothing.
        self._sweeping = False
        self._stop_requested = False
        self._done = 0
        self._total = 0
        self._failures: list[str] = []

    def compose(self) -> ComposeResult:
        yield Static("  offline cache", classes="section-title")
        with VerticalScroll(id="fetch-body"):
            yield Static(id="fetch-plan", classes="panel")
            yield Static(id="fetch-progress", classes="panel")
            yield Static(id="fetch-failures", classes="panel")
        yield Static(id="fetch-hint", classes="hint-bar")
        yield Footer()

    def on_mount(self) -> None:
        self._render_plan()
        self._render_hint()
        self.query_one("#fetch-body", VerticalScroll).focus()

    # --- config -----------------------------------------------------------

    @property
    def _config(self):  # type: ignore[no-untyped-def]
        return self.app.config  # type: ignore[attr-defined]

    @property
    def _lists(self) -> tuple[str, ...]:
        return self._config.active_lists

    # --- rendering --------------------------------------------------------

    def _render_plan(self) -> None:
        """What is already here, and what pressing enter would cost.

        Read off the files on disk rather than the manifest, same as `doctor`:
        the pages are the truth and the manifest is bookkeeping.
        """
        status = cache.status(
            self.app.conn,  # type: ignore[attr-defined]
            lists=self._lists,
            budget_bytes=self._config.cache.budget_bytes,
        )
        missing = max(0, status.total - status.cached - len(status.failures))

        lines = [rule()]
        lines.append(
            self._row(
                "lists",
                ", ".join(self._lists) or "—",
                f"{status.total} problems",
            )
        )
        lines.append(
            self._row(
                "cached",
                f"{status.cached} / {status.total}",
                "nothing to do" if not missing else f"{missing} to fetch",
            )
        )
        lines.append(
            self._row(
                "on disk",
                cache.fmt_bytes(status.bytes),
                f"budget {cache.fmt_bytes(status.budget_bytes)}",
            )
        )
        lines.append(
            self._row(
                "last fetched",
                status.fetched_at[:10] if status.fetched_at else "never",
                "offline mode is ON" if self._config.cache.offline else "offline mode is off",
            )
        )
        lines.append(rule())
        self.query_one("#fetch-plan", Static).update(Text("\n").join(lines))

        # Premium slugs fail every sweep and can never be opened offline either;
        # showing them here as standing state, not as fresh news, is the
        # difference between a warning and noise.
        if status.failures and not self._sweeping:
            self._failures = [
                f"{slug} — {reason}" for slug, reason in sorted(status.failures.items())
            ]
            self._render_failures(title="last sweep couldn't fetch")

    @staticmethod
    def _row(label: str, value: str, note: str = "") -> Text:
        line = Text("  ")
        line.append(f"{label:<14}", style="bright_black")
        line.append(f"{value:<18}", style="bold")
        if note:
            line.append(note, style="bright_black")
        return line

    def _render_progress(self, message: Text | str = "") -> None:
        self.query_one("#fetch-progress", Static).update(message)

    def _render_failures(self, *, title: str) -> None:
        panel = self.query_one("#fetch-failures", Static)
        if not self._failures:
            panel.update("")
            return
        shown = self._failures[-MAX_FAILURE_LINES:]
        body = Text()
        body.append(f"  {title}", style="yellow")
        if len(self._failures) > len(shown):
            body.append(f"  (last {len(shown)} of {len(self._failures)})", style="bright_black")
        for line in shown:
            body.append(f"\n    {line}", style="bright_black")
        panel.update(body)

    def _render_hint(self) -> None:
        hint = (
            "  escape stops it — nothing already cached is lost"
            if self._sweeping
            else "  enter fetch    escape back"
        )
        self.query_one("#fetch-hint", Static).update(hint)

    # --- the sweep --------------------------------------------------------

    def action_start(self) -> None:
        if self._sweeping:
            return
        # The one database read, on the thread that owns the connection.
        order = cache.priority(self.app.conn, self._lists)  # type: ignore[attr-defined]
        if not order:
            self._render_progress(Text("  nothing in the active list to fetch", style="yellow"))
            return

        self._sweeping = True
        self._stop_requested = False
        self._done = 0
        self._total = len(order)
        self._failures = []
        self._render_failures(title="couldn't fetch")
        self._render_progress(Text("  starting…", style="bright_black"))
        self._render_hint()
        self.run_worker(lambda: self._sweep(order), thread=True, exclusive=True)

    def _sweep(self, order) -> None:  # type: ignore[no-untyped-def]
        """Worker thread. Nothing here touches the database or a widget."""

        def progress(slug: str, state: str) -> None:
            self.app.call_from_thread(self._on_progress, slug, state)

        report = cache.sync_problems(
            order,
            lists=self._lists,
            budget_bytes=self._config.cache.budget_bytes,
            language=self._config.capture.language,
            on_progress=progress,
            stop=lambda: self._stop_requested,
        )
        self.app.call_from_thread(self._on_done, report)

    def _on_progress(self, slug: str, state: str) -> None:
        self._done += 1
        line = Text("  ")
        line.append(f"{self._done:>3} / {self._total}  ")
        line.append(bar(self._done / self._total if self._total else 0, width=24), style="cyan")
        line.append(f"  {slug}", style="bold")
        line.append(f" — {state}", style="yellow" if state.startswith("failed") else "bright_black")
        self._render_progress(line)
        if state.startswith("failed"):
            self._failures.append(f"{slug} — {state.removeprefix('failed — ')}")
            self._render_failures(title="couldn't fetch")

    def _on_done(self, report: cache.SyncReport) -> None:
        self._sweeping = False
        bits = [f"{len(report.fetched)} fetched"]
        if report.kept:
            bits.append(f"{len(report.kept)} already cached")
        if report.upgraded:
            bits.append(f"{len(report.upgraded)} re-rendered")
        if report.pruned:
            bits.append(f"{len(report.pruned)} pruned")
        if report.skipped:
            bits.append(f"{len(report.skipped)} over budget")
        if report.failures:
            bits.append(f"{len(report.failures)} failed")

        summary = Text("  ")
        if report.cancelled:
            summary.append("stopped", style="bold yellow")
            summary.append(" — ", style="bright_black")
        summary.append(", ".join(bits), style="bold")
        summary.append(
            f" — {cache.fmt_bytes(report.total_bytes)}"
            f" / {cache.fmt_bytes(report.budget_bytes)}",
            style="bright_black",
        )
        if report.cancelled:
            summary.append("\n  what landed is kept; press enter to pick up the rest", "bright_black")
        elif report.over_budget:
            summary.append(
                f"\n  budget spent — raise [cache] max_mb for the remaining {len(report.skipped)}",
                style="yellow",
            )
        self._render_progress(summary)
        self._render_plan()
        self._render_hint()

    # --- leaving ----------------------------------------------------------

    def action_close(self) -> None:
        """Escape: stop a running sweep, or leave if there is nothing to stop.

        Two presses to leave mid-sweep, deliberately. The first one is the only
        way to stop a thread that cannot be killed, and pretending it also
        closed the screen would leave the download running behind a screen you
        can no longer see it on.
        """
        if self._sweeping:
            if not self._stop_requested:
                self._stop_requested = True
                self._render_progress(Text("  stopping after this problem…", style="yellow"))
            return
        self.dismiss()
