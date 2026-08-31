"""Home screen: where you are, and the one key that starts a run."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import Screen
from textual.widgets import Footer, OptionList, Static
from textual.widgets.option_list import Option

from datetime import datetime, timezone

from ... import srs, stats
from ...render import BANNER, rule
from ...scoring import fmt_duration
from ..vim import MOTIONS, VimMotion

from rich.console import Group
from rich.text import Text

#: (key, action, label). The key is the mnemonic shortcut; the menu is also a
#: real list, so `j`/`k` and enter get you there without knowing any of them.
#:
#: `resume run` is not here: it only exists when there is a run to resume, and
#: `build_menu` gives it a row of its own above the columns when there is. That
#: row is full width because its label is not a label — it is a sentence, and
#: `resume run · problem 1 of 3 · Two Sum, 00:00 in · just now` wraps to three
#: lines in half a screen and takes the layout with it. It is also the entry you
#: most want to see, so the width is not wasted on it.
#:
#: The rest is drawn in two columns. Seven entries down the left of an
#: 80-column terminal is a lot of vertical scan for a menu whose whole job is to
#: disappear, and the banner above it already owns the height. Split
#: column-major, so `j` still walks the order written here and `l` steps across.
MENU = [
    ("n", "new_run", "new run"),
    ("q", "queue", "queue"),
    ("r", "history", "runs"),
    ("t", "stats", "stats"),
    # Beside stats because it is the same kind of thing — a record to look at,
    # not an action. `m` for mastered: `r` is already runs and `t` is stats.
    ("m", "mastered", "mastered"),
    # Beside mastered for the same reason mastered sits beside stats: it is a
    # record to read, not an action. `a` because `s` is settings and `t` is
    # already stats -- the letter is the shortcut, not an abbreviation of the
    # word.
    ("a", "methods", "methods"),
    # The offline cache is not here. It is packing, not practice, and it lives
    # one row under the `offline` switch on the settings screen — the switch is
    # useless without it, and turning offline mode on without warming the cache
    # first is the one way to reach a plane with nothing to open.
    ("s", "settings", "settings"),
    ("ctrl+c", "quit", "quit"),
]


class HomeScreen(VimMotion, Screen):
    # History is on `r` (runs), not `h`: `h` is a motion everywhere else and a
    # keymap that means "left" on four screens and "open history" on the fifth
    # is worse than one letter of mnemonic.
    BINDINGS = [
        *MOTIONS,
        Binding("n", "new_run", "new run"),
        # `c` for continue. Bound whether or not there is anything to resume, so
        # the key does not appear and vanish under your fingers; it bells when
        # there isn't.
        Binding("c", "resume_run", "resume run"),
        # `q` for queue. It used to be `d` (due) because `q` meant quit, and
        # that had it backwards: the queue is the screen you open every day
        # without thinking about it, and quitting is the thing you do once. So
        # the queue takes the letter of its own name and quit moves to `ctrl+c`,
        # which is how you leave a terminal program anyway.
        Binding("q", "queue", "queue"),
        Binding("r", "history", "runs"),
        Binding("t", "stats", "stats"),
        Binding("m", "mastered", "mastered"),
        Binding("a", "methods", "methods"),
        Binding("s", "settings", "settings"),
        Binding("ctrl+c", "quit", "quit"),
        # `l` used to be a second `enter`, which quietly broke the one rule
        # `vim.py` sets for these two keys: they may only ever mean left and
        # right, and whatever `l` does `h` has to undo. With two columns they
        # finally do mean that.
        Binding("l", "focus_next_column", "right", show=False),
        Binding("right", "focus_next_column", "right", show=False),
        Binding("h", "focus_prev_column", "left", show=False),
        Binding("left", "focus_prev_column", "left", show=False),
    ]

    #: The columns, left to right. Focus decides which one motions move —
    #: `VimMotion.vim_target` returns the focused widget when it is navigable —
    #: so `l`/`h` only have to move focus and `j`/`k` follow it for free.
    COLUMNS = ("#menu", "#menu-right")

    #: The full-width row above them, empty unless a run is suspended.
    RESUME = "#menu-resume"

    VIM_TARGET = "#menu"

    #: Which column `j` off the resume row drops into — the one `k` came up
    #: from, or the left one on a first move.
    _resume_target = COLUMNS[0]

    def compose(self) -> ComposeResult:
        yield Static(BANNER, id="banner")
        yield Static(
            "timed, scored, permanently-recorded runs — you vs. your past self",
            classes="tagline",
        )
        yield Vertical(Static(id="overview", classes="panel"))
        yield Vertical(
            OptionList(id="menu-resume"),
            Horizontal(
                OptionList(id="menu"),
                OptionList(id="menu-right"),
                id="menu-columns",
            ),
            id="menu-block",
        )
        yield Footer()

    def on_screen_resume(self) -> None:
        # Rebuilt, not just refreshed: suspending a run lands here, and the way
        # back into it has to be on screen the moment you arrive.
        self.build_menu()
        self.refresh_overview()

    def on_mount(self) -> None:
        self.build_menu()  # focuses whichever list it parked the cursor on
        self.refresh_overview()

    def build_menu(self) -> None:
        """Draw the menu, with the suspended run on top of it if there is one."""
        suspended = self.app.suspended_run()  # type: ignore[attr-defined]
        entries = list(MENU)
        resume = (
            [("c", "resume_run", f"resume run  ·  {suspended.summary}")]
            if suspended is not None
            else []
        )

        # Column-major, left column first and one longer when the count is odd.
        # `j` therefore walks `MENU` in the order it is written, which is what
        # `test_the_home_menu_is_navigable` indexes off and what a reader of
        # that list expects; row-major would interleave the two.
        split = (len(entries) + 1) // 2
        columns = (entries[:split], entries[split:])
        for selector, column in zip((self.RESUME, *self.COLUMNS), (resume, *columns)):
            listing = self.query_one(selector, OptionList)
            listing.clear_options()
            for key, action, label in column:
                entry = Text("  ")
                entry.append(f" {key} ", style="reverse")
                entry.append(f"  {label}", style="" if action == "resume_run" else "bright_black")
                listing.add_option(Option(entry, id=action))
            listing.display = bool(column)

        # Exactly one cursor is parked, and it is on the topmost row there is.
        # An `OptionList` leaves `highlighted` unset until something moves it,
        # and leaving the others unset is what keeps one visible cursor on
        # screen — three lists all showing row 0 reads as three cursors.
        first = self.RESUME if resume else self.COLUMNS[0]
        for selector in (self.RESUME, *self.COLUMNS):
            self.query_one(selector, OptionList).highlighted = 0 if selector == first else None
        # Focus goes with it. The two have to agree or the first `j` moves a
        # list that is not showing the cursor, and both lists then show one.
        self.query_one(first, OptionList).focus()

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

        cards, due, mastered = srs.counts(conn, datetime.now(timezone.utc))
        scheduled = f"{due} due now" if cards else "nothing scheduled yet"
        if mastered:
            scheduled += f"  ·  {mastered} mastered"

        rows = [
            rule(),
            row("runs logged", str(ov.total_runs)),
            row("attempts", str(ov.total_attempts), coverage),
            row("scheduled", f"{cards} cards", scheduled),
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

    # --- moving between the three lists -------------------------------------
    #
    # The menu looks like one list and has to behave like one. It is three
    # `OptionList`s, so every move that crosses a boundary is a focus change
    # plus a cursor handoff, and the two rules below are what keep exactly one
    # cursor visible: whoever gives up focus sets `highlighted = None`, and
    # whoever takes it sets a row.

    def _list(self, selector: str) -> OptionList:
        return self.query_one(selector, OptionList)

    def _focused_list(self) -> str:
        """Which of the three has focus. The left column when none of them does."""
        for selector in (self.RESUME, *self.COLUMNS):
            if self.focused is self._list(selector):
                return selector
        return self.COLUMNS[0]

    def _hand_over(self, source: str, target: str, row: int) -> None:
        listing = self._list(target)
        if not listing.option_count:
            return
        self._list(source).highlighted = None
        listing.highlighted = max(0, min(row, listing.option_count - 1))
        listing.focus()

    def _move_column(self, delta: int) -> None:
        """Step focus one column over, carrying the cursor with it.

        Clamped rather than wrapping: `h` has to undo `l`, and a wrap makes the
        pair a cycle instead of an axis. From the resume row — which spans both
        columns — sideways means nothing, so it does nothing.
        """
        source = self._focused_list()
        if source == self.RESUME:
            return
        index = self.COLUMNS.index(source)
        target = self.COLUMNS[max(0, min(len(self.COLUMNS) - 1, index + delta))]
        if target == source:
            return
        # Land on the same row where there is one, so the cursor tracks across
        # rather than jumping to the top of the next column.
        self._hand_over(source, target, self._list(source).highlighted or 0)

    def action_focus_next_column(self) -> None:
        self._move_column(1)

    def action_focus_prev_column(self) -> None:
        self._move_column(-1)

    # `j` off the bottom of the resume row, and `k` off the top of a column,
    # cross into the neighbouring list instead of stopping dead. Overridden here
    # rather than in `VimMotion`, which moves within one focused widget on
    # purpose — this screen is the only one that draws a single list as three.

    def action_vim_down(self) -> None:
        source = self._focused_list()
        if source == self.RESUME:
            # One row, so being on it is being at the bottom of it.
            self._hand_over(source, self._resume_target, 0)
            return
        super().action_vim_down()

    def action_vim_up(self) -> None:
        source = self._focused_list()
        if source in self.COLUMNS and (self._list(source).highlighted or 0) == 0:
            if self._list(self.RESUME).option_count:
                # Remembered, so `k` then `j` puts you back where you started
                # rather than always dropping into the left column.
                self._resume_target = source
                self._hand_over(source, self.RESUME, 0)
                return
        super().action_vim_up()

    def action_quit(self) -> None:
        # Defined here rather than left to `App.action_quit` so every menu entry
        # resolves to a screen method and the dispatch above stays uniform.
        self.app.exit()

    def action_new_run(self) -> None:
        self.app.action_new_run()  # type: ignore[attr-defined]

    def action_resume_run(self) -> None:
        self.app.action_resume_run()  # type: ignore[attr-defined]

    def action_queue(self) -> None:
        self.app.action_queue()  # type: ignore[attr-defined]

    def action_history(self) -> None:
        self.app.action_history()  # type: ignore[attr-defined]

    def action_stats(self) -> None:
        self.app.action_stats()  # type: ignore[attr-defined]

    def action_mastered(self) -> None:
        self.app.action_mastered()  # type: ignore[attr-defined]

    def action_methods(self) -> None:
        self.app.action_methods()  # type: ignore[attr-defined]

    def action_settings(self) -> None:
        self.app.action_settings()  # type: ignore[attr-defined]
