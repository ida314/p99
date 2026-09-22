"""The patterns screen: which techniques solve which problems.

The same list the prompt after a solve edits, months later and out of a run. A
problem you have tagged with min-heap, quickselect and sorting has all three
here, whichever one you wrote the night you tagged it — the tags belong to the
problem, not to the evening. See `strategies`.

The point of coming here is the tag you think of afterwards. "That one is a
monotonic stack problem too" is a thing you notice on the bus a week later, and
the prompt after a solve is only open for ninety seconds; without this screen the
thought has nowhere to go until the next time the problem comes round. That is
the same gap `MethodsScreen` was built to close, and it is closed the same way.

Patterns, not methods. A tag says the problem *can* be solved with a technique
that means the same thing on every problem; a method is one route through this
problem and lives on the other screen. The two lists still never reference each
other — see `methods`.

One key writes:

  e  open the tag picker for the highlighted problem

It is the same `StrategyModal` the prompt after a solve opens, with `esc` turned
from a step back into a cancel, because there is no verdict prompt behind it here.
Saving emits `problem_strategies_set` — the whole list, so a tag you untick comes
off — exactly as it does at the end of a solve.

Deliberately not a schedule and not a drill. Tagging a problem does not put it in
a queue, and nothing here is due. What it builds is the list a drill would be
chosen from later: every problem quickselect is an answer to, which is a question
this screen holds the data for and does not yet ask.
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

from ... import events, strategies
from ...render import strategy_list
from ..vim import MOTIONS, VimMotion
from .strategy import StrategyModal


class StrategyScreen(VimMotion, Screen[None]):
    """Problems on the left, that problem's tags on the right.

    Same two-pane shape as the methods and history screens, and `h`/`l` move
    between the panes on all three. The right pane takes no cursor, like
    history's and unlike the methods screen's: nothing here acts on one tag, and
    a cursor you cannot use is a cursor you have to keep putting back.
    """

    BINDINGS = [
        *MOTIONS,
        Binding("escape", "back", "back"),
        Binding("q", "back", "back", show=False),
        Binding("e", "edit_tags", "tag problem"),
        Binding("h", "focus_problems", "problems", show=False),
        Binding("l", "focus_tags", "patterns", show=False),
    ]

    VIM_TARGET = "#pattern-problems"

    def __init__(self) -> None:
        super().__init__()
        self.problems: list = []

    def compose(self) -> ComposeResult:
        yield Static("  patterns", classes="section-title")
        with Horizontal(id="pattern-body"):
            yield OptionList(id="pattern-problems")
            with VerticalScroll(id="pattern-detail"):
                yield Static(id="pattern-tags")
        yield Footer()

    def on_mount(self) -> None:
        self.load()

    def on_screen_resume(self) -> None:
        # A run finishing behind this screen tags problems, and so does `e`.
        self.load()

    # --- the problem list -------------------------------------------------

    def load(self, focus_slug: str | None = None) -> None:
        conn = self.app.conn  # type: ignore[attr-defined]
        widget = self.query_one("#pattern-problems", OptionList)
        highlighted = widget.highlighted
        self.problems = strategies.problems_with_strategies(conn)
        widget.clear_options()
        widget.border_title = f"{len(self.problems)} problems"
        if not self.problems:
            # In the pane rather than mounted beside it, the same way
            # `MethodsScreen` does it: `load` runs again on every resume, and a
            # widget mounted here would stack up a copy of itself each time.
            self.query_one("#pattern-tags", Static).update(
                Text(
                    "  nothing tagged yet — the prompt after your next solve is "
                    "where this fills up",
                    style="bright_black",
                )
            )
            return
        slugs = [row["slug"] for row in self.problems]
        for row in self.problems:
            widget.add_option(Option(self._problem_label(row), id=row["slug"]))
        # Put the cursor back, as `OptionList` never does by itself — a reload
        # after `e` would otherwise send you to the top of the list, away from
        # the problem you were tagging. `focus_slug` is the other half: a
        # problem you just emptied drops out of the list entirely, so the caller
        # names what it wants rather than trusting an index.
        if focus_slug in slugs:
            widget.highlighted = slugs.index(focus_slug)
        elif highlighted is not None:
            widget.highlighted = min(highlighted, len(slugs) - 1)
        else:
            widget.highlighted = 0
        self._show(slugs[widget.highlighted])

    @staticmethod
    def _problem_label(row) -> Text:
        """One problem: its title and how many patterns it carries.

        Thirty columns and not a column more. The pane is 36 wide, its rounded
        border takes two, the `OptionList`'s own padding takes two more, and the
        scrollbar takes another two as soon as the list is longer than the
        window -- which is the state this screen is in for anyone who has been
        tagging. A row that outgrows what is left does not ellipsise: it wraps
        onto a second line and the list stops being scannable. Only a rendered
        screen shows that, so the budget is written down here.
        """
        count = str(row["tags"])
        # The title gives way to the count rather than the other way round: the
        # count is two characters at most and unreadable if it is cut, and a
        # title is recognisable long before its last word.
        title = row["title"][: 27 - len(count)]
        line = Text("  ")
        line.append(title)
        line.append(" " * max(1, 28 - len(title) - len(count)))
        line.append(count, style="bright_black")
        return line

    def on_option_list_option_highlighted(self, event: OptionList.OptionHighlighted) -> None:
        if event.option_list.id == "pattern-problems" and event.option.id:
            self._show(event.option.id)

    def _show(self, slug: str) -> None:
        conn = self.app.conn  # type: ignore[attr-defined]
        tags = strategies.for_problem(conn, slug)
        pane = self.query_one("#pattern-tags", Static)
        pane.update(Group(*strategy_list(tags, strategies.problem_counts(conn))))
        self.query_one("#pattern-detail", VerticalScroll).border_title = slug

    def _highlighted_slug(self) -> str | None:
        widget = self.query_one("#pattern-problems", OptionList)
        if widget.highlighted is None or not widget.option_count:
            return None
        return widget.get_option_at_index(widget.highlighted).id

    def _highlighted_title(self, slug: str) -> str:
        return next(
            (row["title"] for row in self.problems if row["slug"] == slug), slug
        )

    # --- tagging ----------------------------------------------------------

    def action_edit_tags(self) -> None:
        self.run_worker(self._do_edit_tags(), exclusive=True)

    async def _do_edit_tags(self) -> None:
        """Open the picker on the highlighted problem and record what comes back.

        The same event the prompt after a solve emits, for the same reason it is
        an event at all: the list it carries is the whole list, so a tag you took
        off is gone on the next replay too.
        """
        slug = self._highlighted_slug()
        if slug is None:
            self.app.bell()
            return
        block = await self.app.push_screen_wait(
            StrategyModal(self._highlighted_title(slug), slug, offer_back=False)
        )
        if block is None:  # cancelled
            return
        events.append(
            self.app.conn,  # type: ignore[attr-defined]
            events.PROBLEM_STRATEGIES_SET,
            strategies.set_payload(slug, block.get(strategies.USED) or []),
        )
        self.load(focus_slug=slug)

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        """`enter` on a problem is the same as `e` on it."""
        if event.option_list.id == "pattern-problems":
            self.action_edit_tags()

    # --- navigation --------------------------------------------------------

    def action_focus_problems(self) -> None:
        self.query_one("#pattern-problems", OptionList).focus()

    def action_focus_tags(self) -> None:
        self.query_one("#pattern-detail", VerticalScroll).focus()

    def action_back(self) -> None:
        self.app.pop_screen()
