"""Home screen: where you are, and the one key that starts a run."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import Screen
from textual.widgets import Footer, OptionList, Static
from textual.widgets.option_list import Option

from ... import stats
from ...render import BANNER, rule
from ...scoring import fmt_duration
from ..vim import MOTIONS, VimMotion

from rich.console import Group
from rich.text import Text

#: (key, action, label). The key is the mnemonic shortcut; the menu is also a
#: real list, so `j`/`k` and enter get you there without knowing any of them.
MENU = [
    ("n", "new_run", "new run"),
    ("r", "history", "runs"),
    ("t", "stats", "stats"),
    ("s", "settings", "settings"),
    ("q", "quit", "quit"),
]


class HomeScreen(VimMotion, Screen):
    # History is on `r` (runs), not `h`: `h` is a motion everywhere else and a
    # keymap that means "left" on four screens and "open history" on the fifth
    # is worse than one letter of mnemonic.
    BINDINGS = [
        *MOTIONS,
        Binding("n", "new_run", "new run"),
        Binding("r", "history", "runs"),
        Binding("t", "stats", "stats"),
        Binding("s", "settings", "settings"),
        Binding("q", "quit", "quit"),
        Binding("l", "select", "open", show=False),
    ]

    VIM_TARGET = "#menu"

    def compose(self) -> ComposeResult:
        yield Static(BANNER, id="banner")
        yield Static(
            "timed, scored, permanently-recorded runs — you vs. your past self",
            classes="tagline",
        )
        yield Vertical(Static(id="overview", classes="panel"))
        yield OptionList(id="menu")
        yield Footer()

    def on_screen_resume(self) -> None:
        self.refresh_overview()

    def on_mount(self) -> None:
        menu = self.query_one("#menu", OptionList)
        for key, action, label in MENU:
            entry = Text("  ")
            entry.append(f" {key} ", style="reverse")
            entry.append(f"  {label}", style="bright_black")
            menu.add_option(Option(entry, id=action))
        menu.highlighted = 0
        menu.focus()
        self.refresh_overview()

    def refresh_overview(self) -> None:
        conn = self.app.conn  # type: ignore[attr-defined]
        ov = stats.overview(conn, self.app.weights)  # type: ignore[attr-defined]

        def row(label: str, value: str, note: str = "") -> Text:
            t = Text("  ")
            t.append(f"{label:<18}", style="bright_black")
            t.append(f"{value:<16}", style="bold")
            if note:
                t.append(note, style="bright_black")
            return t

        clean_pct = f"{ov.clean_solves / ov.total_attempts * 100:.0f}%" if ov.total_attempts else "—"
        coverage = f"{ov.distinct_slugs} / {ov.catalog_size} problems seen"

        rows = [
            rule(),
            row("runs logged", str(ov.total_runs), stats.gate_note(ov.total_runs)),
            row("attempts", str(ov.total_attempts), coverage),
            row("clean solves", f"{ov.clean_solves}", f"{clean_pct} of attempts"),
            row("time on problems", fmt_duration(ov.total_active_seconds)),
            row("best run", str(ov.best_score) if ov.total_runs else "—"),
            rule(),
        ]
        self.query_one("#overview", Static).update(Group(*rows))

    # --- menu -------------------------------------------------------------

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        # Option ids are action names, so enter and the mnemonic key run the
        # same code and cannot drift apart.
        if event.option.id:
            getattr(self, f"action_{event.option.id}")()

    def action_select(self) -> None:
        """`l` — same as enter, for hands that never leave the home row."""
        self.query_one("#menu", OptionList).action_select()

    def action_quit(self) -> None:
        # Defined here rather than left to `App.action_quit` so every menu entry
        # resolves to a screen method and the dispatch above stays uniform.
        self.app.exit()

    def action_new_run(self) -> None:
        self.app.action_new_run()  # type: ignore[attr-defined]

    def action_history(self) -> None:
        self.app.action_history()  # type: ignore[attr-defined]

    def action_stats(self) -> None:
        self.app.action_stats()  # type: ignore[attr-defined]

    def action_settings(self) -> None:
        self.app.action_settings()  # type: ignore[attr-defined]
