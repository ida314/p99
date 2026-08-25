"""The strategy prompt: which approach you took, and which better one you see.

Runs once per solve, after the verdict prompt and before the two `$EDITOR`
handoffs. It is the answer to "can you identify a meaningfully better approach?"
asked in the only form worth recording -- not a yes, but a name.

Three roles, and the differences between them are what the rest of the app
reads:

  space  what you wrote
  a      an equal alternative you did not write
  w      the better approach you can see now, and did not write

A solve that was beaten on time and named the better approach found the pattern
late. One that was beaten and named nothing missed it. Only the second needs the
problem back soon, and `srs.rate` is what tells them apart.

`a` is the one role nothing grades. "There is a monotonic stack solution and
mine is a heap and they are both fine" is a fact about the problem, and the
place it goes is the problem's approach library -- not the scheduler, which
would read it as an admission that you were beaten.

The list is in two sections: the approaches this problem already has, then the
rest of your vocabulary. Same list as before, with the boundary drawn: after a
few months the second section is long, and the handful of names that matter here
are the ones this problem has already seen.

`esc` steps back to the verdict prompt with everything you answered still in it,
because `esc` means "back one screen" on every other modal in here and this one
is not special. Nothing has been written by the time you get here -- the attempt
is committed after this screen, not before -- so the step back is real rather
than an undo. Skipping still costs nothing: save with nothing picked and nothing
is recorded, which is the same outcome the old `esc` had.

Each section is alphabetical and both start empty: there is no supplied taxonomy
of techniques here, because the vocabulary that helps is the one in the words you
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

from ... import scoring, strategies
from ...render import QUALITY_LABELS, quality_reason
from .finish import SIGNAL_BACK
from ..vim import MOTIONS, VimMotion

#: What sits in front of a row, by role. Two columns wide, always drawn, so the
#: names line up whether or not anything is picked -- the same reason
#: `render.mastered_prefix` pads instead of omitting.
MARKS = {
    strategies.USED: ("[x]", "bold green"),
    strategies.ALSO_WORKS: ("[a]", "cyan"),
    strategies.WORTH_LEARNING: ("[>]", "yellow"),
    None: ("[ ]", "bright_black"),
}

#: The two section headers, in order. Drawn as disabled rows so `j` and `k`
#: step over them: a cursor that can land on a heading is a cursor that makes
#: `space` do nothing, which reads as a broken key rather than a wrong row.
HERE = "on this problem"
ELSEWHERE = "elsewhere in your vocabulary"


class StrategyModal(VimMotion, ModalScreen[dict[str, list[str]] | None]):
    """Pick the approaches you used, the equal ones, and the ones worth learning.

    Dismisses with a `strategies.payload()` block, with `{SIGNAL_BACK: True}` to
    reopen the verdict prompt, or with None when nothing was picked -- because
    recording that you looked at the list and chose nothing is not a fact about
    the solve.
    """

    BINDINGS = [
        *MOTIONS,
        Binding("space", "toggle_used", "used"),
        # Free of the motion set, and the two letters this screen binds. `h`/`l`
        # are not bound at all here: there is nothing sideways on this screen,
        # and a motion that does nothing is better than one that guesses.
        Binding("a", "toggle_also", "also works"),
        Binding("w", "toggle_worth", "worth learning"),
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
        attempt: dict | None = None,
        picked: dict[str, tuple[str, str]] | None = None,
    ):
        super().__init__()
        self.problem_title = title
        self.slug = slug
        # The finish prompt's answers, so the quality line can be derived live.
        # A plain dict rather than a row: this screen runs before anything is
        # written, and the attempt it describes does not exist yet.
        self.attempt = dict(attempt or {})
        #: The two sections, each alphabetical: the approaches this problem
        #: already has, then the rest of the vocabulary. A technique you named
        #: on another problem is exactly the one worth being offered here, and
        #: it stays offered -- it just stops being the first thing you read.
        self.here: list[strategies.Strategy] = []
        self.elsewhere: list[strategies.Strategy] = []
        #: Held on the screen rather than in the widget, like `SetupScreen.chosen`
        #: -- an OptionList only knows the rows it is currently showing, so
        #: filtering would silently drop every pick that scrolled out of view.
        self.roles: dict[str, str] = {}
        #: Names as typed, keyed the same way, so what is saved is your spelling.
        self.names: dict[str, str] = {}
        self._syncing = False
        # What was picked before you stepped back to the verdict prompt, as
        # `{key: (role, name)}`, so coming forward again is a round trip rather
        # than a form to fill in twice. The name travels with the role because
        # `on_mount` rebuilds the list from the database and nothing about this
        # attempt is in the database yet -- a strategy you typed here exists
        # only in this dict until the attempt is committed.
        self._restore = dict(picked or {})

    # --- composition -----------------------------------------------------

    def compose(self) -> ComposeResult:
        with Vertical(id="strategy-box"):
            yield Static(self.problem_title, classes="modal-title")
            yield Static("how did you solve it? — optional", classes="field-label")
            yield OptionList(id="strategy-list")
            yield Input(placeholder="name another approach…  enter adds it", id="strategy-new")
            yield Static(id="strategy-quality", classes="panel")
            yield Static(
                "  space used    a also works    w worth learning    i add one"
                "    ctrl+s save    esc back to the verdict",
                classes="hint-bar",
            )
            with Horizontal(id="confirm-buttons"):
                yield Button("save  (ctrl+s)", variant="primary", id="save")
                yield Button("back  (esc)", id="back")

    def on_mount(self) -> None:
        conn = self.app.conn  # type: ignore[attr-defined]
        here = strategies.for_problem(conn, self.slug)
        self.here = self._sorted(here)
        self.elsewhere = self._sorted(
            [s for s in strategies.vocabulary(conn) if s.key not in {e.key for e in here}]
        )
        self.names = {s.key: s.name for s in (*self.here, *self.elsewhere)}
        # Anything picked before a step back, including a name that only exists
        # because you typed it: it is in `_restore` and not yet on either list,
        # so it has to be put back on one as well as back in the roles. It goes
        # in the top section, which is where you put it: you typed it here, on
        # this problem, moments ago.
        for key, (role, name) in self._restore.items():
            if key not in self.names:
                self._add_here(strategies.Strategy(key=key, name=name))
            self.roles[key] = role
        self._populate()
        self._refresh_quality()
        self.query_one("#strategy-list", OptionList).focus()

    @staticmethod
    def _sorted(group: list[strategies.Strategy]) -> list[strategies.Strategy]:
        """One alphabetical section, deduped by key.

        First-wins on the dedupe rather than last, so a strategy already on this
        problem keeps the spelling it was recorded under here.
        """
        seen: dict[str, strategies.Strategy] = {}
        for entry in group:
            seen.setdefault(entry.key, entry)
        return sorted(seen.values(), key=lambda s: s.name.lower())

    def _add_here(self, entry: strategies.Strategy) -> None:
        """Put a newly named approach in the top section and remember its name."""
        self.here = self._sorted([*self.here, entry])
        self.elsewhere = [s for s in self.elsewhere if s.key != entry.key]
        self.names[entry.key] = entry.name

    # --- the list --------------------------------------------------------

    def _label(self, key: str) -> Text:
        """One row, as `Text` and never a plain string.

        Textual reads a prompt for console markup, so `[x]` would be parsed as a
        tag and the marker would render as nothing at all. This is the same trap
        `SetupScreen._label` documents, and it fails silently both times.
        """
        mark, style = MARKS[self.roles.get(key)]
        line = Text("  ")
        line.append(mark, style=style)
        line.append(" ")
        line.append(self.names.get(key, key))
        role = self.roles.get(key)
        if role:
            pad = max(1, 40 - len(self.names.get(key, key)))
            line.append(" " * pad)
            line.append(strategies.ROLE_LABELS[role], style="bright_black")
        return line

    def _header(self, title: str) -> Text:
        """A section heading, indented to sit under the marks rather than beside."""
        return Text(f"  {title}", style="bold bright_black")

    def _visible(self) -> list[tuple[str | None, Text]]:
        """The rows to draw: `(key, label)` pairs, with `(None, heading)` headers.

        Filtered by whatever is in the box, and a section that filters down to
        nothing loses its heading with it -- a heading over no rows is a claim
        that there is something under it.
        """
        needle = self.query_one("#strategy-new", Input).value.strip().lower()
        rows: list[tuple[str | None, Text]] = []
        for title, group in ((HERE, self.here), (ELSEWHERE, self.elsewhere)):
            keys = [
                s.key
                for s in group
                if not needle or needle in self.names.get(s.key, s.key).lower()
            ]
            if not keys:
                continue
            rows.append((None, self._header(title)))
            rows.extend((k, self._label(k)) for k in keys)
        return rows

    def _populate(self, focus_key: str | None = None) -> None:
        """Redraw the list. `focus_key` parks the cursor on one row.

        The cursor is put back deliberately rather than left to reset: a fresh
        OptionList starts with it unset, so a redraw after every keystroke in the
        filter box would send `j` back to the top of a list you were halfway
        down. `focus_key` is the other half -- naming a new approach has to land
        the cursor *on* it, or the next `w` flags whatever happened to be
        alphabetically first instead.

        Indices here are into the *option list*, which has headings in it. They
        are not indices into the keys, and conflating the two is how the cursor
        ends up one row above whatever you just typed.
        """
        widget = self.query_one("#strategy-list", OptionList)
        highlighted = widget.highlighted
        self._syncing = True
        try:
            widget.clear_options()
            rows = self._visible()
            if rows:
                widget.add_options(
                    [
                        Option(label, id=key, disabled=key is None)
                        for key, label in rows
                    ]
                )
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
        keys = [key for key, _ in rows]
        if focus_key is not None and focus_key in keys:
            widget.highlighted = keys.index(focus_key)
        elif highlighted is not None:
            widget.highlighted = min(highlighted, widget.option_count - 1)
        else:
            widget.highlighted = 0
        # Never leave the cursor on a heading. It can land on one from either
        # branch above -- a clamp onto the last row of a list that just shrank,
        # or row 0 of a list whose first row is a heading, which is every list
        # this screen draws. Forwards first, then backwards, so the nudge is
        # always onto the section the heading introduces.
        at = widget.highlighted or 0
        if keys and keys[at] is None:
            after = [i for i, key in enumerate(keys) if key is not None and i > at]
            before = [i for i, key in enumerate(keys) if key is not None and i < at]
            if after or before:
                widget.highlighted = after[0] if after else before[-1]

    def _current_key(self) -> str | None:
        widget = self.query_one("#strategy-list", OptionList)
        if widget.highlighted is None or not widget.option_count:
            return None
        try:
            return widget.get_option_at_index(widget.highlighted).id
        except Exception:
            return None

    # --- the derived line ------------------------------------------------

    def _refresh_quality(self) -> None:
        """Show what the answers so far come to, and what decided it.

        Live, because this is the moment the last input is still yours to
        change: the quality is derived from the optimality you just answered and
        the strategies on this screen, and a derived value nobody sees is a value
        nobody can catch being wrong. The second line names every input, so the
        derivation can be checked without reading the source.
        """
        facts = {
            **self.attempt,
            "strategies_used": self._named(strategies.USED),
            "strategies_also_works": self._named(strategies.ALSO_WORKS),
            "strategies_worth_learning": self._named(strategies.WORTH_LEARNING),
            "used_keys": frozenset(self._keys(strategies.USED)),
            "saw_better": bool(self._keys(strategies.WORTH_LEARNING)),
        }
        # No prior comparison here: this attempt has no id yet, so the one thing
        # this line cannot know is whether it is a different route than last
        # time. It says `optimal`, and the run summary upgrades it afterwards if
        # it was. Better a label that firms up than one that is wrong now.
        quality = scoring.solution_quality(facts)
        facts["solution_quality"] = quality

        body = Text()
        if quality is None:
            body.append("  quality   ", style="bright_black")
            body.append("not claimed", style="bright_black")
            body.append("\n            you answered 'not sure' on time", style="bright_black")
        else:
            body.append("  quality   ", style="bright_black")
            body.append(QUALITY_LABELS.get(quality, quality))
            reason = quality_reason(facts)
            if reason:
                body.append(f"\n            {reason}", style="bright_black")
        self.query_one("#strategy-quality", Static).update(body)

    def _keys(self, role: str) -> list[str]:
        return [k for k, r in self.roles.items() if r == role]

    def _named(self, role: str) -> list[str]:
        return sorted(
            (self.names.get(k, k) for k in self._keys(role)), key=lambda n: n.lower()
        )

    # --- actions ---------------------------------------------------------

    def _toggle(self, role: str) -> None:
        key = self._current_key()
        if key is None:
            return
        # Toggling off rather than cycling: `space` and `w` each undo
        # themselves, and pressing one on a row the other owns moves it rather
        # than stacking. A strategy cannot be both what you wrote and what you
        # wish you had written.
        if self.roles.get(key) == role:
            del self.roles[key]
        else:
            self.roles[key] = role
        self._populate()
        self._refresh_quality()

    def action_toggle_used(self) -> None:
        self._toggle(strategies.USED)

    def action_toggle_also(self) -> None:
        self._toggle(strategies.ALSO_WORKS)

    def action_toggle_worth(self) -> None:
        self._toggle(strategies.WORTH_LEARNING)

    def action_focus_filter(self) -> None:
        self.query_one("#strategy-new", Input).focus()

    def on_input_changed(self) -> None:
        if not self._syncing:
            self._populate()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Enter in the box names a new strategy and marks it used.

        Marked used, not merely added: you typed it on the screen that asks what
        you wrote, immediately after writing it. Anything else would need a
        second keystroke to say the obvious thing.
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
            self._add_here(new)
        self.roles[new.key] = strategies.USED
        self._syncing = True
        try:
            widget.value = ""
        finally:
            self._syncing = False
        # Land the cursor on what you just named, so `w` right after `enter`
        # flags the thing you were looking at.
        self._populate(focus_key=new.key)
        self._refresh_quality()
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

    def action_save(self) -> None:
        block = strategies.payload(
            self._named(strategies.USED),
            also_works=self._named(strategies.ALSO_WORKS),
            worth_learning=self._named(strategies.WORTH_LEARNING),
        )
        self.dismiss(None if strategies.is_empty(block) else block)

    def action_back(self) -> None:
        """Step back to the verdict prompt, keeping both screens' answers.

        Not an undo and not a cancel: nothing about this attempt has been
        written yet, so the verdict prompt reopens as the live screen it was.
        The caller carries what is picked here back forward.

        `escape` inside the text box goes back to the list instead, so the way
        out of insert mode is never also the way out of the screen -- the same
        rule `SetupScreen.action_cancel` follows.
        """
        if getattr(self.focused, "id", None) == "strategy-new":
            self.query_one("#strategy-list", OptionList).focus()
            return
        self.dismiss(
            {
                SIGNAL_BACK: True,
                "picked": {k: (role, self.names.get(k, k)) for k, role in self.roles.items()},
            }
        )
