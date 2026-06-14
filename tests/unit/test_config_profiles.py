"""
Tests for config_profiles, config, and expiry_detector modules.
Ensures the auto-switching logic works correctly without breaking existing settings.
"""
import pytest
from unittest.mock import patch, MagicMock
from dataclasses import fields as dc_fields
from datetime import date
from config import Settings, Secrets
from config_profiles import TuningConfig, EXPIRY_CONFIG, NON_EXPIRY_CONFIG
from detectors import expiry_detector


# ─── TuningConfig Dataclass Tests ────────────────────────────────────────────

class TestTuningConfig:
    """Tests that the TuningConfig dataclass and both profiles are well-formed."""

    def test_both_profiles_are_tuning_config_instances(self):
        assert isinstance(EXPIRY_CONFIG, TuningConfig)
        assert isinstance(NON_EXPIRY_CONFIG, TuningConfig)

    def test_profiles_have_different_values(self):
        """At least some values should differ between the two profiles."""
        differences = 0
        for f in dc_fields(TuningConfig):
            if getattr(EXPIRY_CONFIG, f.name) != getattr(NON_EXPIRY_CONFIG, f.name):
                differences += 1
        assert differences > 0, "Profiles should have at least some different values"

    def test_profiles_share_unchanged_fields(self):
        """Fields not tuned for expiry should be identical in both profiles."""
        assert EXPIRY_CONFIG.poll_interval_seconds == NON_EXPIRY_CONFIG.poll_interval_seconds
        assert EXPIRY_CONFIG.candle_buffer_size == NON_EXPIRY_CONFIG.candle_buffer_size
        assert EXPIRY_CONFIG.strike_interval == NON_EXPIRY_CONFIG.strike_interval
        assert EXPIRY_CONFIG.entry_zone_offset_pts == NON_EXPIRY_CONFIG.entry_zone_offset_pts

    def test_expiry_profile_values(self):
        """Spot-check specific expiry profile values."""
        assert EXPIRY_CONFIG.signal_cooldown_minutes == 20
        assert EXPIRY_CONFIG.breakout_confirmation_candles == 2
        assert EXPIRY_CONFIG.oi_wall_min_oi == 10_000_000
        assert EXPIRY_CONFIG.target_1_pts == 25.0
        assert EXPIRY_CONFIG.target_2_pts == 50.0
        assert EXPIRY_CONFIG.level_scan_range == 300.0

    def test_non_expiry_profile_values(self):
        """Spot-check specific non-expiry profile values."""
        assert NON_EXPIRY_CONFIG.signal_cooldown_minutes == 15
        assert NON_EXPIRY_CONFIG.breakout_confirmation_candles == 3
        assert NON_EXPIRY_CONFIG.oi_wall_min_oi == 4_000_000
        assert NON_EXPIRY_CONFIG.target_1_pts == 35.0
        assert NON_EXPIRY_CONFIG.target_2_pts == 70.0
        assert NON_EXPIRY_CONFIG.level_scan_range == 500.0


# ─── Settings Facade Tests ──────────────────────────────────────────────────

class TestSettings:
    """Tests the unified Settings facade that combines Secrets + TuningConfig."""

    def test_settings_exposes_secrets(self):
        """settings.xxx should work for secret fields."""
        s = Settings()
        assert s.dhan_client_id  # loaded from env
        assert s.dhan_access_token
        assert s.discord_webhook_url

    def test_settings_exposes_tuning(self):
        """settings.xxx should work for tuning fields."""
        s = Settings()
        assert isinstance(s.signal_cooldown_minutes, int)
        assert isinstance(s.target_1_pts, float)
        assert isinstance(s.oi_wall_min_oi, int)

    def test_apply_profile_changes_tuning(self):
        """apply_profile should swap the tuning config."""
        s = Settings()

        s.apply_profile(EXPIRY_CONFIG)
        assert s.signal_cooldown_minutes == 20
        assert s.target_1_pts == 25.0
        assert s.oi_wall_min_oi == 10_000_000

        s.apply_profile(NON_EXPIRY_CONFIG)
        assert s.signal_cooldown_minutes == 15
        assert s.target_1_pts == 35.0
        assert s.oi_wall_min_oi == 4_000_000

    def test_apply_profile_preserves_secrets(self):
        """Swapping the tuning profile must NOT affect secrets."""
        s = Settings()
        original_client_id = s.dhan_client_id
        original_webhook = s.discord_webhook_url

        s.apply_profile(EXPIRY_CONFIG)

        assert s.dhan_client_id == original_client_id
        assert s.discord_webhook_url == original_webhook

    def test_unknown_attribute_raises(self):
        """Accessing a non-existent attribute should raise AttributeError."""
        s = Settings()
        with pytest.raises(AttributeError):
            _ = s.totally_fake_attribute


# ─── Expiry Detector Tests ──────────────────────────────────────────────────

class TestExpiryDetector:
    """Tests for expiry day detection logic."""

    def test_simple_tuesday_is_expiry(self):
        with patch.object(expiry_detector, "_today_ist", return_value=date(2026, 6, 16)):
            assert expiry_detector.is_expiry_day_simple() is True

    def test_simple_monday_is_not_expiry(self):
        with patch.object(expiry_detector, "_today_ist", return_value=date(2026, 6, 15)):
            assert expiry_detector.is_expiry_day_simple() is False

    def test_simple_wednesday_is_not_expiry(self):
        with patch.object(expiry_detector, "_today_ist", return_value=date(2026, 6, 17)):
            assert expiry_detector.is_expiry_day_simple() is False

    def test_simple_friday_is_not_expiry(self):
        with patch.object(expiry_detector, "_today_ist", return_value=date(2026, 6, 19)):
            assert expiry_detector.is_expiry_day_simple() is False

    def test_simple_thursday_is_not_expiry(self):
        with patch.object(expiry_detector, "_today_ist", return_value=date(2026, 6, 18)):
            assert expiry_detector.is_expiry_day_simple() is False

    @pytest.mark.asyncio
    async def test_api_fallback_on_tuesday(self):
        """When API fails on Tuesday, fallback returns True."""
        with patch.object(expiry_detector, "_today_ist", return_value=date(2026, 6, 16)):
            result = await expiry_detector.is_expiry_day_from_api()
            assert result is True

    @pytest.mark.asyncio
    async def test_api_fallback_on_wednesday(self):
        """When API fails on Wednesday, fallback returns False."""
        with patch.object(expiry_detector, "_today_ist", return_value=date(2026, 6, 17)):
            result = await expiry_detector.is_expiry_day_from_api()
            assert result is False
