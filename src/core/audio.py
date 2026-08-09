"""Speech mode: record what you say while a problem is running.

Two of the four hint tiers already tell you to say the answer out loud, and the
thing an interview actually judges is how you talk through a problem — none of
which the record has ever held. This captures it, one file per attempt, paused
exactly when the clock is paused so the recording is your solve and nothing
else. Nothing transcribes it and nothing scores it; the artifact is the point.

Shaped like `capture`: `ffmpeg` is spawned, never linked against, every failure
comes back as a return value rather than an exception, and no failure here is
ever allowed to cost an attempt that was already recorded.

The one structural decision worth explaining is that **a pause stops the
current segment and a resume starts a new one**, rather than suspending the
process. A stopped `ffmpeg` on a live audio input keeps being handed samples it
never reads; what comes back after a resume is an overrun, not a continuation.
Separate segments joined at the end are clean, and the seam is exactly the
pause — which is what a pause was supposed to remove from the recording anyway.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from . import branding, paths

ENV_FFMPEG = branding.env("FFMPEG")

#: How long to wait for a segment to finalize itself after being asked nicely.
#: Ogg needs its last page written, so the polite exit matters; three seconds is
#: far more than closing a voice stream takes and still short enough that a hung
#: encoder cannot hold up the finish prompt.
FINALIZE_TIMEOUT = 3.0


def ffmpeg() -> str:
    """The recorder binary. Overridable so tests never need a microphone."""
    return os.environ.get(ENV_FFMPEG) or "ffmpeg"


def available() -> bool:
    return shutil.which(ffmpeg()) is not None


class Recorder:
    """One attempt's recording, accumulated as segments and joined at the end.

    Every method is safe to call in any order and none of them raise: a recorder
    that could throw would have to be wrapped at seven call sites in the run
    loop, and one missed wrap would cost a solve.
    """

    def __init__(
        self,
        slug: str,
        attempt_id: int,
        *,
        bitrate_kbps: int = 24,
        input_format: str = "pulse",
        device: str = "default",
    ) -> None:
        self.slug = slug
        self.attempt_id = attempt_id
        self.bitrate_kbps = bitrate_kbps
        self.input_format = input_format
        self.device = device
        self.segment_dir = paths.audio_segments(slug, attempt_id)
        self._proc: subprocess.Popen | None = None
        self._segments: list[Path] = []
        self._stopped = False

    # --- state ------------------------------------------------------------

    @property
    def recording(self) -> bool:
        """Is a segment being written right now?"""
        return self._proc is not None and self._proc.poll() is None

    @property
    def paused(self) -> bool:
        return not self._stopped and not self.recording and bool(self._segments)

    # --- lifecycle --------------------------------------------------------

    def start(self) -> bool:
        """Open the first segment. False if it could not launch."""
        if self._stopped or self.recording:
            return self.recording
        return self._open_segment()

    def adopt(self) -> int:
        """Take over the segments an earlier process left behind. Returns how many.

        For resuming a suspended run. The segment directory is keyed on
        `(slug, attempt_id)` and only `stop`/`discard` ever clear it, so the
        pieces of the first half are sitting exactly where a fresh recorder for
        the same attempt would look. Seeding `_segments` is enough: the numbering
        in `_open_segment` counts off it, so the next segment lands after the
        last one rather than on top of it.

        Leaves the recorder closed, in the paused state a resume comes back in.
        """
        if self._stopped or self.recording:
            return len(self._segments)
        try:
            self._segments = sorted(self.segment_dir.glob("*.opus"))
        except OSError:
            self._segments = []
        return len(self._segments)

    def pause(self) -> None:
        self._close_segment()

    def resume(self) -> bool:
        if self._stopped or self.recording:
            return self.recording
        return self._open_segment()

    def stop(self) -> Path | None:
        """Finalize and join. Returns the finished file, or None if there is none.

        A failed join is not a failed recording: the segments are left where
        they are and the caller says so, because half a recording on disk beats
        a tidy directory.
        """
        if self._stopped:
            return None
        self._close_segment()
        self._stopped = True

        kept = [s for s in self._segments if s.exists() and s.stat().st_size > 0]
        if not kept:
            self._cleanup()
            return None

        dest = paths.unclaimed(paths.audio_path(self.slug, self.attempt_id))
        dest.parent.mkdir(parents=True, exist_ok=True)
        if len(kept) == 1:
            try:
                shutil.move(str(kept[0]), dest)
            except OSError:
                return None
            self._cleanup()
            return dest

        if not self._join(kept, dest):
            return None
        self._cleanup()
        return dest

    def discard(self) -> None:
        """Finalize and delete everything, leaving no orphan on disk.

        The one place this departs from how `events._forget_attempts` treats
        archived code and notes, which it deliberately leaves alone. Those are
        your writing. A recording of the wrong problem opened by mistake is not,
        and once the attempt is forgotten nothing would ever point at it again.
        """
        self._close_segment()
        self._stopped = True
        self._cleanup()

    # --- the subprocess ---------------------------------------------------

    def _command(self, dest: Path) -> list[str]:
        return [
            ffmpeg(),
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            self.input_format,
            "-i",
            self.device,
            "-ac",
            "1",
            "-c:a",
            "libopus",
            "-b:a",
            f"{max(1, int(self.bitrate_kbps))}k",
            # Tuned for a single voice rather than music: this is the setting
            # that keeps speech intelligible at a bitrate this low.
            "-application",
            "voip",
            # Constrained VBR, so the configured bitrate is a ceiling instead of
            # a hope. Measured over a minute of pink noise the two modes are
            # indistinguishable (23.4 kbps either way at a 24k target), but on a
            # pathological input -- a sustained full-scale tone -- plain VBR ran
            # to 38.3 kbps while constrained held 24.9. The size a user was
            # promised should not depend on what the microphone picked up.
            "-vbr",
            "constrained",
            "-vn",
            "-y",
            str(dest),
        ]

    def _open_segment(self) -> bool:
        dest = self.segment_dir / f"{len(self._segments) + 1:03d}.opus"
        try:
            self.segment_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            return False
        try:
            self._proc = subprocess.Popen(
                self._command(dest),
                # stdin is a pipe so the polite `q` has somewhere to go, and so
                # the encoder can never read the terminal Textual is about to
                # hand to $EDITOR. A new session keeps the TUI's ctrl-c off it.
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except (OSError, subprocess.SubprocessError):
            self._proc = None
            return False
        self._segments.append(dest)
        return True

    def _close_segment(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None:
            return
        try:
            if proc.poll() is None:
                # `q` on stdin is ffmpeg's documented graceful quit and is what
                # writes Ogg's trailing page. Signals are the fallback, and a
                # kill is the last resort: it costs the tail of the segment.
                try:
                    if proc.stdin is not None:
                        proc.stdin.write(b"q\n")
                        proc.stdin.flush()
                except (OSError, ValueError):
                    pass
                try:
                    proc.wait(timeout=FINALIZE_TIMEOUT)
                except subprocess.TimeoutExpired:
                    proc.terminate()
                    try:
                        proc.wait(timeout=FINALIZE_TIMEOUT)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait(timeout=FINALIZE_TIMEOUT)
        except (OSError, subprocess.SubprocessError):
            pass
        finally:
            for stream in (proc.stdin, proc.stdout, proc.stderr):
                if stream is not None:
                    try:
                        stream.close()
                    except OSError:
                        pass

    def _join(self, segments: list[Path], dest: Path) -> bool:
        """Concatenate the segments into `dest` without re-encoding.

        A stream copy is valid here because every segment was encoded by the
        same command with the same parameters — which is the other reason a
        segment is a whole `ffmpeg` invocation rather than a resumed one.
        """
        listing = self.segment_dir / "segments.txt"
        try:
            listing.write_text(
                "".join(f"file '{s.name}'\n" for s in segments)
            )
        except OSError:
            return False
        try:
            result = subprocess.run(
                [
                    ffmpeg(),
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-f",
                    "concat",
                    "-safe",
                    "0",
                    "-i",
                    str(listing),
                    "-c",
                    "copy",
                    "-y",
                    str(dest),
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=60,
            )
        except (OSError, subprocess.SubprocessError):
            return False
        return result.returncode == 0 and dest.exists() and dest.stat().st_size > 0

    def _cleanup(self) -> None:
        shutil.rmtree(self.segment_dir, ignore_errors=True)
