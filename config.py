from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    """
    Configuration for the ARES Trading System.
    Loads values from a .env file automatically.
    """
    
    # Dhan API
    dhan_client_id: str
    dhan_access_token: str
    # Asset
    security_id: str = "13"
    exchange_segment: str = "IDX_I"
    instrument_type: str = "INDEX"
    yahoo_symbol: str = "^NSEI"

    # Discord
    discord_webhook_url: str

    # Supabase
    supabase_url: str
    supabase_key: str

    # Engine
    poll_interval_seconds: int = 60
    signal_cooldown_minutes: int = 5
    candle_buffer_size: int = 30
    iv_buffer_size: int = 10

    # Failed Breakout Detector
    breakout_confirmation_candles: int = 3
    breakout_failure_min_score: int = 2
    breakout_weak_volume_ratio: float = 0.8
    breakout_iv_falling_threshold: float = -3.0
    breakout_stop_buffer: float = 30.0
    breakout_resistance_proximity: float = 20.0

    # OI Wall Detector
    oi_wall_min_oi: int = 5000000  # 50 lakh minimum OI to be considered a wall
    oi_wall_min_oi_change_pct: float = 20.0
    oi_wall_approach_distance: float = 40.0
    oi_wall_test_distance: float = 20.0

    # Exhaustion Detector
    exhaustion_volume_multiplier: float = 2.5
    exhaustion_body_ratio: float = 0.3
    exhaustion_iv_spike_threshold: float = 1.0
    exhaustion_min_candles: int = 5

    # Targets
    target_1_pts: float = 40.0
    target_2_pts: float = 80.0
    strike_interval: int = 50
    entry_zone_offset_pts: float = 5.0
    oi_wall_stop_buffer: float = 10.0
    exhaustion_stop_buffer: float = 20.0
    level_scan_range: float = 500.0

    model_config = SettingsConfigDict(env_file='.env', env_file_encoding='utf-8', extra='ignore')

# Singleton settings instance
settings = Settings()
