"""Settings: the knobs worth changing between runs, without leaving the app.

Every change is a `settings_changed` event, so the settings screen is the same
kind of thing as the run loop — an append to the log, folded into a projection.
`config.toml` is never rewritten: it stays the base layer with its comments
intact, and `x` clears an override to fall back to it. That is why each row
says where its value came from.
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import Screen
from textual.widgets import Footer, OptionList, Static
from textual.widgets.option_list import Option as ListOption

from rich.text import Text

from ... import config as config_module
from ...render import WIDTH
from ..vim import MOTIONS, VimMotion


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
            "  h/l change    x back to config.toml    escape done",
            classes="hint-bar",
        )
        yield Footer()

    def on_mount(self) -> None:
        self._populate()
        listing = self.query_one("#settings-list", OptionList)
        listing.highlighted = 0
        listing.focus()

    # --- rendering --------------------------------------------------------

    @property
    def options(self) -> tuple[config_module.Option, ...]:
        return config_module.options()

    def _overrides(self) -> dict[str, object]:
        return config_module.overrides(self.app.conn)  # type: ignore[attr-defined]

    def _row(self, option: config_module.Option, value: object, overridden: bool) -> Text:
        line = Text("  ")
        line.append(f"{option.label:<24}", style="bright_black")
        line.append(f"{option.render(value):<16}", style="bold")
        line.append("• set here" if overridden else "", style="cyan")
        return line

    def _populate(self) -> None:
        listing = self.query_one("#settings-list", OptionList)
        cfg = self.app.config  # type: ignore[attr-defined]
        overrides = self._overrides()
        # Rebuilt wholesale on every change: six rows, and a full rebuild can
        # never leave a row showing a value the config no longer holds.
        keep = listing.highlighted
        listing.clear_options()
        for option in self.options:
            listing.add_option(
                ListOption(
                    self._row(option, config_module.value_of(cfg, option), option.key in overrides),
                    id=option.key,
                )
            )
        if keep is not None:
            listing.highlighted = min(keep, listing.option_count - 1)
        self._describe()

    def _describe(self) -> None:
        option = self.current
        if option is None:
            return
        self.query_one("#settings-help", Static).update(
            Text(f"  {option.help}", style="bright_black")
        )
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
    def current(self) -> config_module.Option | None:
        listing = self.query_one("#settings-list", OptionList)
        index = listing.highlighted
        if index is None or not (0 <= index < len(self.options)):
            return None
        return self.options[index]

    def _apply(self, delta: int) -> None:
        option = self.current
        if option is None:
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
        self._apply(1)

    def action_next_value(self) -> None:
        self._apply(1)

    def action_prev_value(self) -> None:
        self._apply(-1)

    def action_clear(self) -> None:
        """Forget the override for this row and take the file's answer again."""
        option = self.current
        if option is None:
            return
        if option.key not in self._overrides():
            return
        config_module.clear_option(self.app.conn, option.key)  # type: ignore[attr-defined]
        self.app.reload_config()  # type: ignore[attr-defined]
        self._populate()

    def action_close(self) -> None:
        self.dismiss()
