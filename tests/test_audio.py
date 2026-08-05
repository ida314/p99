"""The speech-mode recorder.

Almost everything here drives a fake `ffmpeg` — a shell script pointed at by
`P99_FFMPEG`, the same trick the capture tests use for `$EDITOR`. That keeps the
suite off the microphone and makes the segment bookkeeping assertable.

One test at the bottom runs the real encoder against a synthetic source, because
the fake cannot tell you whether the concat produces a file anything can play.
"""

from __future__ import annotations

import shutil
import subprocess

import pytest

from core import audio, branding, paths

# Writes a non-empty file to the destination — the last argument, for a segment
# and for the join alike — then blocks on stdin until the polite `q` arrives, so
# the pause/resume handshake is the real one. On the join, stdin is /dev/null and
# the read returns at once.
FAKE_FFMPEG = """#!/bin/sh
for arg in "$@"; do dest="$arg"; done
printf 'segment' > "$dest"
read -r line
exit 0
"""


@pytest.fixture
def fake_ffmpeg(isolated_home, monkeypatch):
    script = isolated_home / "fake-ffmpeg"
    script.write_text(FAKE_FFMPEG)
    script.chmod(0o755)
    monkeypatch.setenv(branding.env("FFMPEG"), str(script))
    paths.ensure_dirs()
    return script


def test_availability_follows_the_binary(fake_ffmpeg, monkeypatch):
    assert audio.available() is True
    monkeypatch.setenv(branding.env("FFMPEG"), "/nonexistent/ffmpeg")
    assert audio.available() is False


def test_a_missing_recorder_fails_without_raising(isolated_home, monkeypatch):
    monkeypatch.setenv(branding.env("FFMPEG"), "/nonexistent/ffmpeg")
    paths.ensure_dirs()
    recorder = audio.Recorder("two-sum", 1)
    assert recorder.start() is False
    assert recorder.recording is False
    # And stopping something that never started is a no-op, not an error.
    assert recorder.stop() is None


def test_pause_and_resume_open_one_segment_each(fake_ffmpeg):
    recorder = audio.Recorder("two-sum", 1)
    assert recorder.start() is True
    recorder.pause()
    assert recorder.paused is True
    assert recorder.recording is False
    recorder.resume()
    recorder.pause()
    recorder.resume()
    # start + two resumes
    assert len(recorder._segments) == 3
    recorder.discard()


def test_stop_lands_the_file_where_the_attempt_can_find_it(fake_ffmpeg):
    recorder = audio.Recorder("two-sum", 7)
    recorder.start()
    path = recorder.stop()
    assert path == paths.audio_path("two-sum", 7)
    assert path.exists()
    # The working directory is gone: a finished recording is one file.
    assert not recorder.segment_dir.exists()


def test_stop_joins_several_segments_into_one_file(fake_ffmpeg):
    recorder = audio.Recorder("two-sum", 2)
    recorder.start()
    recorder.pause()
    recorder.resume()
    path = recorder.stop()
    assert path is not None and path.exists()
    assert not recorder.segment_dir.exists()


def test_stopping_twice_does_not_produce_a_second_file(fake_ffmpeg):
    recorder = audio.Recorder("two-sum", 3)
    recorder.start()
    assert recorder.stop() is not None
    assert recorder.stop() is None
    # Nothing claimed a `-2` suffix on the way through.
    assert not paths.audio_path("two-sum", 3).with_name("3-2.opus").exists()


def test_discard_leaves_nothing_behind(fake_ffmpeg):
    recorder = audio.Recorder("two-sum", 4)
    recorder.start()
    recorder.pause()
    recorder.resume()
    recorder.discard()
    assert not recorder.segment_dir.exists()
    assert not paths.audio_path("two-sum", 4).exists()
    # And a discarded recorder cannot be talked into producing a file later.
    assert recorder.stop() is None


def test_an_existing_file_is_stepped_around_rather_than_overwritten(fake_ffmpeg):
    """Attempt ids renumber after a run deletion — see `paths.unclaimed`."""
    taken = paths.audio_path("two-sum", 5)
    taken.parent.mkdir(parents=True, exist_ok=True)
    taken.write_text("an earlier attempt that got this id first")

    recorder = audio.Recorder("two-sum", 5)
    recorder.start()
    path = recorder.stop()
    assert path is not None and path != taken
    assert taken.read_text() == "an earlier attempt that got this id first"


def test_a_recorder_that_dies_mid_segment_does_not_raise(fake_ffmpeg, isolated_home, monkeypatch):
    dying = isolated_home / "dying-ffmpeg"
    dying.write_text("#!/bin/sh\nexit 1\n")
    dying.chmod(0o755)
    monkeypatch.setenv(branding.env("FFMPEG"), str(dying))

    recorder = audio.Recorder("two-sum", 6)
    # Popen succeeds — the failure is the exit code, which is what a missing
    # audio device actually looks like.
    assert recorder.start() is True
    # Nothing was written, so there is nothing to keep, and saying so is the
    # whole contract: the caller toasts and the attempt is unaffected.
    assert recorder.stop() is None


def test_the_command_carries_the_configured_bitrate_and_device(fake_ffmpeg):
    recorder = audio.Recorder(
        "two-sum", 1, bitrate_kbps=12, input_format="alsa", device="hw:0"
    )
    cmd = recorder._command(paths.audio_path("two-sum", 1))
    assert "-b:a" in cmd and cmd[cmd.index("-b:a") + 1] == "12k"
    assert cmd[cmd.index("-f") + 1] == "alsa"
    assert cmd[cmd.index("-i") + 1] == "hw:0"
    # Constrained VBR is what makes the configured bitrate a ceiling.
    assert cmd[cmd.index("-vbr") + 1] == "constrained"
    assert cmd[cmd.index("-ac") + 1] == "1"


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs a real ffmpeg")
def test_real_ffmpeg_produces_a_playable_joined_recording(isolated_home, monkeypatch):
    """The one end-to-end pass: real encoder, real container, no microphone.

    `lavfi` is a synthetic source, so this touches no audio hardware and is safe
    on a headless machine.
    """
    monkeypatch.delenv(branding.env("FFMPEG"), raising=False)
    paths.ensure_dirs()

    recorder = audio.Recorder(
        "two-sum", 1, bitrate_kbps=24, input_format="lavfi", device="sine=frequency=440"
    )
    assert recorder.start() is True
    recorder.pause()
    recorder.resume()
    path = recorder.stop()
    assert path is not None and path.exists()

    probe = subprocess.run(
        [
            "ffprobe", "-hide_banner", "-v", "error",
            "-show_entries", "stream=codec_name,channels",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert probe.returncode == 0, probe.stderr
    assert probe.stdout.split() == ["opus", "1"], probe.stdout


def test_the_binary_is_overridable_and_defaults_to_ffmpeg(monkeypatch):
    monkeypatch.delenv(branding.env("FFMPEG"), raising=False)
    assert audio.ffmpeg() == "ffmpeg"
    monkeypatch.setenv(branding.env("FFMPEG"), "/opt/ffmpeg")
    assert audio.ffmpeg() == "/opt/ffmpeg"
