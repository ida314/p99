"""Post-solve capture: the two `$EDITOR` handoffs (spec §7).

Both are skippable in one keystroke and skipping costs nothing — no nag, no
"are you sure", no score penalty. The moment it feels mandatory you start
writing "idk" to get past it, which is worse than empty.

The reflection template is load-bearing. A blank buffer at 11pm after five
problems produces an empty file every single time; three concrete questions
produce two sentences, and two sentences is all the coach needs.

Notes are collected from day one even though nothing reads them until Phase 3:
metrics can be recomputed from the event log forever, reflections cannot be
written retroactively.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from . import branding, config, paths
from .catalog import Problem
from .scoring import VERDICT_LABELS, fmt_duration

COMMENT_PREFIX = {
    "py": "#",
    "rb": "#",
    "sh": "#",
    "go": "//",
    "rs": "//",
    "c": "//",
    "cpp": "//",
    "java": "//",
    "js": "//",
    "ts": "//",
    "kt": "//",
    "swift": "//",
    "scala": "//",
    "cs": "//",
    "sql": "--",
}

NOTE_TEMPLATE = """\
<!-- {app} reflection | {slug} -->
<!-- optional. :q! to skip. two sentences is plenty. -->

## what made it hard
<!-- the specific moment you were stuck, not "it was tricky" -->

## what unstuck you
<!-- the insight, the hint, the example you drew, the thing you googled -->

## if you saw this cold in 6 months
<!-- what would you want to have remembered? -->
"""


def note_template(slug: str) -> str:
    """The reflection buffer for one problem.

    Goes through here rather than `NOTE_TEMPLATE.format(...)` at the call site
    so nobody has to remember that the template carries the app name too — the
    emptiness check compares against this exact text, and a template rendered
    with a stray `{app}` in it would never match.
    """
    return NOTE_TEMPLATE.format(app=branding.NAME, slug=slug)


@dataclass(frozen=True)
class CaptureResult:
    saved: bool
    path: Path | None = None
    reason: str = ""


def editor_available() -> bool:
    cmd = config.editor()
    return bool(cmd) and shutil.which(cmd[0]) is not None


def _code_header(problem: Problem, attempt: dict, ext: str, status: str, prompt: str) -> str:
    """The three pre-filled comment lines every code buffer opens with."""
    prefix = COMMENT_PREFIX.get(ext, "#")
    started = attempt.get("started_at")
    when = ""
    if started:
        try:
            when = datetime.fromisoformat(started).astimezone().strftime("%Y-%m-%d %H:%M")
        except ValueError:
            when = started
    tier = int(attempt.get("max_hint_tier") or 0)
    hints = f"hints: tier {tier}" if tier else "hints: none"
    duration = fmt_duration(attempt.get("active_seconds"))
    return (
        f"{prefix} {branding.NAME} | {problem.slug} | {problem.difficulty_label}\n"
        f"{prefix} {when} | {duration} | {hints} | {status}\n"
        f"{prefix} {prompt} :wq to save, :q! to skip.\n\n"
    )


def solution_header(problem: Problem, attempt: dict, ext: str) -> str:
    """The pre-filled comment header on the solution buffer."""
    return _code_header(
        problem,
        attempt,
        ext,
        VERDICT_LABELS.get(attempt.get("verdict") or "", "UNRESOLVED"),
        "paste your solution below.",
    )


def submission_header(problem: Problem, attempt: dict, ext: str, n: int) -> str:
    """The same header, for the code behind a failed submit.

    The duration on line two is the clock at the moment of the submit, not at
    the end of the attempt — that is the point of keeping these: how far in you
    were when this particular wrong answer looked right.
    """
    return _code_header(
        problem,
        attempt,
        ext,
        f"FAILED SUBMIT #{n}",
        "paste the code you just submitted below.",
    )


def _strip_comments(text: str, prefix: str) -> str:
    lines = [ln for ln in text.splitlines() if not ln.strip().startswith(prefix)]
    return "\n".join(lines).strip()


def _strip_note_scaffolding(text: str) -> str:
    """What's left after removing HTML comments and the template's headings."""
    out: list[str] = []
    in_comment = False
    for raw in text.splitlines():
        line = raw.strip()
        if in_comment:
            if "-->" in line:
                in_comment = False
            continue
        if line.startswith("<!--"):
            if "-->" not in line:
                in_comment = True
            continue
        if line.startswith("#"):
            continue
        if line:
            out.append(line)
    return "\n".join(out).strip()


def spawn_editor(path: Path) -> bool:
    """Run `$EDITOR` on `path`. Returns False if the editor could not run.

    The caller is responsible for suspending the TUI around this.
    """
    cmd = [*config.editor(), str(path)]
    try:
        subprocess.call(cmd)
    except (OSError, subprocess.SubprocessError):
        return False
    return True


def _run_capture(
    tmp_name: str,
    initial: str,
    dest: Path,
    is_empty,
) -> CaptureResult:
    """Shared machinery: temp file → editor → keep only if the user wrote something."""
    if not editor_available():
        return CaptureResult(False, reason="no editor available")

    with tempfile.TemporaryDirectory(prefix=f"{branding.SLUG}-") as tmpdir:
        tmp = Path(tmpdir) / tmp_name
        tmp.write_text(initial)
        if not spawn_editor(tmp):
            return CaptureResult(False, reason="editor failed to launch")
        try:
            content = tmp.read_text()
        except OSError:
            return CaptureResult(False, reason="skipped")

        # `:q!` leaves the buffer byte-identical to the template.
        if content == initial or is_empty(content):
            return CaptureResult(False, reason="skipped")

        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(content)
    return CaptureResult(True, path=dest)


def capture_solution(
    problem: Problem,
    attempt: dict,
    attempt_id: int,
    language: str | None = None,
) -> CaptureResult:
    """Step 1 — archive the solution to `code/<slug>/<attempt_id>.<ext>`."""
    cfg = config.load()
    lang = language or cfg.capture.language
    ext = config.EXT_BY_LANGUAGE.get(lang.lower(), "txt")
    prefix = COMMENT_PREFIX.get(ext, "#")
    header = solution_header(problem, attempt, ext)
    dest = paths.code_path(problem.slug, attempt_id, ext)
    return _run_capture(
        f"{problem.slug}.{ext}",
        header,
        dest,
        lambda text: not _strip_comments(text, prefix),
    )


def capture_submission(
    problem: Problem,
    attempt: dict,
    attempt_id: int,
    n: int,
    language: str | None = None,
) -> CaptureResult:
    """The wrong answer behind failed submit `n`, mid-attempt.

    Same bargain as every other capture step: pre-filled header, `:q!` skips,
    skipping costs nothing. A rejected submission is the only artifact of the
    attempt that stops existing the moment you edit the buffer it came from, so
    it is worth one keystroke to keep — the diff against what finally passed is
    the whole lesson.
    """
    cfg = config.load()
    lang = language or cfg.capture.language
    ext = config.EXT_BY_LANGUAGE.get(lang.lower(), "txt")
    prefix = COMMENT_PREFIX.get(ext, "#")
    header = submission_header(problem, attempt, ext, n)
    dest = paths.submission_path(problem.slug, attempt_id, n, ext)
    return _run_capture(
        f"{problem.slug}-wrong{n}.{ext}",
        header,
        dest,
        lambda text: not _strip_comments(text, prefix),
    )


def capture_note(problem: Problem, attempt_id: int) -> CaptureResult:
    """Step 2 — the reflection note, `notes/<slug>/<attempt_id>.md`."""
    template = note_template(problem.slug)
    dest = paths.note_path(problem.slug, attempt_id)
    return _run_capture(
        f"{problem.slug}.md",
        template,
        dest,
        lambda text: not _strip_note_scaffolding(text),
    )


SESSION_NOTE_TEMPLATE = f"""\
<!-- {branding.NAME} session reflection -->
<!-- optional. :q! to skip. energy, focus, anything environmental. -->

"""


def capture_session_note() -> str | None:
    """The end-of-run buffer. Stored on `sessions.session_note`, not on disk."""
    if not editor_available():
        return None
    with tempfile.TemporaryDirectory(prefix=f"{branding.SLUG}-") as tmpdir:
        tmp = Path(tmpdir) / "session.md"
        tmp.write_text(SESSION_NOTE_TEMPLATE)
        if not spawn_editor(tmp):
            return None
        try:
            content = tmp.read_text()
        except OSError:
            return None
        if content == SESSION_NOTE_TEMPLATE:
            return None
        body = _strip_note_scaffolding(content)
        return body or None


ENV_BROWSER = branding.env("BROWSER")


def open_url(url: str) -> bool:
    """Open the problem in the browser (spec §1: you solve in the browser)."""
    opener = os.environ.get(ENV_BROWSER) or ("open" if os.uname().sysname == "Darwin" else "xdg-open")
    if shutil.which(opener) is None:
        return False
    try:
        subprocess.Popen(
            [opener, url],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return True
