"""The methods screen: every way you know to solve every problem.

The same list the prompt after a solve edits, months later and out of a run. A
problem you have solved three ways has three rows here — the route, what it
costs, whether it is the optimal one, and your write-up of it if you have one.

Methods, not strategies. A pattern you reached for belongs to the vocabulary and
turns up on the stats screen, sliced against your solve times; a method is one
problem's own route and lives here. The two lists never reference each other —
see `methods`.

The point of coming here is the rows that are *not* filled in. "There is a
monotonic stack route through this, it is the optimal one, and I have never
written it" is a fact about your preparation that no attempt record can state,
and it is the row you would open this screen to close.

Two keys write:

  o  cycle what this method costs — optimal, not optimal, not sure, unclaimed
  e  open `$EDITOR` on the first method with no code, and archive what you write

Both go through the event log like everything else. `o` emits `method_updated`,
which matters more than it looks: an optimal method that is not the one you wrote
is what `srs.rate` reads as "you knew there was better", so marking one here
feeds the scheduler for every future attempt at that problem. `e` emits
`method_archived`, an event with no attempt on it — you did not sit a timed
problem, you sat down with a route you named months ago and finally wrote it. It
scores nothing for exactly that reason.

Deliberately not a schedule. Nothing here is due, and recording a second method
does not shorten an interval or add a second thing to review: the card is the
problem's and stays the problem's. This is a record you go to, not a queue that
comes to you.

The one thing this screen must never become is a way to read the answer while
solving. `solve._render_problem` withholds method names for the same reason it
withholds the archived code, and reaching this screen mid-run means walking out
of the run to do it.
"""

from __future__ import annotations

from rich.text import Text
from textual.app import ComposeResult, SuspendNotSupported
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.screen import Screen
from textual.widgets import Footer, OptionList, Static
from textual.widgets.option_list import Option

from ... import capture, catalog, events, methods
from ...render import method_row
from ..vim import MOTIONS, VimMotion
from .methods import OPTIMALITY_CYCLE


class MethodsScreen(VimMotion, Screen[None]):
    """Problems on the left, that problem's methods on the right.

    Same two-pane shape as the history screen, and `h`/`l` move between the
    panes there too — one layout for "a list, and the detail of whichever row
    you are on" rather than two that have to be learned separately.

    Unlike history, both panes take a cursor: the right one is editable, and `o`
    has to act on a row you can see yourself pointing at.
    """

    BINDINGS = [
        *MOTIONS,
        Binding("escape", "back", "back"),
        Binding("q", "back", "back", show=False),
        Binding("o", "cycle_optimality", "optimal / not"),
        Binding("e", "write_code", "write code"),
        Binding("h", "focus_problems", "problems", show=False),
        Binding("l", "focus_ways", "methods", show=False),
    ]

    VIM_TARGET = "#method-problems"

    def __init__(self) -> None:
        super().__init__()
        self.problems: list = []
        #: The methods of whichever problem the left cursor is on, in draw
        #: order, so `o` and `e` act without asking the database for a list they
        #: just rendered.
        self.ways: list[methods.Method] = []
        self._busy = False

    def compose(self) -> ComposeResult:
        yield Static("  methods", classes="section-title")
        with Horizontal(id="method-body"):
            yield OptionList(id="method-problems")
            with VerticalScroll(id="method-detail"):
                yield OptionList(id="method-ways")
        yield Footer()

    def on_mount(self) -> None:
        self.load()

    def on_screen_resume(self) -> None:
        # A run finishing behind this screen adds rows to it, and so does `e`,
        # which suspends the app rather than pushing a screen but lands back
        # here either way.
        self.load()

    # --- the problem list -------------------------------------------------

    def load(self) -> None:
        conn = self.app.conn  # type: ignore[attr-defined]
        widget = self.query_one("#method-problems", OptionList)
        highlighted = widget.highlighted
        self.problems = methods.problems_with_methods(conn)
        widget.clear_options()
        widget.border_title = f"{len(self.problems)} problems"
        if not self.problems:
            self.ways = []
            listing = self.query_one("#method-ways", OptionList)
            listing.clear_options()
            # In the list rather than mounted beside it: `load` runs again on
            # every resume, and a widget mounted here would stack up a copy of
            # itself each time.
            listing.add_option(
                Option(
                    Text(
                        "  nothing recorded yet — the prompt after your next "
                        "solve is where this fills up",
                        style="bright_black",
                    ),
                    id=None,
                    disabled=True,
                )
            )
            return
        for row in self.problems:
            widget.add_option(Option(self._problem_label(row), id=row["slug"]))
        # Put the cursor back where it was, as `OptionList` never does by
        # itself — a reload after `o` would otherwise send you to the top of the
        # list, away from the problem you were editing.
        widget.highlighted = (
            min(highlighted, len(self.problems) - 1) if highlighted is not None else 0
        )
        self._show(self.problems[widget.highlighted]["slug"])

    @staticmethod
    def _problem_label(row) -> Text:
        """One problem: its title, how many methods, and how many have code.

        `2/3` rather than a bare count, because the ratio is what you scan the
        list for — three methods with one written is a different state from
        three of three, and it is the one worth walking over to.
        """
        line = Text("  ")
        line.append(row["title"][:26])
        counts = f"{row['written'] or 0}/{row['ways']}"
        pad = max(1, 32 - len(row["title"][:26]) - len(counts))
        line.append(" " * pad)
        line.append(counts, style="bright_black")
        return line

    def on_option_list_option_highlighted(self, event: OptionList.OptionHighlighted) -> None:
        if event.option_list.id == "method-problems" and event.option.id:
            self._show(event.option.id)

    def _show(self, slug: str, focus_key: str | None = None) -> None:
        conn = self.app.conn  # type: ignore[attr-defined]
        self.ways = methods.for_problem(conn, slug)
        widget = self.query_one("#method-ways", OptionList)
        highlighted = widget.highlighted
        widget.clear_options()
        widget.border_title = slug
        if not self.ways:
            widget.add_option(
                Option(
                    Text("  no methods recorded for this problem yet", style="bright_black"),
                    id=None,
                    disabled=True,
                )
            )
            return
        for way in self.ways:
            widget.add_option(
                Option(
                    method_row(
                        name=way.name,
                        optimality=way.optimality,
                        complexity=way.complexity,
                        written=way.written,
                        attempt_id=way.attempt_id,
                    ),
                    id=way.key,
                )
            )
        keys = [w.key for w in self.ways]
        if focus_key in keys:
            widget.highlighted = keys.index(focus_key)
        elif highlighted is not None:
            widget.highlighted = min(highlighted, len(keys) - 1)
        else:
            widget.highlighted = 0

    def _highlighted_slug(self) -> str | None:
        widget = self.query_one("#method-problems", OptionList)
        if widget.highlighted is None or not widget.option_count:
            return None
        return widget.get_option_at_index(widget.highlighted).id

    def _highlighted_way(self) -> methods.Method | None:
        widget = self.query_one("#method-ways", OptionList)
        if widget.highlighted is None or not widget.option_count:
            return None
        key = widget.get_option_at_index(widget.highlighted).id
        return next((w for w in self.ways if w.key == key), None)

    # --- what a way costs -------------------------------------------------

    def action_cycle_optimality(self) -> None:
        """Cycle the highlighted method through the four states, as an event.

        An event rather than an UPDATE because it feeds a rating: an optimal
        method that is not the one you wrote is what `srs.rate` reads as "you
        knew there was better", and anything a rating depends on has to survive
        a replay.
        """
        slug = self._highlighted_slug()
        way = self._highlighted_way()
        if slug is None or way is None:
            return
        current = way.optimality
        index = OPTIMALITY_CYCLE.index(current) if current in OPTIMALITY_CYCLE else -1
        nxt = OPTIMALITY_CYCLE[(index + 1) % len(OPTIMALITY_CYCLE)]
        events.append(
            self.app.conn,  # type: ignore[attr-defined]
            events.METHOD_UPDATED,
            {"slug": slug, "methods": [{"name": way.name, "optimality": nxt}]},
        )
        self._show(slug, focus_key=way.key)

    # --- writing the code for one way -------------------------------------

    def action_write_code(self) -> None:
        self.run_worker(self._do_write_code(), exclusive=True)

    async def _do_write_code(self) -> None:
        """Open `$EDITOR` for the highlighted method, or the first with no code.

        Falling back to the first gap rather than refusing: the right pane's
        cursor is usually parked wherever the last `o` left it, and the thing you
        came here to do is close a gap.
        """
        if self._busy:
            return
        slug = self._highlighted_slug()
        if slug is None:
            return
        way = self._highlighted_way()
        if way is None or way.written:
            way = next((w for w in self.ways if not w.written), None)
        if way is None:
            self.notify("every method on this problem already has code")
            return

        conn = self.app.conn  # type: ignore[attr-defined]
        cfg = self.app.config  # type: ignore[attr-defined]
        problem = catalog.get(conn, slug)
        if problem is None:
            return

        self._busy = True
        try:
            if not capture.editor_available():
                self.notify("no $EDITOR found", severity="warning")
                return
            try:
                with self.app.editor_context():  # type: ignore[attr-defined]
                    result = capture.capture_method(
                        problem,
                        methods.Named(key=way.key, name=way.name),
                        cfg.capture.language,
                    )
            except SuspendNotSupported:
                self.notify("this terminal can't hand off to $EDITOR", severity="warning")
                return
            if not (result.saved and result.path):
                self.notify(f"{way.name} — skipped")
                return
            events.append(
                conn,
                events.METHOD_ARCHIVED,
                {
                    "slug": slug,
                    "method": way.name,
                    "code_path": str(result.path),
                    "language": cfg.capture.language,
                },
            )
            self.notify(f"{way.name} — archived")
            self.load()
        finally:
            self._busy = False

    # --- navigation --------------------------------------------------------

    def action_focus_problems(self) -> None:
        self.query_one("#method-problems", OptionList).focus()

    def action_focus_ways(self) -> None:
        self.query_one("#method-ways", OptionList).focus()

    def action_back(self) -> None:
        self.app.pop_screen()
