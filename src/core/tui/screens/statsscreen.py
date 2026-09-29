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

from ... import catalog, problemtypes, stats
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
        # Which kind of problem. Shown only once there is more than one: see
        # `check_action`. `y` because `t` opens this screen from home and a key
        # that is both the way in and a thing to press once inside is a key you
        # press twice by accident.
        Binding("y", "cycle_type", "type"),
    ]

    #: One scrolling pane, so motions work without anything being focused.
    VIM_TARGET = "#stats-body"

    def __init__(self) -> None:
        super().__init__()
        self.dimension_index = 0
        self.window_index = 1
        #: Every type there is a problem of, the active list's first, and which
        #: of them is on screen. A distribution is only ever over one: a
        #: forty-minute design and a twelve-minute LeetCode problem are both
        #: `medium`, and a percentile across the two is a number about neither.
        self.types: tuple[str, ...] = (problemtypes.DEFAULT_TYPE,)
        self.type_index = 0

    def compose(self) -> ComposeResult:
        yield Static(id="stats-title", classes="section-title")
        with VerticalScroll(id="stats-body"):
            yield Static(id="stats-content")
        yield Footer()

    def on_mount(self) -> None:
        conn = self.app.conn  # type: ignore[attr-defined]
        cfg = self.app.config  # type: ignore[attr-defined]
        first = catalog.type_of(conn, cfg.session.active_list)
        seeded = [
            r["type"]
            for r in conn.execute("SELECT DISTINCT type FROM problems ORDER BY type")
        ]
        self.types = tuple(dict.fromkeys((first, *seeded)))
        self.refresh_bindings()
        self.refresh_stats()

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        if action == "cycle_type":
            return len(self.types) > 1
        return True

    @property
    def type_name(self) -> str:
        return self.types[self.type_index]

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
        # Named only when there is a choice. With one type the title is the one
        # it has always been.
        which = (
            f"{problemtypes.load(self.type_name).title}  ·  "
            if len(self.types) > 1
            else ""
        )
        self.query_one("#stats-title", Static).update(
            f"  solve time distributions  ·  {which}by {self.dimension}  ·  {window_label}"
        )

        overall = stats.distribution(
            conn,
            label="ALL PROBLEMS",
            days=self.window,
            min_samples=cfg.stats.min_samples,
            weights=weights,
            type=self.type_name,
        )
        if overall.n == 0:
            self.query_one("#stats-content", Static).update(
                empty_state(
                    "No finished attempts in this window.",
                    "Percentiles need attempts. Press w to widen the window."
                    + (" y is the next type." if len(self.types) > 1 else ""),
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
            type=self.type_name,
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

    def action_cycle_type(self) -> None:
        self.type_index = (self.type_index + 1) % len(self.types)
        self.refresh_stats()

    def action_cycle_window(self) -> None:
        self.window_index = (self.window_index + 1) % len(WINDOWS)
        self.refresh_stats()

    def action_back(self) -> None:
        self.app.pop_screen()
