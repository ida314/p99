"""The only place the project's name is written down.

Everything user-visible that says what this tool is *called* reads from here:
the TUI title and banner, the CLI program name, the config file header, the
comment header stamped onto archived solutions, the XDG directory names, and
the environment-variable prefix. No other module hardcodes the name, and none
of them should — a rebrand is this file plus the two branded lines in
`pyproject.toml` (`[project] name` and the `[project.scripts]` entry).

`SLUG` is the on-disk and environment identity, deliberately separate from
`NAME`: it has to survive being a directory name and an environment-variable
prefix. Changing it moves `~/.config/<slug>/` and `~/.local/share/<slug>/` and
renames every `<SLUG>_*` variable, so an existing install needs its data
directory moved by hand. That is the real cost of a rename and it belongs here
in the open, not scattered across twenty modules.
"""

from __future__ import annotations

#: Display name — window title, headers, prose.
NAME = "p99"

#: Filesystem and environment identity. Lowercase, no spaces.
SLUG = "p99"

#: The command users type. Appears in help text and "run `x`" hints.
COMMAND = "p99"

#: One line under the banner.
TAGLINE = "you vs. your past self"

#: One sentence, for `--help`.
DESCRIPTION = "Timed, scored, permanently-recorded interview prep runs."

#: The home screen banner. Hand-drawn per brand; there is no generator.
BANNER = r"""
                 ___   ___
     _ __       / _ \ / _ \
    | '_ \     | (_) | (_) |
    | |_) |     \__, |\__, |
    | .__/        /_/   /_/
    |_|
"""

#: Prefix for every environment override, e.g. `P99_HOME`.
ENV_PREFIX = SLUG.upper().replace("-", "_")


def env(name: str) -> str:
    """The environment variable named `<PREFIX>_<NAME>`."""
    return f"{ENV_PREFIX}_{name}"
