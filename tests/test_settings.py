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
