"""Filesystem layout (spec §3).

    ~/.config/<slug>/config.toml
    ~/.config/<slug>/session               (0600, optional)
    ~/.local/share/<slug>/<slug>.db
    ~/.local/share/<slug>/code/<problem-slug>/<attempt_id>.<ext>
    ~/.local/share/<slug>/code/<problem-slug>/<attempt_id>-wrong<n>.<ext>
    ~/.local/share/<slug>/notes/<problem-slug>/<attempt_id>.md
    ~/.local/share/<slug>/cache/<problem-slug>.html

`<slug>` is `branding.SLUG`; nothing here spells the name out. Everything is
overridable with `<PREFIX>_HOME` so tests and throwaway profiles never touch
the real database.
"""

from __future__ import annotations

import os
from pathlib import Path

from . import branding

APP = branding.SLUG

ENV_HOME = branding.env("HOME")
ENV_DB = branding.env("DB")


def _env_home() -> Path | None:
    raw = os.environ.get(ENV_HOME)
    return Path(raw).expanduser() if raw else None


def config_dir() -> Path:
    if (home := _env_home()) is not None:
        return home / "config"
    base = os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config"
    return Path(base) / APP


def data_dir() -> Path:
    if (home := _env_home()) is not None:
        return home / "data"
    base = os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share"
    return Path(base) / APP


def config_file() -> Path:
    return config_dir() / "config.toml"


def session_file() -> Path:
    """The LeetCode session cookie, for caching premium problems.

    A file of its own rather than a key in `config.toml` or the settings table:
    the settings table is a projection of the append-only log, so a credential
    put there could never be deleted, would survive every replay, and would sit
    in the one file you would hand someone while debugging.
    """
    return config_dir() / "session"


def db_file() -> Path:
    if raw := os.environ.get(ENV_DB):
        return Path(raw).expanduser()
    return data_dir() / f"{APP}.db"


def code_dir() -> Path:
    return data_dir() / "code"


def notes_dir() -> Path:
    return data_dir() / "notes"


def cache_dir() -> Path:
    """Offline problem statements. Derived, disposable, safe to delete.

    Unlike everything else under `data_dir`, nothing here is yours: it is
    `<command> fetch`'s copy of the catalog's problem pages, and the manifest
    that tracks it lives inside the directory rather than in the database so
    that deleting the directory is a complete reset.
    """
    return data_dir() / "cache"


def code_path(slug: str, attempt_id: int, ext: str) -> Path:
    ext = ext.lstrip(".")
    return code_dir() / slug / f"{attempt_id}.{ext}"


def submission_path(slug: str, attempt_id: int, n: int, ext: str) -> Path:
    """A failed submit's code, beside the solution it eventually became."""
    ext = ext.lstrip(".")
    return code_dir() / slug / f"{attempt_id}-wrong{n}.{ext}"


def note_path(slug: str, attempt_id: int) -> Path:
    return notes_dir() / slug / f"{attempt_id}.md"


def cache_path(slug: str) -> Path:
    return cache_dir() / f"{slug}.html"


def cache_manifest() -> Path:
    return cache_dir() / "manifest.json"


def ensure_dirs() -> None:
    for d in (config_dir(), data_dir(), code_dir(), notes_dir(), cache_dir()):
        d.mkdir(parents=True, exist_ok=True)
