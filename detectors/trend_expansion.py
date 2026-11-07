"""Incremental live detector for the gated Trend Expansion POC."""

from datetime import datetime, time
from math import isfinite, sqrt
from statistics import mean, stdev
from typing import List, Optional
from zoneinfo import ZoneInfo

from models import (
    AresSignal,
    Direction,
    OHLCVCandle,
    ResistanceLevel,
    SetupType,
    confidence_from_score,
)
from config import settings


IST = ZoneInfo("Asia/Kolkata")
SESSION_START = time(9, 15)
REGIME_RANGE_END = time(9, 45)
REGIME_DISPERSION_TIME = time(10, 30)
MORNING_RANGE_END = time(10, 15)
RECLAIM_START = time(10, 30)
SESSION_END = time(15, 30)


class TrendExpansionDetector:
    """Evaluate the gated expansion setups from one completed candle at a time."""

    def __init__(self, calibration_policy=None):
        self.calibration_policy = calibration_policy
        self._session_date = None
        self._candles: list[OHLCVCandle] = []
        self._session_complete = False
        self._regime_evaluated = False
        self._regime_score = 0
        self._regime_factors: dict[str, bool] = {}
        self._cumulative_volume = 0.0
        self._cumulative_tp_volume = 0.0
        self._cumulative_tp2_volume = 0.0
        self._ema9: Optional[float] = None
        self._ema20: Optional[float] = None
        self._bbw_history: list[float] = []
        self._dipped_below_vwap = False
        self._sweep_low = float("inf")
        self._trades_today = 0

    @property
    def session_complete(self) -> bool:
        """Whether the detector has observed an unbroken session from 09:15."""
        return self._session_complete

    def update(
        self,
        candle: OHLCVCandle,
        pdc: Optional[float],
        pdl: Optional[float] = None,
        levels: Optional[List[ResistanceLevel]] = None,
        allow_signal: bool = True,
    ) -> Optional[AresSignal]:
        """Advance session state and return a qualified manual signal, if any."""
        timestamp = self._local_timestamp(candle.timestamp)
        if not SESSION_START <= timestamp.time() < SESSION_END:
            return None

        if timestamp.date() != self._session_date:
            self._reset_session(timestamp.date(), timestamp.time())
        elif self._candles:
            previous = self._local_timestamp(self._candles[-1].timestamp)
            if timestamp <= previous:
                return None
            if (timestamp - previous).total_seconds() != 60:
                self._session_complete = False

        self._candles.append(candle)
        self._update_indicators(candle)

        current_time = timestamp.time()
        if not self._regime_evaluated:
            if current_time == REGIME_DISPERSION_TIME:
                self._evaluate_regime(pdc)
            elif current_time > REGIME_DISPERSION_TIME:
                self._regime_evaluated = True
                self._regime_factors = {}

        if (
            not self._session_complete
            or not self._regime_evaluated
            or self._regime_score < settings.trend_expansion_min_regime_score
            or self._trades_today >= settings.trend_expansion_max_trades_per_day
        ):
            return None

        if (
            pdl is not None
            and candle.close < pdl - settings.trend_expansion_pdl_safety_buffer_pts
        ):
            return None

        reclaim = self._check_reclaim(candle, levels or [], allow_signal)
        if reclaim is not None:
            self._trades_today += 1
            self._dipped_below_vwap = False
            self._sweep_low = float("inf")
            return reclaim

        if not allow_signal:
            return None
        breakout = self._check_afternoon_breakout(candle, levels or [])
        if breakout is not None:
            self._trades_today += 1
        return breakout

    def _reset_session(self, session_date, first_time: time) -> None:
        self._session_date = session_date
        self._candles = []
        self._session_complete = first_time == SESSION_START
        self._regime_evaluated = False
        self._regime_score = 0
        self._regime_factors = {}
        self._cumulative_volume = 0.0
        self._cumulative_tp_volume = 0.0
        self._cumulative_tp2_volume = 0.0
        self._ema9 = None
        self._ema20 = None
        self._bbw_history = []
        self._dipped_below_vwap = False
        self._sweep_low = float("inf")
        self._trades_today = 0

    def _update_indicators(self, candle: OHLCVCandle) -> None:
        volume = max(float(candle.volume), 0.0)
        typical_price = (candle.high + candle.low + candle.close) / 3.0
        self._cumulative_volume += volume
        self._cumulative_tp_volume += typical_price * volume
        self._cumulative_tp2_volume += typical_price * typical_price * volume

        self._ema9 = self._next_ema(self._ema9, candle.close, 9)
        self._ema20 = self._next_ema(self._ema20, candle.close, 20)

        if len(self._candles) >= 20:
            closes = [item.close for item in self._candles[-20:]]
            average = mean(closes)
            deviation = stdev(closes)
            bbw = (4.0 * deviation / average * 100.0) if average else 0.0
            self._bbw_history.append(bbw)

    @staticmethod
    def _next_ema(previous: Optional[float], value: float, span: int) -> float:
        if previous is None:
            return value
        alpha = 2.0 / (span + 1.0)
        return alpha * value + (1.0 - alpha) * previous

    def _evaluate_regime(self, pdc: Optional[float]) -> None:
        self._regime_evaluated = True
        if not self._session_complete or pdc is None or not isfinite(float(pdc)):
            return

        opening = [c for c in self._candles if self._time(c) <= REGIME_RANGE_END]
        if not opening:
            return
        opening_range = max(c.high for c in opening) - min(c.low for c in opening)
        displacement = abs(opening[-1].close - self._candles[0].open)
        vwap = self._cumulative_tp_volume / self._cumulative_volume if self._cumulative_volume else 0.0
        variance = (
            self._cumulative_tp2_volume / self._cumulative_volume - vwap * vwap
            if self._cumulative_volume
            else 0.0
        )
        dispersion = sqrt(max(variance, 0.0))

        self._regime_factors = {
            "gap_extension": abs(self._candles[0].open - float(pdc))
            >= settings.trend_expansion_gap_extension_pts,
            "range_expansion": opening_range >= settings.trend_expansion_opening_range_pts,
            "vwap_dispersion": dispersion >= settings.trend_expansion_vwap_dispersion_pts,
            "directional_displacement": (
                displacement >= settings.trend_expansion_directional_displacement_pts
            ),
        }
        self._regime_score = sum(self._regime_factors.values())

    def _check_reclaim(
        self, candle: OHLCVCandle, levels: List[ResistanceLevel], allow_signal: bool
    ) -> Optional[AresSignal]:
        current_time = self._time(candle)
        if not RECLAIM_START <= current_time <= self._parse_time(settings.trend_expansion_range_anchor_end):
            return None

        morning = [c for c in self._candles if self._time(c) <= MORNING_RANGE_END]
        if not morning:
            return None
        morning_high = max(c.high for c in morning)
        morning_low = min(c.low for c in morning)
        morning_open = morning[0].open
        impulse = morning_high - morning_low
        if (
            impulse < settings.trend_expansion_min_impulse_pts
            or morning_high <= morning_open
        ):
            return None

        if candle.low < candle.vwap + settings.trend_expansion_sweep_vwap_buffer_pts:
            self._dipped_below_vwap = True
            self._sweep_low = min(self._sweep_low, candle.low)
        if not self._dipped_below_vwap:
            return None
        if self._sweep_low <= morning_low:
            self._dipped_below_vwap = False
            self._sweep_low = float("inf")
            return None

        retrace = morning_high - self._sweep_low
        if not (
            settings.trend_expansion_min_retrace_ratio * impulse
            <= retrace
            <= settings.trend_expansion_max_retrace_ratio * impulse
        ):
            return None
        if candle.close <= candle.vwap or self._ema9 < self._ema20:
            return None
        if not allow_signal:
            return None

        stop_loss = self._sweep_low - settings.trend_expansion_reclaim_stop_buffer_pts
        risk = candle.close - stop_loss
        reward = morning_high - candle.close
        if (
            risk < settings.trend_expansion_reclaim_min_stop_pts
            or risk > settings.trend_expansion_max_stop_pts
            or reward <= 0
            or reward / risk < settings.min_rr_ratio
        ):
            return None

        return self._build_signal(
            candle=candle,
            levels=levels,
            setup_type=SetupType.TREND_EXPANSION_RECLAIM,
            setup_name="VWAP_RECLAIM",
            stop_loss=stop_loss,
            target_1=morning_high,
            reasons=[
                f"VWAP reclaim after a {impulse:.1f}-point morning impulse",
                f"Pullback depth {retrace:.1f} pts ({retrace / impulse:.0%}) stayed within the configured retracement band",
                "VWAP reclaimed with EMA9 at or above EMA20",
            ],
        )

    def _check_afternoon_breakout(
        self, candle: OHLCVCandle, levels: List[ResistanceLevel]
    ) -> Optional[AresSignal]:
        current_time = self._time(candle)
        anchor_end = self._parse_time(settings.trend_expansion_range_anchor_end)
        cutoff = self._parse_time(settings.trend_expansion_entry_cutoff_time)
        if not anchor_end < current_time <= cutoff:
            return None

        extended_range = [c for c in self._candles if self._time(c) <= anchor_end]
        previous_candles = self._candles[:-1]
        if not extended_range or not previous_candles:
            return None
        range_high = max(c.high for c in extended_range)
        if self._cumulative_volume <= 0:
            return None
        vwap = self._cumulative_tp_volume / self._cumulative_volume
        variance = max(
            self._cumulative_tp2_volume / self._cumulative_volume - vwap * vwap,
            0.0,
        )
        vwap_std = sqrt(variance)
        vwap_zscore = (candle.close - vwap) / vwap_std if vwap_std else 0.0

        volumes = [float(item.volume) for item in self._candles[-20:]]
        volume_sma = mean(volumes) if len(self._candles) >= 20 else float(candle.volume)
        bbw = self._current_bbw()
        bbw_expansion = self._bbw_expansion(bbw)

        if not (
            candle.close > range_high + settings.trend_expansion_breakout_buffer_pts
            and candle.close > candle.vwap
            and candle.close > self._ema20
            and vwap_zscore >= settings.trend_expansion_min_afternoon_vwap_zscore
            and candle.volume >= settings.trend_expansion_min_volume_ratio * volume_sma
            and bbw_expansion >= settings.trend_expansion_min_bbw_expansion
        ):
            return None

        base = previous_candles[-20:]
        base_low = min(item.low for item in base)
        stop_loss = max(
            base_low,
            candle.close - settings.trend_expansion_max_stop_pts,
        )
        risk = candle.close - stop_loss
        if (
            risk < settings.trend_expansion_breakout_min_stop_pts
            or risk > settings.trend_expansion_max_stop_pts
        ):
            return None

        target_1 = candle.close + settings.trend_expansion_t1_reward_pts
        if settings.trend_expansion_t1_reward_pts / risk < settings.min_rr_ratio:
            return None
        return self._build_signal(
            candle=candle,
            levels=levels,
            setup_type=SetupType.TREND_EXPANSION_BREAKOUT,
            setup_name="AFTERNOON_BREAKOUT",
            stop_loss=stop_loss,
            target_1=target_1,
            reasons=[
                f"Close broke {range_high:.2f} range high with volume confirmation",
                f"VWAP z-score {vwap_zscore:.2f} and BBW expansion {bbw_expansion:.2f} confirmed momentum",
            ],
        )

    def _build_signal(
        self,
        candle: OHLCVCandle,
        levels: List[ResistanceLevel],
        setup_type: SetupType,
        setup_name: str,
        stop_loss: float,
        target_1: float,
        reasons: list[str],
    ) -> AresSignal:
        structural_targets = [
            level.price for level in levels if level.price > target_1
        ]
        fallback_target_2 = max(
            candle.close + settings.trend_expansion_target_2_fallback_pts,
            target_1 + 1.0,
        )
        target_2 = min(structural_targets) if structural_targets else fallback_target_2
        reasons.append(
            f"Expansion regime qualified on {self._regime_score}/4 configured factors"
        )
        signal = AresSignal(
            setup_type=setup_type,
            direction=Direction.BULLISH,
            trigger_price=candle.close,
            entry_zone=(
                candle.close - settings.entry_zone_offset_pts,
                candle.close + settings.entry_zone_offset_pts,
            ),
            stop_loss=stop_loss,
            target_1=target_1,
            target_2=target_2,
            confidence=confidence_from_score(self._regime_score, max_score=4),
            reasons=reasons,
            timestamp=candle.timestamp,
            strike_to_trade=int(
                round(candle.close / settings.strike_interval) * settings.strike_interval
            ),
            option_type="CE",
            use_detector_levels=True,
            market_context={
                "trend_expansion_setup": setup_name,
                "regime_score": self._regime_score,
                "regime_factors": dict(self._regime_factors),
                "target_2_is_reference": True,
                "signal_mode": "MANUAL_SIGNAL_REFERENCE",
            },
        )
        return self.calibration_policy.apply_to_signal(signal) if self.calibration_policy else signal

    def _current_bbw(self) -> float:
        return self._bbw_history[-1] if self._bbw_history else 0.0

    def _bbw_expansion(self, bbw: float) -> float:
        if not self._bbw_history:
            return 0.0
        baseline = mean(self._bbw_history[-20:]) if len(self._bbw_history) >= 20 else bbw
        return bbw / baseline if baseline > 0 else 0.0

    @staticmethod
    def _time(candle: OHLCVCandle) -> time:
        return TrendExpansionDetector._local_timestamp(candle.timestamp).time()

    @staticmethod
    def _local_timestamp(timestamp: datetime) -> datetime:
        if timestamp.tzinfo is None:
            return timestamp
        return timestamp.astimezone(IST).replace(tzinfo=None)

    @staticmethod
    def _parse_time(value: str) -> time:
        hour, minute = (int(part) for part in value.split(":"))
        return time(hour, minute)
