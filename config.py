from pydantic_settings import BaseSettings, SettingsConfigDict
from config_profiles import TuningConfig, NON_EXPIRY_CONFIG


class Secrets(BaseSettings):
    """
    Credentials and API keys only.
    Loaded from .env file or environment variables (e.g., fly.io secrets).
    """
    security_id: str = "13"
    exchange_segment: str = "IDX_I"

    discord_webhook_url: str
    discord_health_webhook_url: str | None = None
    # Separate channel for observation-only (alert_only) signals, so they
    # don't spam the main trading channel. Falls back to discord_webhook_url
    # when unset (TASK-178).
    discord_observation_webhook_url: str | None = None

    supabase_url: str
    supabase_key: str

    model_config = SettingsConfigDict(
        env_file='.env', env_file_encoding='utf-8', extra='ignore'
    )


class Settings:
    """
    Unified access point for all ARES configuration.

    Combines two sources:
      - Secrets  → loaded from .env / environment variables
      - TuningConfig → loaded from config_profiles.py (expiry or non-expiry)

    All existing code can keep using `settings.xxx` unchanged.
    """

    def __init__(self):
        self._secrets = Secrets()
        self._tuning: TuningConfig = NON_EXPIRY_CONFIG  # default until apply_profile() is called
        self.dhan_client_id: str = ""
        self.dhan_access_token: str = ""

    def apply_profile(self, tuning: TuningConfig) -> None:
        """Swap the active tuning profile (called once at startup)."""
        self._tuning = tuning

    def __getattr__(self, name: str):
        # Called only when normal attribute lookup fails (i.e., not _secrets/_tuning).
        # Delegate to secrets first, then tuning config.
        try:
            return getattr(self._secrets, name)
        except AttributeError:
            pass
        try:
            return getattr(self._tuning, name)
        except AttributeError:
            raise AttributeError(f"Settings has no attribute '{name}'")


# Singleton — importable as `from config import settings`
settings = Settings()
