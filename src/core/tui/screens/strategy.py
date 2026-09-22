"""The strategy prompt: which patterns can solve this problem.

Runs once per solve, after the verdict prompt and before the methods page. One
question, and it is a question about the *problem*: which reusable techniques are
an answer to it. Every tag you have ever put on this problem is already ticked
when it opens, so tonight you are correcting a list rather than filling one in.

Tick min-heap, quickselect and sorting on a problem that admits all three, and
all three stay ticked next time -- including the times you reach for none of
them. That is what makes the vocabulary worth having: "every problem quickselect
is an answer to" is the list you want when you sit down to drill quickselect, and
a list of the evenings quickselect happened to be the route you took is not it.

Which route you took tonight is the methods page that follows this one, where a
whole route through this one problem gets a row of its own, with its own cost and
its own code. The two lists never reference each other: a strategy means the same
thing on every problem, a method means nothing away from its own, and a method
built out of three patterns says so in its name. See `methods`.

Unticking is how you take a tag off, and it takes it off the problem rather than
off tonight -- a claim you have decided is wrong should not come back on the next
solve. Saving with nothing ticked clears the list, which is why saving here
always answers, where it used to answer only when something was picked.

It used to ask more than this. A second role named the better approach you could
see and had not written, and a third named an equal one. Both were answers about
the *problem* wearing an attempt's clothes, and both now live on the methods page,
where a way to solve the problem can carry its own cost and its own code. What
that move bought: a route you notice two months later can be recorded when you
notice it, instead of only in the ninety seconds after a solve. Old answers under
the retired roles keep grading exactly as they did -- see `strategies` and
`srs.grade_attempt`.

`esc` steps back to the verdict prompt with everything you answered still in it,
because `esc` means "back one screen" on every other modal in here and this one
is not special. Nothing has been written by the time you get here -- the attempt
is committed after all three prompts, not before -- so the step back is real
rather than an undo.

The same screen opens off a run, from the patterns screen, to tag a problem you
never sat down to tonight. There `esc` is a cancel rather than a step back, since
there is no verdict prompt behind it to step to: see `offer_back`.

The list is alphabetical and starts empty: there is no supplied taxonomy of
techniques here, because the vocabulary that helps is the one in the words you
already use.
"""

from __future__ import annotations

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, OptionList, Static
from textual.widgets.option_list import Option

from ... import strategies
from .finish import SIGNAL_BACK
from ..vim import MOTIONS, VimMotion

#: What sits in front of a row. Two columns wide, always drawn, so the names
#: line up whether or not anything is picked -- the same reason
#: `render.mastered_prefix` pads instead of omitting.
MARKS = {
    strategies.USED: ("[x]", "bold green"),
    None: ("[ ]", "bright_black"),
}


class StrategyModal(VimMotion, ModalScreen[dict[str, list[str]] | None]):
    """Tick every pattern that can solve this problem, from your own vocabulary.

    Dismisses with a `strategies.payload()` block -- always, even an empty one,
    because the list is the problem's and clearing it is an answer. `None` is a
    cancel and only reachable when `offer_back` is off; the step back to the
    verdict prompt is `{SIGNAL_BACK: True}`.
    """

    BINDINGS = [
        *MOTIONS,
        Binding("space", "toggle_used", "solves it"),
        # `h`/`l` are not bound at all here: there is nothing sideways on this
        # screen, and a motion that does nothing is better than one that guesses.
        Binding("i", "focus_filter", "add one", show=False),
        Binding("slash", "focus_filter", "add one", show=False),
        Binding("ctrl+s", "save", "save"),
        Binding("escape", "back", "back"),
    ]

    VIM_TARGET = "#strategy-list"

    def __init__(
        self,
        title: str,
        slug: str,
        picked: list[tuple[str, str]] | None = None,
        *,
        offer_back: bool = True,
    ):
        super().__init__()
        self.problem_title = title
        self.slug = slug
        #: Whether `esc` steps back to the verdict prompt or simply closes. True
        #: in a run, where there is a screen behind this one; False off the
        #: patterns screen, where "back" would have nowhere to go and a cancel is
        #: the honest thing to offer.
        self.offer_back = offer_back
        #: Every strategy on offer, alphabetical. The ones you have already named
        #: on this problem first, then the rest of the vocabulary, because a
        #: technique you named on another problem is exactly the one worth being
        #: offered here.
        self.known: list[strategies.Strategy] = []
        #: Held on the screen rather than in the widget, like `SetupScreen.chosen`
        #: -- an OptionList only knows the rows it is currently showing, so
        #: filtering would silently drop every pick that scrolled out of view.
        self.chosen: set[str] = set()
        #: Names as typed, keyed the same way, so what is saved is your spelling.
        self.names: dict[str, str] = {}
        self._syncing = False
        # What was ticked before you stepped back, as `(key, name)` pairs, so
        # coming forward again is a round trip rather than a form to fill in
        # twice. The name travels with the key because `on_mount` rebuilds the
        # list from the database and nothing about this attempt is in the
        # database yet -- a strategy you typed here exists only in this list
        # until the attempt is committed.
        #
        # `None` and `[]` mean different things and the difference is the whole
        # round trip. `None` is "no answer yet", and the screen opens with the
        # problem's saved tags ticked; `[]` is "you came back having unticked
        # every one of them", and restoring the problem's tags over the top of
        # that would silently undo the only edit you made.
        self._restore = None if picked is None else list(picked)

    # --- composition -----------------------------------------------------

    def compose(self) -> ComposeResult:
        with Vertical(id="strategy-box"):
            yield Static(self.problem_title, classes="modal-title")
            yield Static(
                "which patterns can solve this problem? — optional",
                classes="field-label",
            )
            yield OptionList(id="strategy-list")
            yield Input(placeholder="name another pattern…  enter adds it", id="strategy-new")
            yield Static(
                "  space  a pattern that solves it    i add one    ctrl+s save"
                + ("    esc back to the verdict" if self.offer_back else "    esc cancel"),
                classes="hint-bar",
            )
            with Horizontal(id="confirm-buttons"):
                yield Button("save  (ctrl+s)", variant="primary", id="save")
                yield Button(
                    "back  (esc)" if self.offer_back else "cancel  (esc)", id="back"
                )

    def on_mount(self) -> None:
        conn = self.app.conn  # type: ignore[attr-defined]
        self.known = self._merge(
            strategies.for_problem(conn, self.slug), strategies.vocabulary(conn)
        )
        self.names = {s.key: s.name for s in self.known}
        if self._restore is None:
            # First time through: the problem's own tags, already ticked. This is
            # the screen's whole shape -- you are correcting a list, not filling
            # one in, and a technique that solved this problem in March is still
            # a technique that solves it tonight.
            self.chosen = {s.key for s in strategies.for_problem(conn, self.slug)}
        else:
            # Coming forward again after a step back, exactly as you left it,
            # including a name that only exists because you typed it: it is in
            # `_restore` and not yet in `known`, so it has to be put back on the
            # list as well as back in the ticks.
            for key, name in self._restore:
                if key not in self.names:
                    self.known = self._merge(
                        self.known, [strategies.Strategy(key=key, name=name)]
                    )
                    self.names[key] = name
                self.chosen.add(key)
        self._populate()
        self.query_one("#strategy-list", OptionList).focus()

    @staticmethod
    def _merge(*groups: list[strategies.Strategy]) -> list[strategies.Strategy]:
        """One alphabetical list, deduped by key, this problem's entries first.

        First-wins on the dedupe rather than last, so a strategy already on this
        problem keeps the spelling it was recorded under here.
        """
        seen: dict[str, strategies.Strategy] = {}
        for group in groups:
            for entry in group:
                seen.setdefault(entry.key, entry)
        return sorted(seen.values(), key=lambda s: s.name.lower())

    # --- the list --------------------------------------------------------

    def _label(self, key: str) -> Text:
        """One row, as `Text` and never a plain string.

        Textual reads a prompt for console markup, so `[x]` would be parsed as a
        tag and the marker would render as nothing at all. This is the same trap
        `SetupScreen._label` documents, and it fails silently both times.
        """
        mark, style = MARKS[strategies.USED if key in self.chosen else None]
        line = Text("  ")
        line.append(mark, style=style)
        line.append(" ")
        line.append(self.names.get(key, key))
        # No word after the name. There was one -- "used" -- back when a tick
        # meant "I reached for this tonight" and the column had something to add
        # to the box. A tick now means the problem can be solved this way, which
        # the question at the top already says and the box already shows.
        return line

    def _visible(self) -> list[str]:
        """Keys to draw, filtered by whatever is in the box, alphabetical."""
        needle = self.query_one("#strategy-new", Input).value.strip().lower()
        keys = [s.key for s in self.known]
        if not needle:
            return keys
        return [k for k in keys if needle in self.names.get(k, k).lower()]

    def _populate(self, focus_key: str | None = None) -> None:
        """Redraw the list. `focus_key` parks the cursor on one row.

        The cursor is put back deliberately rather than left to reset: a fresh
        OptionList starts with it unset, so a redraw after every keystroke in the
        filter box would send `j` back to the top of a list you were halfway
        down. `focus_key` is the other half -- naming a new approach has to land
        the cursor *on* it, or the next keystroke acts on whatever happened to be
        alphabetically first instead.
        """
        widget = self.query_one("#strategy-list", OptionList)
        highlighted = widget.highlighted
        self._syncing = True
        try:
            widget.clear_options()
            keys = self._visible()
            if keys:
                widget.add_options([Option(self._label(k), id=k) for k in keys])
            else:
                # Not an empty widget: the first time through, the only thing
                # this screen can tell you is how to put something in it.
                widget.add_options(
                    [
                        Option(
                            Text(
                                "  nothing named yet — type one below and press enter",
                                style="bright_black",
                            ),
                            id=None,
                            disabled=True,
                        )
                    ]
                )
        finally:
            self._syncing = False
        if not widget.option_count:
            return
        if focus_key is not None and focus_key in keys:
            widget.highlighted = keys.index(focus_key)
        elif highlighted is not None:
            widget.highlighted = min(highlighted, widget.option_count - 1)
        else:
            widget.highlighted = 0

    def _current_key(self) -> str | None:
        widget = self.query_one("#strategy-list", OptionList)
        if widget.highlighted is None or not widget.option_count:
            return None
        try:
            return widget.get_option_at_index(widget.highlighted).id
        except Exception:
            return None

    # --- actions ---------------------------------------------------------

    def action_toggle_used(self) -> None:
        key = self._current_key()
        if key is None:
            return
        # Toggling off rather than cycling: `space` undoes itself, which is the
        # only sane behaviour for a checkbox and the only behaviour this screen
        # needs now that there is one thing to check. Untick a tag the problem
        # came in wearing and saving takes it off the problem -- see
        # `strategies.set_payload`.
        self.chosen.symmetric_difference_update({key})
        self._populate()

    def action_focus_filter(self) -> None:
        self.query_one("#strategy-new", Input).focus()

    def on_input_changed(self) -> None:
        if not self._syncing:
            self._populate()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Enter in the box names a new pattern and ticks it.

        Ticked, not merely added: you typed it on the screen that asks which
        patterns solve this problem, while looking at the problem. Anything else
        would need a second keystroke to say the obvious thing.
        """
        typed = event.value.strip()
        widget = event.input
        if not typed:
            self.query_one("#strategy-list", OptionList).focus()
            return
        entry = strategies.clean([typed])
        if not entry:
            return
        new = entry[0]
        if new.key not in self.names:
            self.known = self._merge(self.known, [new])
            self.names[new.key] = new.name
        self.chosen.add(new.key)
        self._syncing = True
        try:
            widget.value = ""
        finally:
            self._syncing = False
        # Land the cursor on what you just named, so the next keystroke acts on
        # the thing you were looking at.
        self._populate(focus_key=new.key)
        self.query_one("#strategy-list", OptionList).focus()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        """`enter` on a row is the same as `space` on it."""
        if not self._syncing:
            self.action_toggle_used()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "save":
            self.action_save()
        else:
            self.action_back()

    def _named(self) -> list[str]:
        return sorted(
            (self.names.get(k, k) for k in self.chosen), key=lambda n: n.lower()
        )

    def action_save(self) -> None:
        """Answer with the list as it now stands, empty or not.

        Empty is an answer here and did not used to be. When a tick meant "I
        reached for this tonight", saving with nothing ticked was a prompt you
        skipped and recording it would have been recording that you looked at a
        list. The tick means the problem can be solved this way now, so an empty
        list says this problem carries no tags -- which is a thing you can only
        say by unticking the ones it had.
        """
        self.dismiss(strategies.payload(self._named()))

    def action_back(self) -> None:
        """Step back to the verdict prompt, keeping both screens' answers.

        Not an undo and not a cancel: nothing about this attempt has been
        written yet, so the verdict prompt reopens as the live screen it was.
        The caller carries what is ticked here back forward -- including what you
        unticked, which is why the ticks travel as a list and not as a flag.

        Off a run there is nothing behind this screen, so `esc` is a plain
        cancel: `None`, and the problem's tags are left exactly as they were.

        `escape` inside the text box goes back to the list instead, so the way
        out of insert mode is never also the way out of the screen -- the same
        rule `SetupScreen.action_cancel` follows.
        """
        if getattr(self.focused, "id", None) == "strategy-new":
            self.query_one("#strategy-list", OptionList).focus()
            return
        if not self.offer_back:
            self.dismiss(None)
            return
        self.dismiss(
            {
                SIGNAL_BACK: True,
                "picked": [(k, self.names.get(k, k)) for k in sorted(self.chosen)],
            }
        )
