"""The two config layers: `config.toml` underneath, the settings table on top."""

from __future__ import annotations

from core import config, events, paths

FILE_CONFIG = """
[session]
planned_n = 5

[capture]
enabled = true
on_failed_submit = true
language = "python"
"""


def _write_file_config(text: str = FILE_CONFIG) -> None:
    paths.config_file().parent.mkdir(parents=True, exist_ok=True)
    paths.config_file().write_text(text)


def test_the_file_is_the_base_layer(conn):
    _write_file_config()
    cfg = config.load(conn)
    assert cfg.session.planned_n == 5
    assert cfg.capture.on_failed_submit is True


def test_wrong_answer_capture_can_be_turned_off_and_back_on(conn):
    _write_file_config()
    config.set_option(conn, "capture.on_failed_submit", False)
    assert config.load(conn).capture.on_failed_submit is False

    config.set_option(conn, "capture.on_failed_submit", True)
    assert config.load(conn).capture.on_failed_submit is True


def test_clearing_an_override_hands_the_knob_back_to_the_file(conn):
    _write_file_config()
    config.set_option(conn, "session.planned_n", 1)
    assert config.load(conn).session.planned_n == 1
    assert "session.planned_n" in config.overrides(conn)

    config.clear_option(conn, "session.planned_n")
    assert config.overrides(conn) == {}
    assert config.load(conn).session.planned_n == 5  # the file, untouched all along


def test_settings_never_rewrite_the_config_file(conn):
    """The file keeps its comments; overrides live in the log."""
    _write_file_config()
    before = paths.config_file().read_text()
    config.set_option(conn, "capture.enabled", False)
    assert paths.config_file().read_text() == before


def test_overrides_survive_a_replay(conn):
    _write_file_config()
    config.set_option(conn, "capture.on_failed_submit", False)
    config.set_option(conn, "session.planned_n", 2)
    config.clear_option(conn, "session.planned_n")

    events.replay(conn)

    assert config.overrides(conn) == {"capture.on_failed_submit": False}
    assert config.load(conn).session.planned_n == 5


def test_a_setting_this_version_no_longer_offers_is_ignored(conn):
    """The settings table outlives any one version of `options()`."""
    _write_file_config()
    events.append(conn, events.SETTINGS_CHANGED, {"key": "capture.telepathy", "value": True})
    events.append(conn, events.SETTINGS_CHANGED, {"key": "capture.language", "value": "cobol"})
    assert config.overrides(conn) == {}
    assert config.load(conn).capture.language == "python"


def test_load_without_a_connection_reads_only_the_file(conn):
    """Nothing outside the app has to know the second layer exists."""
    _write_file_config()
    config.set_option(conn, "session.planned_n", 1)
    assert config.load().session.planned_n == 5


def test_every_option_points_at_a_real_config_field(conn):
    cfg = config.load()
    for option in config.options():
        assert config.value_of(cfg, option) is not None
        assert option.choices


def test_stepping_wraps_and_recovers_from_an_unknown_value():
    option = config.options()[0]  # capture.enabled: (True, False)
    assert option.step(True, 1) is False
    assert option.step(False, 1) is True
    assert option.step(False, -1) is True
    assert option.step("nonsense", 1) is True  # first choice, not a crash


def test_offline_mode_round_trips_through_the_settings_layer(conn):
    """The one knob you flip without a network, so it must not need the file."""
    _write_file_config()
    assert config.load(conn).cache.offline is False

    config.set_option(conn, "cache.offline", True)
    assert config.load(conn).cache.offline is True

    config.clear_option(conn, "cache.offline")
    assert config.load(conn).cache.offline is False


def test_the_cache_budget_is_read_as_bytes(conn):
    _write_file_config(FILE_CONFIG + "\n[cache]\nmax_mb = 2\n")
    assert config.load(conn).cache.budget_bytes == 2 * 1024 * 1024


def test_speech_mode_round_trips_through_the_settings_layer(conn):
    _write_file_config()
    assert config.load(conn).audio.speech_mode is False

    config.set_option(conn, "audio.speech_mode", True)
    assert config.load(conn).audio.speech_mode is True

    config.clear_option(conn, "audio.speech_mode")
    assert config.load(conn).audio.speech_mode is False


def test_the_recording_bitrate_is_a_settings_knob(conn):
    _write_file_config()
    assert config.load(conn).audio.bitrate_kbps == 24

    config.set_option(conn, "audio.bitrate_kbps", 12)
    assert config.load(conn).audio.bitrate_kbps == 12

    # A value that is no longer on offer is ignored rather than loaded.
    config.set_option(conn, "audio.bitrate_kbps", 999)
    assert config.load(conn).audio.bitrate_kbps == 24


def test_the_queue_size_follows_planned_n_until_it_is_set(conn):
    """The two numbers were one, so a config written before the split still works.

    Taking the dataclass default for `queue_n` there would quietly shrink a
    queue somebody had already sized by setting `planned_n`.
    """
    _write_file_config()  # planned_n = 5, no queue_n
    cfg = config.load(conn)
    assert cfg.session.planned_n == 5
    assert cfg.session.queue_n == 5


def test_the_queue_size_and_the_run_size_are_separate_knobs(conn):
    _write_file_config(FILE_CONFIG + "\n[stats]\nmin_samples = 3\n")
    config.set_option(conn, "session.queue_n", 2)
    cfg = config.load(conn)
    assert cfg.session.queue_n == 2
    assert cfg.session.planned_n == 5  # untouched by its neighbour

    config.set_option(conn, "session.planned_n", 1)
    cfg = config.load(conn)
    assert cfg.session.planned_n == 1
    assert cfg.session.queue_n == 2  # and once set, it stops following

    config.clear_option(conn, "session.queue_n")
    assert config.load(conn).session.queue_n == 1  # back to following the file's chain


def test_the_queue_size_is_a_settings_knob(conn):
    _write_file_config('[session]\nqueue_n = 4\n')
    assert config.load(conn).session.queue_n == 4

    config.set_option(conn, "session.queue_n", 7)
    assert config.load(conn).session.queue_n == 7

    # A value that is no longer on offer is ignored rather than loaded.
    config.set_option(conn, "session.queue_n", 999)
    assert config.load(conn).session.queue_n == 4


def test_the_queue_size_sits_beside_the_run_size(conn):
    """Two rows apart would reintroduce the confusion the split exists to end."""
    keys = [o.key for o in config.options()]
    assert keys.index("session.queue_n") == keys.index("session.planned_n") + 1


def test_the_microphone_is_a_file_only_setting(conn):
    """Which device to record from is a fact about the machine, not a knob."""
    _write_file_config(FILE_CONFIG + '\n[audio]\ninput_format = "alsa"\ndevice = "hw:0"\n')
    cfg = config.load(conn)
    assert cfg.audio.input_format == "alsa"
    assert cfg.audio.device == "hw:0"
    assert not {o.key for o in config.options()} & {"audio.input_format", "audio.device"}


def test_new_options_go_on_the_end(conn):
    """The settings screen is navigated by row index, here and in its tests.

    Inserting a knob above an existing one silently retargets every one of them,
    so the two newest have to be the last two.
    """
    keys = [o.key for o in config.options()]
    assert keys[-3:] == ["audio.bitrate_kbps", "strategy.enabled", "capture.per_approach"]
