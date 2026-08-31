"""The percentile screen (spec §6).

Solve times as distributions, not averages. Your median sliding-window solve
being 18 minutes matters far less than your p99 being 55: the interview is a
single sample and the tail is what kills you.
"""

from __future__ import annotations

from rich.console import Group
from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.screen import Screen
from textual.widgets import Footer, Static

from ... import stats
from ...render import distribution_panel, empty_state, strategy_coverage_table
from ..vim import MOTIONS, VimMotion

# `strategy` last: it is empty until you have named a few, and a slice-by that
# opens on nothing would read as a broken screen rather than an unused one.
DIMENSIONS = ("pattern", "difficulty", "tag", "strategy")
WINDOWS = (30, 60, 90, None)


class StatsScreen(VimMotion, Screen[None]):
    BINDINGS = [
        *MOTIONS,
        Binding("escape", "back", "back"),
        Binding("q", "back", "back", show=False),
        Binding("d", "cycle_dimension", "slice by"),
        Binding("w", "cycle_window", "window"),
    ]

    #: One scrolling pane, so motions work without anything being focused.
    VIM_TARGET = "#stats-body"

    def __init__(self) -> None:
        super().__init__()
        self.dimension_index = 0
        self.window_index = 1

    def compose(self) -> ComposeResult:
        yield Static(id="stats-title", classes="section-title")
        with VerticalScroll(id="stats-body"):
            yield Static(id="stats-content")
        yield Footer()

    def on_mount(self) -> None:
        self.refresh_stats()

    @property
    def dimension(self) -> str:
        return DIMENSIONS[self.dimension_index]

    @property
    def window(self) -> int | None:
        return WINDOWS[self.window_index]

    def refresh_stats(self) -> None:
        conn = self.app.conn  # type: ignore[attr-defined]
        cfg = self.app.config  # type: ignore[attr-defined]
        weights = self.app.weights  # type: ignore[attr-defined]

        window_label = f"last {self.window}d" if self.window else "all time"
        self.query_one("#stats-title", Static).update(
            f"  solve time distributions  ·  by {self.dimension}  ·  {window_label}"
        )

        overall = stats.distribution(
            conn,
            label="ALL PROBLEMS",
            days=self.window,
            min_samples=cfg.stats.min_samples,
            weights=weights,
        )
        if overall.n == 0:
            self.query_one("#stats-content", Static).update(
                empty_state(
                    "No finished attempts in this window.",
                    "Percentiles need attempts. Press w to widen the window.",
                )
            )
            return

        blocks = [distribution_panel(overall), Text("")]
        slices = stats.distributions_by(
            conn,
            self.dimension,
            days=self.window,
            min_samples=cfg.stats.min_samples,
            weights=weights,
            limit=12,
        )
        for dist in slices:
            if dist.n == 0:
                continue
            blocks.append(distribution_panel(dist))
            blocks.append(Text(""))

        # Only under `by strategy`, where it is the same subject seen from the
        # other side: the distributions say how long each pattern takes you, and
        # this says how many problems you have reached for it on -- with the
        # methods list underneath, which is where "recorded but never written"
        # lives now.
        if self.dimension == "strategy":
            blocks.append(
                strategy_coverage_table(
                    stats.strategy_coverage(conn), stats.method_coverage(conn)
                )
            )
            blocks.append(Text(""))

        blocks.append(
            Text(
                "  p99 is greyed out below "
                f"{cfg.stats.min_samples} samples — at that size it is one attempt, not a statistic.",
                style="bright_black italic",
            )
        )
        self.query_one("#stats-content", Static).update(Group(*blocks))

    def action_cycle_dimension(self) -> None:
        self.dimension_index = (self.dimension_index + 1) % len(DIMENSIONS)
        self.refresh_stats()

    def action_cycle_window(self) -> None:
        self.window_index = (self.window_index + 1) % len(WINDOWS)
        self.refresh_stats()

    def action_back(self) -> None:
        self.app.pop_screen()
