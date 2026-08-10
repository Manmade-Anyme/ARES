"""
TASK-208 — Tests for Secrets default values and offline training configuration.

Ensures Secrets can be instantiated without requiring DISCORD_WEBHOOK_URL or other
secrets to be set, preventing ValidationError in headless / offline environments
like GitHub Actions ML model training.
"""
import os
from unittest.mock import patch
import pytest
from config import Secrets, Settings


def test_secrets_defaults_without_env(monkeypatch):
    """Secrets should have sensible empty defaults when env vars and .env are absent."""
    # Ensure env vars are not set
    monkeypatch.delenv("DISCORD_WEBHOOK_URL", raising=False)
    monkeypatch.delenv("DISCORD_HEALTH_WEBHOOK_URL", raising=False)
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    monkeypatch.delenv("SUPABASE_KEY", raising=False)
    monkeypatch.delenv("SECURITY_ID", raising=False)
    monkeypatch.delenv("EXCHANGE_SEGMENT", raising=False)

    # Instantiate Secrets with no .env file
    secrets = Secrets(_env_file=None)
    assert secrets.security_id == "13"
    assert secrets.exchange_segment == "IDX_I"
    assert secrets.discord_webhook_url == ""
    assert secrets.discord_health_webhook_url is None
    assert secrets.supabase_url == ""
    assert secrets.supabase_key == ""


def test_settings_instantiation_without_discord_webhook(monkeypatch):
    """Settings() should instantiate cleanly even if DISCORD_WEBHOOK_URL is missing."""
    monkeypatch.delenv("DISCORD_WEBHOOK_URL", raising=False)
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    monkeypatch.delenv("SUPABASE_KEY", raising=False)
    
    with patch("config.Secrets", return_value=Secrets(_env_file=None)):
        s = Settings()
        assert hasattr(s, "discord_webhook_url")
        assert s.discord_webhook_url == ""
        assert s.supabase_url == ""
        assert s.supabase_key == ""


def test_ml_train_offline_import_without_discord_webhook(monkeypatch):
    """Offline ML training should not fail on import when DISCORD_WEBHOOK_URL is not set."""
    monkeypatch.delenv("DISCORD_WEBHOOK_URL", raising=False)
    monkeypatch.setenv("SUPABASE_URL", "https://mock.supabase.co")
    monkeypatch.setenv("SUPABASE_KEY", "mock-key")

    import ml_signal.train_offline as train_offline
    assert hasattr(train_offline, "main")
    assert hasattr(train_offline, "run_training")
