"""
ARES Tuning Configuration Profiles

Two complete profiles: EXPIRY and NON_EXPIRY.
Selected automatically at startup based on whether today is an expiry day.
Secrets (API keys, webhooks) are NOT here — they live in .env only.
"""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class SetupLevels:
    """
    Per-setup-type SL / T1 / T2 in ABSOLUTE points from entry (TASK-185).

    These are the single source of truth for a signal's trade levels; detectors
    no longer compute SL/T1/T2. engine.apply_per_type_levels sets, per setup:
      * stop_pts / target_1_pts — fixed distances from the trigger price.
      * target_2_fallback_pts — T2 when no structural support/resistance level
        sits beyond T1; otherwise T2 is that nearest structural level.

    Re-tune monthly by editing the numbers in each profile below; no code change.
    """
    stop_pts: float
    target_1_pts: float
    target_2_fallback_pts: float


# Validated defaults (TASK-185 SL/target study, replace-semantics). Only
# EXHAUSTION_REVERSAL is well-supported (n=13); the other three (n=5/4/2) are
# indicative and expected to be re-tuned after ~1 month of live data.
#                          SL   T1   T2fb
_PER_TYPE_LEVELS_DEFAULT = {
    "EXHAUSTION_REVERSAL": SetupLevels(18.0, 24.0, 40.0),
    "TREND_CONTINUATION":  SetupLevels(32.0, 40.0, 80.0),
    "OI_WALL_REJECTION":   SetupLevels(16.0, 25.0, 40.0),
    "FAILED_BREAKOUT":     SetupLevels(15.0, 30.0, 55.0),
}


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
    # Minimum scored conditions (of 4) required alongside the mandatory
    # closed_back gate.
    #
    # TASK-184 — INTENTIONAL, NOT A BUG. Set back to 2 (from the 3 that TASK-172
    # introduced). At 3 the engine fired only HIGH-confidence breakouts (score
    # >= 60% of the matrix) and silenced every MEDIUM one (score 2/4 == 50%).
    # ARES is used as a manual-trading confirmation aid, not only an autotrader:
    # a MEDIUM breakout that agrees with an open position is useful confidence,
    # one that disagrees is a useful prompt to reconsider, and a silent detector
    # gives neither. The AresSignal already carries a `confidence` field so the
    # trader can weight MEDIUM vs HIGH themselves — the engine must not
    # pre-suppress the MEDIUM tier. closed_back remains a hard gate and the
    # score still filters pure noise (score 0-1 never fires). Do NOT raise this
    # back to 3 to "tighten precision"; see CHANGELOG TASK-184 and
    # tests/unit/test_task184_medium_breakout.py.
    breakout_failure_min_score: int = 2
    breakout_weak_volume_ratio: float = 0.75
    breakout_iv_falling_threshold: float = -3.0
    # Minimum ATM OI growth (%) for the writers_active scored condition
    # (TASK-174). Was hardcoded 3.0 — intraminute OI drift of 3-5% is common
    # noise; a scored "active defense" needs a decisive build.
    breakout_writers_active_min_pct: float = 10.0
    # Points the close must travel back past the level for the deep_close
    # scored condition (TASK-175; was hardcoded 5.0).
    breakout_deep_close_pts: float = 5.0
    # TASK-175: stop buffers removed — SL sits exactly at the structural
    # reference (breakout level / wall strike / exhaustion candle extreme).
    # The dead breakout_resistance_proximity field (never read) was dropped.

    # OI Wall Detector
    oi_wall_min_oi: int = 4000000
    oi_wall_min_oi_change_pct: float = 5.0
    oi_wall_approach_distance: float = 80.0
    oi_wall_test_distance: float = 20.0
    oi_wall_wick_rejection_ratio: float = 0.4
    # Multiplier over the min OI / min OI-change bars that upgrades wall
    # magnitude and writer defense to their "high conviction" scored variants
    # (TASK-175; was hardcoded 1.5 in both places).
    oi_wall_conviction_multiplier: float = 1.5
    # Minimum candle range (pts) before the wick-rejection scored condition is
    # evaluated — sub-range candles are all wick by noise (was hardcoded 2.0).
    oi_wall_wick_min_range_pts: float = 2.0

    # Exhaustion Detector
    exhaustion_volume_multiplier: float = 2.5
    exhaustion_body_ratio: float = 0.35
    exhaustion_iv_spike_threshold: float = 3.0
    exhaustion_min_candles: int = 6
    # Factor over the base volume-multiplier bar that upgrades a climax to the
    # "extreme volume" scored condition (TASK-175; was hardcoded 1.5).
    exhaustion_extreme_volume_factor: float = 1.5
    # Factor under the base body-ratio bar that upgrades a doji to the
    # "extreme doji" scored condition (TASK-175; was hardcoded 0.5).
    exhaustion_extreme_doji_factor: float = 0.5
    # Distance (pts) from a structural level for the level-test scored
    # condition (was hardcoded 10.0).
    exhaustion_level_proximity_pts: float = 10.0
    # Rolling volume-history window used for the climax baseline (was a
    # hardcoded deque maxlen of 20).
    exhaustion_volume_history_size: int = 20

    # Min distance from entry close for a structural level to qualify. Still
    # used by the continuation detector's scoring (opposing-level distance);
    # the per-detector target-selection use was removed with TASK-185, when
    # SL/T1/T2 moved to per_type_levels + engine.apply_per_type_levels.
    structural_target_min_distance_pts: float = 20.0

    # Trade quality gates (TASK-171 audit P0). The R:R gate is the only
    # remaining protective filter — TASK-182 removed the observation-only gate
    # (exhaustion/continuation alert-only modes), the trend-regime filter, the
    # flat-market speed filter and the anti-IV-crush filter, which between them
    # silenced the system in trending sessions by neutering tradeable setups
    # into observation-only alerts or suppressing them outright.
    min_rr_ratio: float = 1.0
    time_stop_minutes: int = 45

    # Cadence for tick-driven exit checks against the WebSocket feed between
    # the 60s REST poll cycles (TASK-173 audit item 18).
    tick_exit_check_interval_seconds: float = 2.0

    # Flat-market annotation (TASK-182 follow-up). The old speed filter used
    # these to SUPPRESS MEDIUM signals when the rolling window range fell below
    # the threshold; now they only drive an informational "Price is FLAT"
    # reason appended to the alert — the signal still trades, it is just
    # flagged so the user knows the market is consolidating.
    speed_filter_window_candles: int = 15
    speed_filter_min_range_pts: float = 15.0

    # Trend Continuation Detector (TASK-177). The only trend-aligned setup in
    # the suite — regime must persist, then a shallow pullback, then a
    # resumption candle. Runs on both profiles; continuation_enabled is a real
    # off-switch (independent of the removed observation gate, TASK-182).
    continuation_enabled: bool = True
    continuation_regime_min_candles: int = 15
    continuation_pullback_vwap_pts: float = 10.0
    continuation_pullback_max_candles: int = 10
    continuation_resume_volume_ratio: float = 1.2
    continuation_min_score: int = 2

    # Per-setup-type SL / T1 / T2 (TASK-185). Applied centrally in the engine,
    # keyed by SetupType.value. See SetupLevels above. Same table in both
    # profiles for now; expiry may diverge after a month of live re-tuning.
    per_type_levels: dict = field(
        default_factory=lambda: dict(_PER_TYPE_LEVELS_DEFAULT)
    )

    # Zones (T1/T2 distances now live per setup type in per_type_levels above)
    strike_interval: int = 50
    entry_zone_offset_pts: float = 5.0
    level_scan_range: float = 500.0

    # Options lot sizing calculations
    risk_per_trade_pct: float = 10.0
    nifty_lot_size: int = 65
    default_capital: float = 100000.0

    # Position manager — a new signal matching an open trade's setup/direction
    # with entry within this many points is treated as a duplicate (TASK-172
    # guard; tolerance was hardcoded 1.0).
    trade_dedupe_tolerance_pts: float = 1.0



# ─── The two profiles ────────────────────────────────────────────────────────

NON_EXPIRY_CONFIG = TuningConfig(
    signal_cooldown_minutes=15,
    breakout_confirmation_candles=3,
    breakout_weak_volume_ratio=0.75,
    breakout_iv_falling_threshold=-3.0,
    breakout_writers_active_min_pct=10.0,
    oi_wall_min_oi=4000000,
    oi_wall_min_oi_change_pct=5.0,
    oi_wall_approach_distance=80.0,
    oi_wall_test_distance=20.0,
    oi_wall_wick_rejection_ratio=0.4,
    exhaustion_volume_multiplier=2.5,
    exhaustion_body_ratio=0.35,
    exhaustion_iv_spike_threshold=3.0,
    per_type_levels=dict(_PER_TYPE_LEVELS_DEFAULT),
    level_scan_range=500.0,
)

EXPIRY_CONFIG = TuningConfig(
    signal_cooldown_minutes=20,

    # Trade quality gates — faster time-stop on expiry (moves die quicker)
    min_rr_ratio=1.0,
    time_stop_minutes=30,

    # Breakout — faster confirmation, stricter filters
    breakout_confirmation_candles=2,
    breakout_weak_volume_ratio=0.70,
    breakout_iv_falling_threshold=-5.0,
    breakout_writers_active_min_pct=15.0,

    # OI Wall — only massive walls matter, tighter proximity
    oi_wall_min_oi=10000000,
    oi_wall_min_oi_change_pct=15.0,
    oi_wall_approach_distance=50.0,
    oi_wall_test_distance=10.0,
    oi_wall_wick_rejection_ratio=0.4,

    # Exhaustion — higher bar
    exhaustion_volume_multiplier=3.5,
    exhaustion_body_ratio=0.30,
    exhaustion_iv_spike_threshold=6.0,

    # Per-type SL/T1/T2 — identical to NON_EXPIRY for now (TASK-185). Replace
    # this line with an explicit dict to give expiry its own tuned levels.
    per_type_levels=dict(_PER_TYPE_LEVELS_DEFAULT),
    level_scan_range=300.0,

    # Trend Continuation — runs on expiry too, using the class-default
    # continuation_enabled; only the expiry-specific speed knobs below (tuned
    # for expiry's faster candles since TASK-177) need overriding.
    continuation_regime_min_candles=10,
    continuation_pullback_vwap_pts=8.0,
    continuation_pullback_max_candles=6,
    continuation_resume_volume_ratio=1.3,
    continuation_min_score=3,
)
