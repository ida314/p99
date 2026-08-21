"""Settings: the knobs worth changing between runs, without leaving the app.

Every change is a `settings_changed` event, so the settings screen is the same
kind of thing as the run loop — an append to the log, folded into a projection.
`config.toml` is never rewritten: it stays the base layer with its comments
intact, and `x` clears an override to fall back to it. That is why each row
says where its value came from.

Most rows hold a value. One does not: warming the offline cache is an action,
and it lives here rather than on the home menu because it is meaningless apart
from the switch directly above it — turning offline mode on without warming the
cache first is the one way to reach a plane with nothing to open. The home menu
is for starting and reviewing runs; packing for a flight is not that.
"""

from __future__ import annotations

from dataclasses import dataclass

from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import Screen
from textual.widgets import Footer, OptionList, Static
from textual.widgets.option_list import Option as ListOption

from rich.text import Text

from ... import config as config_module
from ...render import WIDTH
from ..vim import MOTIONS, VimMotion


@dataclass(frozen=True)
class Action:
    """A row that does something instead of holding a value.

    Deliberately not a `config.Option` with a fake value list: `x` (back to
    config.toml) and `h`/`l` (step the value) have nothing to say about it, and
    the guards that skip it read better than three special cases would.
    """

    key: str
    label: str
    help: str
    #: A method on the app, so this screen does not import another screen and
    #: the guard that keeps `fetch` from opening behind a live run stays in the
    #: one place that already has it.
    app_action: str


#: Actions, each anchored below the option it belongs to rather than parked on
#: the end. `config.options()` stays a list of values only, which is what its
#: own "new options go on the end" note is protecting.
ACTIONS_BELOW = {
    "cache.offline": Action(
        "cache.warm",
        "warm the cache",
        "download the active list for offline use — minutes, and it can be stopped",
        "action_fetch",
    ),
}


class SettingsScreen(VimMotion, Screen):
    # `h`/`l` change the value under the cursor, which is the one place in the
    # app where a motion key does something — and it is safe here for the same
    # reason it is not on the solve screen: nothing you can do with it is
    # irreversible, and `h` puts back exactly what `l` took.
    BINDINGS = [
        *MOTIONS,
        Binding("l", "next_value", "change"),
        Binding("right", "next_value", "change", show=False),
        Binding("enter", "next_value", "change", show=False),
        Binding("space", "next_value", "change", show=False),
        Binding("h", "prev_value", "back", show=False),
        Binding("left", "prev_value", "back", show=False),
        Binding("x", "clear", "use config.toml"),
        Binding("escape", "close", "back"),
        Binding("q", "close", "back", show=False),
    ]

    VIM_TARGET = "#settings-list"

    def compose(self) -> ComposeResult:
        yield Static("  settings", classes="section-title")
        yield OptionList(id="settings-list")
        yield Static(id="settings-help")
        yield Static(id="settings-source")
        yield Static(
            "  h/l change    enter run    x back to config.toml    escape done",
            classes="hint-bar",
        )
        yield Footer()

    def on_mount(self) -> None:
        self._populate()
        listing = self.query_one("#settings-list", OptionList)
        listing.highlighted = 0
        listing.focus()

    def on_screen_resume(self) -> None:
        # Coming back from the fetch screen: the cache is warmer than it was,
        # and `cache.offline` may have been flipped there.
        self._populate()

    # --- rendering --------------------------------------------------------

    @property
    def options(self) -> tuple[config_module.Option, ...]:
        return config_module.options()

    @property
    def rows(self) -> tuple[config_module.Option | Action, ...]:
        """What is actually on screen, in order — values with their actions.

        The one mapping from row index to meaning. `current` reads it rather
        than indexing `options()`, which stopped being the same list the moment
        an action row went in the middle of it.
        """
        out: list[config_module.Option | Action] = []
        for option in self.options:
            out.append(option)
            extra = ACTIONS_BELOW.get(option.key)
            if extra is not None:
                out.append(extra)
        return tuple(out)

    def _overrides(self) -> dict[str, object]:
        return config_module.overrides(self.app.conn)  # type: ignore[attr-defined]

    def _row(self, option: config_module.Option, value: object, overridden: bool) -> Text:
        line = Text("  ")
        line.append(f"{option.label:<24}", style="bright_black")
        line.append(f"{option.render(value):<16}", style="bold")
        line.append("• set here" if overridden else "", style="cyan")
        return line

    def _action_row(self, action: Action) -> Text:
        """An action row, indented under the switch it serves.

        Arrowed and dimmed rather than styled like a value, because the column
        a value would sit in is empty here and an empty column in a list of
        values reads as a bug.
        """
        line = Text("  ")
        line.append(f"{'→ ' + action.label:<24}", style="bright_black")
        line.append("enter", style="cyan")
        return line

    def _populate(self) -> None:
        listing = self.query_one("#settings-list", OptionList)
        cfg = self.app.config  # type: ignore[attr-defined]
        overrides = self._overrides()
        # Rebuilt wholesale on every change: a handful of rows, and a full
        # rebuild can never leave a row showing a value the config no longer
        # holds.
        keep = listing.highlighted
        listing.clear_options()
        for row in self.rows:
            if isinstance(row, Action):
                listing.add_option(ListOption(self._action_row(row), id=row.key))
                continue
            listing.add_option(
                ListOption(
                    self._row(row, config_module.value_of(cfg, row), row.key in overrides),
                    id=row.key,
                )
            )
        if keep is not None:
            listing.highlighted = min(keep, listing.option_count - 1)
        self._describe()

    def _describe(self) -> None:
        row = self.current
        if row is None:
            return
        self.query_one("#settings-help", Static).update(
            Text(f"  {row.help}", style="bright_black")
        )
        if isinstance(row, Action):
            self.query_one("#settings-source", Static).update(
                Text(f"  {row.key} — enter runs it; nothing is written", style="bright_black")
            )
            return
        option = row
        overridden = option.key in self._overrides()
        source = (
            f"  {option.key} — changed here; x puts it back to config.toml"
            if overridden
            else f"  {option.key} — from config.toml"
        )
        self.query_one("#settings-source", Static).update(
            Text(source[: WIDTH + 8], style="bright_black")
        )

    # --- state ------------------------------------------------------------

    @property
    def current(self) -> config_module.Option | Action | None:
        listing = self.query_one("#settings-list", OptionList)
        index = listing.highlighted
        rows = self.rows
        if index is None or not (0 <= index < len(rows)):
            return None
        return rows[index]

    def _run_current_action(self) -> bool:
        """Fire the row under the cursor if it is an action. True if it was."""
        row = self.current
        if not isinstance(row, Action):
            return False
        getattr(self.app, row.app_action)()
        return True

    def _apply(self, delta: int) -> None:
        option = self.current
        if not isinstance(option, config_module.Option):
            return
        cfg = self.app.config  # type: ignore[attr-defined]
        new_value = option.step(config_module.value_of(cfg, option), delta)
        config_module.set_option(self.app.conn, option.key, new_value)  # type: ignore[attr-defined]
        self.app.reload_config()  # type: ignore[attr-defined]
        self._populate()

    # --- actions ----------------------------------------------------------

    def on_option_list_option_highlighted(self, event: OptionList.OptionHighlighted) -> None:
        self._describe()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        # The list gets `enter` before the screen does, so selection has to mean
        # the same thing `l` does or enter would silently do nothing.
        if self._run_current_action():
            return
        self._apply(1)

    def action_next_value(self) -> None:
        if self._run_current_action():
            return
        self._apply(1)

    def action_prev_value(self) -> None:
        # `h` deliberately does not fire an action. A motion key that goes back
        # everywhere else must not start a multi-minute download here.
        self._apply(-1)

    def action_clear(self) -> None:
        """Forget the override for this row and take the file's answer again."""
        option = self.current
        if not isinstance(option, config_module.Option):
            return
        if option.key not in self._overrides():
            return
        config_module.clear_option(self.app.conn, option.key)  # type: ignore[attr-defined]
        self.app.reload_config()  # type: ignore[attr-defined]
        self._populate()

    def action_close(self) -> None:
        self.dismiss()
