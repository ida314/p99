"""Put text on the system clipboard.

Shaped like `audio`: the clipboard tool is spawned, never linked against, and a
failure comes back as a return value. The caller has a fallback worth trying —
Textual's OSC 52 escape, which works over ssh but which plenty of terminals
(GNOME Terminal, for one) ignore — so this never raises.

Which tool is picked by what the session actually is rather than what happens
to be installed: `xsel` is often present on a Wayland desktop, where it writes
to an XWayland clipboard that native apps may never see.
"""

from __future__ import annotations

import os
import shutil
import subprocess

#: Long enough for a clipboard daemon to take the text, short enough that a
#: wedged one cannot hold up the finish prompt.
TIMEOUT = 2.0


def _commands() -> list[list[str]]:
    out: list[list[str]] = []
    if os.environ.get("WAYLAND_DISPLAY"):
        out.append(["wl-copy"])
    if os.environ.get("DISPLAY"):
        out.append(["xclip", "-selection", "clipboard"])
        out.append(["xsel", "--clipboard", "--input"])
    out.append(["pbcopy"])
    return out


def copy(text: str) -> bool:
    """True if a clipboard tool took `text`."""
    for command in _commands():
        if shutil.which(command[0]) is None:
            continue
        try:
            # Output to /dev/null, not a pipe: wl-copy and xsel fork a child to
            # serve the selection, and that child would hold a captured pipe
            # open until something else took the clipboard.
            result = subprocess.run(
                command,
                input=text.encode(),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=TIMEOUT,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if result.returncode == 0:
            return True
    return False
