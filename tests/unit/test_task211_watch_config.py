"""Ready-watch tuning belongs to config files, never the credentials environment."""
from dataclasses import replace

import pytest

import config
from config_profiles import OI_WATCH_JEV_CONFIG
from system_one import watch_consumer


def test_application_and_worker_use_the_same_file_configuration(monkeypatch):
    monkeypatch.setenv("OI_WATCH_JEV_ENABLED", "false")
    monkeypatch.setenv("OI_WATCH_JEV_MINIMUM_MOVE_POINTS", "999")
    assert config.OI_WATCH_JEV_CONFIG is watch_consumer.OI_WATCH_JEV_CONFIG
    settings = config.Settings()
    assert settings.oi_watch_jev_enabled is True
    assert settings.oi_watch_jev_minimum_move_points == 25.0
    assert settings.oi_watch_jev_wait_seconds == 8.0
    assert settings.oi_watch_jev_max_age_seconds == 60.0
    assert not any(name.startswith("oi_watch_jev") for name in config.Secrets.model_fields)


@pytest.mark.parametrize("name,value", [
    ("oi_watch_jev_minimum_move_points", 0),
    ("oi_watch_jev_minimum_move_points", float("nan")),
    ("oi_watch_jev_minimum_move_points", True),
    ("oi_watch_jev_wait_seconds", -1),
    ("oi_watch_jev_wait_seconds", float("inf")),
    ("oi_watch_jev_max_age_seconds", 61),
    ("oi_watch_jev_max_age_seconds", 0),
    ("oi_watch_jev_enabled", "false"),
])
def test_file_configuration_rejects_invalid_values(name, value):
    with pytest.raises(ValueError):
        replace(OI_WATCH_JEV_CONFIG, **{name: value})


def test_configuration_can_disable_both_paths_without_env(monkeypatch):
    disabled = replace(OI_WATCH_JEV_CONFIG, oi_watch_jev_enabled=False)
    monkeypatch.setattr(config, "OI_WATCH_JEV_CONFIG", disabled)
    monkeypatch.setattr(watch_consumer, "OI_WATCH_JEV_CONFIG", disabled)
    assert config.Settings().oi_watch_jev_enabled is False
    assert watch_consumer.start_watch_workers(None, lambda: pytest.fail("client created")) == []
