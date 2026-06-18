"""
ARES Tuning Configuration Profiles

Two complete profiles: EXPIRY and NON_EXPIRY.
Selected automatically at startup based on whether today is an expiry day.
Secrets (API keys, webhooks) are NOT here — they live in .env only.
"""

from dataclasses import dataclass


@dataclass
class TuningConfig:
    """
    All tunable trading parameters.
    Each profile is a full instance of this class — no partial overrides.
    """

    # Asset
    instrument_type: str = "INDEX"
    yahoo_symbol: str = "^NSEI"

    # Engine
    poll_interval_seconds: int = 60
    signal_cooldown_minutes: int = 15
    candle_buffer_size: int = 30
    iv_buffer_size: int = 10

    # Failed Breakout Detector
    breakout_confirmation_candles: int = 3
    breakout_failure_min_score: int = 2
    breakout_weak_volume_ratio: float = 0.75
    breakout_iv_falling_threshold: float = -3.0
    breakout_stop_buffer: float = 25.0
    breakout_resistance_proximity: float = 20.0

    # OI Wall Detector
    oi_wall_min_oi: int = 4000000
    oi_wall_min_oi_change_pct: float = 5.0
    oi_wall_approach_distance: float = 80.0
    oi_wall_test_distance: float = 20.0
    oi_wall_stop_buffer: float = 25.0

    # Exhaustion Detector
    exhaustion_volume_multiplier: float = 2.5
    exhaustion_body_ratio: float = 0.35
    exhaustion_iv_spike_threshold: float = 3.0
    exhaustion_min_candles: int = 6
    exhaustion_stop_buffer: float = 20.0

    # Targets & Zones
    target_1_pts: float = 35.0
    target_2_pts: float = 70.0
    strike_interval: int = 50
    entry_zone_offset_pts: float = 5.0
    level_scan_range: float = 500.0


# ─── The two profiles ────────────────────────────────────────────────────────

NON_EXPIRY_CONFIG = TuningConfig(
    signal_cooldown_minutes=15,
    breakout_confirmation_candles=3,
    breakout_weak_volume_ratio=0.75,
    breakout_iv_falling_threshold=-3.0,
    breakout_stop_buffer=25.0,
    oi_wall_min_oi=4000000,
    oi_wall_min_oi_change_pct=5.0,
    oi_wall_approach_distance=80.0,
    oi_wall_test_distance=20.0,
    oi_wall_stop_buffer=25.0,
    exhaustion_volume_multiplier=2.5,
    exhaustion_body_ratio=0.35,
    exhaustion_iv_spike_threshold=3.0,
    target_1_pts=35.0,
    target_2_pts=70.0,
    level_scan_range=500.0,
)

EXPIRY_CONFIG = TuningConfig(
    signal_cooldown_minutes=20,

    # Breakout — faster confirmation, stricter filters, tighter stops
    breakout_confirmation_candles=2,
    breakout_weak_volume_ratio=0.70,
    breakout_iv_falling_threshold=-5.0,
    breakout_stop_buffer=15.0,

    # OI Wall — only massive walls matter, tighter proximity
    oi_wall_min_oi=10000000,
    oi_wall_min_oi_change_pct=15.0,
    oi_wall_approach_distance=50.0,
    oi_wall_test_distance=10.0,
    oi_wall_stop_buffer=15.0,

    # Exhaustion — higher bar
    exhaustion_volume_multiplier=3.5,
    exhaustion_body_ratio=0.30,
    exhaustion_iv_spike_threshold=6.0,

    # Targets — smaller (faster moves on expiry)
    target_1_pts=25.0,
    target_2_pts=50.0,
    level_scan_range=300.0,
)
