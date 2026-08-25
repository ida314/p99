"""The approach library: every route you know through a problem, and its code.

One problem, many approaches. The strategy prompt after a solve is where they
get named; this is where they accumulate into something you can read. A problem
you have solved three ways has three files here, each one labelled with the
technique it is, and the empty rows are as much the point as the full ones --
"there is a monotonic stack solution and I have never written it" is a fact
about your preparation that no attempt record can state.

Deliberately not a schedule. Nothing here is due, nothing here is graded, and
naming a second approach does not shorten an interval: the card is the problem's
and stays the problem's. The library is a record you go to, not a queue that
comes to you.

`e` opens `$EDITOR` on the highlighted approach, which is the one write this
screen does. It emits `solution_archived` -- an event with no attempt on it,
because there was no attempt: you did not sit a timed problem, you sat down with
a route you named months ago and finally wrote it. It scores nothing for exactly
that reason.

The one thing this screen must never become is a way to read the answer while
solving. `solve._render_problem` withholds approach names for the same reason it
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

from ... import capture, catalog, events, strategies
from ...render import approach_library
from ..vim import MOTIONS, VimMotion


class ApproachesScreen(VimMotion, Screen[None]):
    """Problems on the left, that problem's approaches on the right.

    Same two-pane shape as the history screen, and `h`/`l` move between the
    panes there too — one layout for "a list, and the detail of whichever row
    you are on" rather than two that have to be learned separately.
    """

    BINDINGS = [
        *MOTIONS,
        Binding("escape", "back", "back"),
        Binding("q", "back", "back", show=False),
        Binding("e", "write_code", "write code"),
        Binding("h", "focus_list", "problems", show=False),
        Binding("l", "focus_detail", "approaches", show=False),
    ]

    VIM_TARGET = "#approach-problems"

    def __init__(self) -> None:
        super().__init__()
        self.problems: list = []
        #: The approaches of whichever problem the cursor is on, in the order
        #: they are drawn, so `e` can act on the highlighted row without asking
        #: the database again for a list it just rendered.
        self.approaches: list[strategies.Approach] = []
        self._busy = False

    def compose(self) -> ComposeResult:
        yield Static("  approaches", classes="section-title")
        with Horizontal(id="approach-body"):
            yield OptionList(id="approach-problems")
            with VerticalScroll(id="approach-detail"):
                yield OptionList(id="approach-list")
        yield Footer()

    def on_mount(self) -> None:
        self.load()

    def on_screen_resume(self) -> None:
        # A run finishing behind this screen adds approaches to it, and so does
        # `e` — which suspends the app rather than pushing a screen, but lands
        # back here either way.
        self.load()

    # --- the problem list -------------------------------------------------

    def load(self) -> None:
        conn = self.app.conn  # type: ignore[attr-defined]
        widget = self.query_one("#approach-problems", OptionList)
        highlighted = widget.highlighted
        self.problems = strategies.problems_with_approaches(conn)
        widget.clear_options()
        widget.border_title = f"{len(self.problems)} problems"
        if not self.problems:
            # In the list rather than mounted beside it: `load` runs again on
            # every resume, and a widget mounted here would stack up a copy of
            # itself each time.
            listing = self.query_one("#approach-list", OptionList)
            listing.clear_options()
            listing.add_option(
                Option(
                    Text(
                        "  nothing named yet — name an approach after your next solve",
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
        # itself — a reload after writing a file would otherwise send you to the
        # top of the list, away from the problem you were just working through.
        widget.highlighted = (
            min(highlighted, len(self.problems) - 1) if highlighted is not None else 0
        )
        self._show(self.problems[widget.highlighted]["slug"])

    @staticmethod
    def _problem_label(row) -> Text:
        """One problem: its title, and how much of its library has code.

        `2/3` rather than a bare count, because the ratio is the thing worth
        seeing from the list — a problem with three named approaches and one
        written is a different state than one with three of three.
        """
        line = Text("  ")
        line.append(row["title"])
        counts = f"{row['written'] or 0}/{row['approaches']}"
        pad = max(1, 34 - len(row["title"]) - len(counts))
        line.append(" " * pad)
        line.append(counts, style="bright_black")
        return line

    def on_option_list_option_highlighted(self, event: OptionList.OptionHighlighted) -> None:
        if event.option_list.id == "approach-problems" and event.option.id:
            self._show(event.option.id)

    def _show(self, slug: str) -> None:
        conn = self.app.conn  # type: ignore[attr-defined]
        self.approaches = strategies.library(conn, slug)
        widget = self.query_one("#approach-list", OptionList)
        widget.clear_options()
        widget.border_title = slug
        for row in approach_library(self.approaches):
            widget.add_option(Option(row, id=None, disabled=True))
        # Rows are drawn disabled and the cursor lives on the problem list: the
        # right pane is a read, and `e` acts on the approach the *left* cursor
        # names. Nothing here is selectable, so nothing here needs a cursor.

    # --- writing code for one approach ------------------------------------

    def action_write_code(self) -> None:
        self.run_worker(self._do_write_code(), exclusive=True)

    async def _do_write_code(self) -> None:
        """Open `$EDITOR` for an approach that has no code yet.

        Offers the first unwritten approach on the highlighted problem rather
        than asking which one. That is the whole job of this key: the written
        ones already have their file, and the gap is what you came here to
        close. Alphabetical, so a problem with two gaps closes them in a
        predictable order across two presses.
        """
        if self._busy:
            return
        slug = self._highlighted_slug()
        if slug is None:
            return
        pending = [a for a in self.approaches if not a.written]
        if not pending:
            self.notify("every approach on this problem already has code")
            return
        approach = pending[0]

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
                    result = capture.capture_library_solution(
                        problem,
                        strategies.Strategy(key=approach.key, name=approach.name),
                        cfg.capture.language,
                    )
            except SuspendNotSupported:
                self.notify("this terminal can't hand off to $EDITOR", severity="warning")
                return
            if not (result.saved and result.path):
                self.notify(f"{approach.name} — skipped")
                return
            events.append(
                conn,
                events.SOLUTION_ARCHIVED,
                {
                    "slug": slug,
                    "approach": approach.name,
                    "code_path": str(result.path),
                    "language": cfg.capture.language,
                },
            )
            self.notify(f"{approach.name} — archived")
            self.load()
        finally:
            self._busy = False

    def _highlighted_slug(self) -> str | None:
        widget = self.query_one("#approach-problems", OptionList)
        if widget.highlighted is None or not widget.option_count:
            return None
        return widget.get_option_at_index(widget.highlighted).id

    # --- navigation --------------------------------------------------------

    def action_focus_list(self) -> None:
        self.query_one("#approach-problems", OptionList).focus()

    def action_focus_detail(self) -> None:
        self.query_one("#approach-detail", VerticalScroll).focus()

    def action_back(self) -> None:
        self.app.pop_screen()
