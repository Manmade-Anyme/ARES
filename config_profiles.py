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
    # Minimum scored conditions (of 5) required alongside the mandatory
    # closed_back gate. TASK-172 raised 2→3 and removed closed_back from the
    # score — the old 2 effectively meant "closed back + one coin-flip".
    breakout_failure_min_score: int = 3
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
    oi_wall_wick_rejection_ratio: float = 0.4

    # Exhaustion Detector
    exhaustion_volume_multiplier: float = 2.5
    exhaustion_body_ratio: float = 0.35
    exhaustion_iv_spike_threshold: float = 3.0
    exhaustion_min_candles: int = 6
    exhaustion_stop_buffer: float = 20.0

    # Trade quality gates (TASK-171 audit P0)
    min_rr_ratio: float = 1.0
    time_stop_minutes: int = 45
    exhaustion_alert_only: bool = True

    # Engine protective filters (TASK-172 audit P1)
    # Speed filter: suppress MEDIUM signals when the rolling N-candle range is
    # below the threshold (flat market). Previously hardcoded 15 candles / 15 pts.
    speed_filter_window_candles: int = 15
    speed_filter_min_range_pts: float = 15.0
    # Anti-IV-crush filter: suppress MEDIUM signals whose option side has IV at
    # or above this percentile of the lookback. 60 samples ≈ one hour of polls
    # (the old 20-sample window flagged "high IV" off 20 minutes of data).
    iv_crush_lookback_size: int = 60
    iv_crush_percentile: float = 90.0

    # Targets & Zones
    target_1_pts: float = 35.0
    target_2_pts: float = 70.0
    strike_interval: int = 50
    entry_zone_offset_pts: float = 5.0
    level_scan_range: float = 500.0

    # Options lot sizing calculations
    risk_per_trade_pct: float = 10.0
    nifty_lot_size: int = 65
    default_capital: float = 100000.0



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
    oi_wall_wick_rejection_ratio=0.4,
    exhaustion_volume_multiplier=2.5,
    exhaustion_body_ratio=0.35,
    exhaustion_iv_spike_threshold=3.0,
    target_1_pts=35.0,
    target_2_pts=70.0,
    level_scan_range=500.0,
)

EXPIRY_CONFIG = TuningConfig(
    signal_cooldown_minutes=20,

    # Trade quality gates — faster time-stop on expiry (moves die quicker)
    min_rr_ratio=1.0,
    time_stop_minutes=30,
    exhaustion_alert_only=True,

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
    oi_wall_wick_rejection_ratio=0.4,

    # Exhaustion — higher bar
    exhaustion_volume_multiplier=3.5,
    exhaustion_body_ratio=0.30,
    exhaustion_iv_spike_threshold=6.0,

    # Targets — smaller (faster moves on expiry)
    target_1_pts=25.0,
    target_2_pts=50.0,
    level_scan_range=300.0,
)
