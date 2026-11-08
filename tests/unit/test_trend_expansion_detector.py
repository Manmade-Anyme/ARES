from dataclasses import replace
from datetime import datetime, timedelta
from unittest.mock import MagicMock

import pytest

from config import settings
from config_profiles import NON_EXPIRY_CONFIG
from detectors.trend_expansion import TrendExpansionDetector
from engine import AresEngine, apply_per_type_levels
from models import AresSignal, ATMStrikes, Direction, OHLCVCandle, SetupType


def _session_candles(start_time=datetime(2026, 11, 5, 9, 15)):
    candles = []
    start_price = 24300.0
    for index in range(301):
        timestamp = start_time + timedelta(minutes=index)
        time_str = timestamp.strftime("%H:%M")
        if time_str <= "09:45":
            close = start_price + index * 4.0
        elif time_str <= "10:15":
            close = 24420.0 + (index - 30) * 1.0
        elif time_str < "12:30":
            close = 24450.0 - (index - 60) * 0.25
        else:
            close = 24460.0 + (index - 195) * 0.5
        candles.append(
            OHLCVCandle(
                timestamp=timestamp,
                open=close - 0.5,
                high=close + 1.0,
                low=close - 1.0,
                close=close,
                volume=1000,
                vwap=close - 40.0,
            )
        )
    return candles


def test_trend_expansion_detects_rolling_breakout_before_1030():
    settings.apply_profile(replace(
        NON_EXPIRY_CONFIG,
        trend_expansion_min_bbw_expansion=0.0,
    ))
    detector = TrendExpansionDetector()
    result = None

    for candle in _session_candles():
        result = detector.update(
            candle, pdc=24200.0, levels=[], iv_current=15.0, strike=24450.0
        )
        if result is not None:
            break

    assert result is not None
    assert result.setup_type == SetupType.TREND_EXPANSION_BREAKOUT
    assert result.direction == Direction.BULLISH
    assert result.use_detector_levels is True
    assert result.stop_loss < result.trigger_price < result.target_1 < result.target_2
    assert result.market_context["regime_score"] >= 2
    assert result.market_context["trend_expansion_setup"] == "ROLLING_BREAKOUT"
    assert result.timestamp.time() < datetime(2026, 11, 5, 10, 30).time()


def test_trend_expansion_requires_verified_prior_close_but_can_start_after_open():
    settings.apply_profile(NON_EXPIRY_CONFIG)
    detector = TrendExpansionDetector()

    for candle in _session_candles():
        result = detector.update(candle, pdc=None, levels=[])

    assert result is None

    late_start = datetime(2026, 11, 5, 9, 16)
    detector = TrendExpansionDetector()
    for candle in _session_candles(late_start)[:25]:
        detector.update(candle, pdc=24200.0, levels=[])

    assert detector._regime_evaluated is True


def test_regime_is_evaluated_on_each_live_candle_without_waiting_until_1030():
    settings.apply_profile(replace(
        NON_EXPIRY_CONFIG,
        trend_expansion_min_bbw_expansion=0.0,
    ))
    detector = TrendExpansionDetector()
    candles = _session_candles()

    detector.update(candles[0], pdc=24200.0)

    assert detector._regime_evaluated is True
    assert detector._regime_score >= 1


def test_missing_pdc_can_be_retried_on_a_later_candle():
    settings.apply_profile(replace(
        NON_EXPIRY_CONFIG,
        trend_expansion_min_bbw_expansion=0.0,
    ))
    detector = TrendExpansionDetector()
    candles = _session_candles()

    detector.update(candles[0], pdc=None)
    assert detector._regime_evaluated is False

    detector.update(
        candles[1],
        pdc=24200.0,
    )

    assert detector._regime_evaluated is True
    assert detector._regime_score >= 1


def test_missing_pdc_retry_failure_remains_fail_closed():
    """Verify that if PDC retry continues to fail (pdc stays None), regime is not evaluated and signals fail closed."""
    settings.apply_profile(replace(
        NON_EXPIRY_CONFIG,
        trend_expansion_min_bbw_expansion=0.0,
    ))
    detector = TrendExpansionDetector()
    candles = _session_candles()

    result = None
    for candle in candles:
        signal = detector.update(candle, pdc=None, levels=[])
        if signal is not None:
            result = signal
            break

    assert detector._regime_evaluated is False
    assert result is None


def test_dynamic_trend_expansion_levels_are_not_overwritten_by_profile():
    settings.apply_profile(NON_EXPIRY_CONFIG)
    signal = AresSignal(
        setup_type=SetupType.TREND_EXPANSION_BREAKOUT,
        direction=Direction.BULLISH,
        trigger_price=100.0,
        entry_zone=(95.0, 105.0),
        stop_loss=70.0,
        target_1=135.0,
        target_2=170.0,
        confidence="MEDIUM",
        reasons=[],
        timestamp=datetime(2026, 11, 5, 12, 31),
        strike_to_trade=100,
        option_type="CE",
        use_detector_levels=True,
    )

    apply_per_type_levels(signal, settings, levels=[])

    assert (signal.stop_loss, signal.target_1, signal.target_2) == (70.0, 135.0, 170.0)


def test_engine_wires_trend_expansion_before_continuation():
    settings.apply_profile(NON_EXPIRY_CONFIG)
    engine = AresEngine()
    signal = AresSignal(
        setup_type=SetupType.TREND_EXPANSION_RECLAIM,
        direction=Direction.BULLISH,
        trigger_price=100.0,
        entry_zone=(95.0, 105.0),
        stop_loss=70.0,
        target_1=130.0,
        target_2=160.0,
        confidence="MEDIUM",
        reasons=[],
        timestamp=datetime(2026, 11, 5, 11, 0),
        strike_to_trade=100,
        option_type="CE",
        use_detector_levels=True,
    )
    atm = MagicMock(spec=ATMStrikes)
    atm.ce = MagicMock()
    atm.ce.iv = 12.0
    atm.ce.oi = atm.ce.oi_prev = 100
    atm.pe = MagicMock()
    atm.pe.iv = 12.0
    atm.pe.oi = atm.pe.oi_prev = 100
    atm.spot_price = 100.0
    candle = OHLCVCandle(
        timestamp=datetime(2026, 11, 5, 11, 0),
        open=99.0,
        high=101.0,
        low=98.0,
        close=100.0,
        volume=1000,
        vwap=99.0,
    )

    engine.breakout_detector.update = MagicMock(return_value=None)
    engine.oi_wall_detector.update = MagicMock(return_value=None)
    engine.oi_wall_filter.update = MagicMock(return_value=MagicMock(status="NO_WALL"))
    engine.trend_expansion_detector.update = MagicMock(return_value=signal)
    engine.continuation_detector.update = MagicMock(return_value=None)
    engine.exhaustion_detector.update = MagicMock(return_value=None)

    result = engine.tick(candle, [], atm, 0.0, [], 110.0, 90.0, 95.0)

    assert result is signal
    engine.trend_expansion_detector.update.assert_called_once_with(
        candle=candle,
        pdc=95.0,
        pdl=90.0,
        levels=[],
        allow_signal=True,
        iv_current=12.0,
        strike=atm.ce.strike,
    )
    engine.continuation_detector.update.assert_not_called()
    assert engine.last_signal_time is not None


def test_opening_candle_gap_does_not_disable_the_rest_of_the_session():
    settings.apply_profile(replace(
        NON_EXPIRY_CONFIG,
        trend_expansion_min_bbw_expansion=0.0,
    ))
    detector = TrendExpansionDetector()
    candles = _session_candles()

    # Drop one early candle to model a temporary feed gap.
    candles_with_gap = [c for c in candles if c.timestamp.strftime("%H:%M") != "09:16"]

    result = None
    for candle in candles_with_gap:
        signal = detector.update(
            candle, pdc=24200.0, levels=[], iv_current=15.0, strike=24450.0
        )
        if signal is not None:
            result = signal
            break

    assert detector._regime_evaluated is True
    assert result is not None


def test_regime_evaluation_does_not_depend_on_1030_timestamp():
    settings.apply_profile(replace(
        NON_EXPIRY_CONFIG,
        trend_expansion_min_bbw_expansion=0.0,
    ))
    jittered_detector = TrendExpansionDetector()
    jittered_candles = [
        replace(c, timestamp=c.timestamp + timedelta(seconds=15))
        if c.timestamp.strftime("%H:%M") == "09:15"
        else c
        for c in _session_candles()[:1]
    ]
    jittered_detector.update(
        jittered_candles[0],
        pdc=24200.0,
        levels=[],
        iv_current=15.0,
        strike=24450.0,
    )

    assert jittered_detector._regime_evaluated is True
    assert jittered_detector._regime_score >= 1


def test_regime_range_expansion_uses_the_rolling_window_including_current_candle():
    settings.apply_profile(replace(
        NON_EXPIRY_CONFIG,
        trend_expansion_pdc_extension_pts=10000.0,
        trend_expansion_range_expansion_pts=100.0,
        trend_expansion_vwap_dispersion_pts=10000.0,
        trend_expansion_directional_displacement_pts=10000.0,
    ))
    detector = TrendExpansionDetector()
    detector._candles = [
        OHLCVCandle(datetime(2026, 11, 5, 14, 45), 24300, 24301, 24299, 24300, 1000),
        OHLCVCandle(datetime(2026, 11, 5, 14, 46), 24300, 24301, 24299, 24300, 1000),
        OHLCVCandle(datetime(2026, 11, 5, 14, 47), 24300, 24450, 24299, 24300, 1000),
        OHLCVCandle(datetime(2026, 11, 5, 14, 48), 24300, 24301, 24299, 24300, 1000),
    ]
    detector._cumulative_volume = 4000.0
    detector._cumulative_tp_volume = 24300.0 * 4000.0
    detector._cumulative_tp2_volume = 24300.0 ** 2 * 4000.0

    detector._evaluate_regime(pdc=24300.0)

    assert detector._regime_factors["range_expansion"] is True


def test_reclaim_uses_recent_impulse_at_any_time_of_day():
    settings.apply_profile(NON_EXPIRY_CONFIG)
    detector = TrendExpansionDetector()
    detector._candles = [
        OHLCVCandle(
            datetime(2026, 11, 5, 14, 0) + timedelta(minutes=i),
            100, 180, 90, 170, 1000,
        )
        for i in range(25)
    ]
    current = OHLCVCandle(datetime(2026, 11, 5, 14, 30), 100, 101, 97, 100, 1000, vwap=100)

    detector._check_reclaim(current, levels=[], allow_signal=False)

    assert detector._dipped_below_vwap is True


def test_rolling_breakout_is_eligible_during_morning():
    settings.apply_profile(replace(
        NON_EXPIRY_CONFIG,
        trend_expansion_min_bbw_expansion=0.0,
    ))
    candles = _session_candles()

    breakout_detector = TrendExpansionDetector()
    signal = None
    for candle in candles:
        signal = breakout_detector.update(
            candle, pdc=24200.0, iv_current=15.0, strike=24450.0
        )
        if signal is not None:
            break
    assert signal is not None
    assert signal.timestamp.time() < datetime(2026, 11, 5, 10, 30).time()


def test_rolling_breakout_is_eligible_late_in_the_session():
    settings.apply_profile(replace(
        NON_EXPIRY_CONFIG,
        trend_expansion_min_regime_score=1,
        trend_expansion_min_bbw_expansion=0.0,
        trend_expansion_iv_filter_enabled=False,
    ))
    detector = TrendExpansionDetector()
    date = datetime(2026, 11, 5)
    for minute in range(25):
        close = 24400.0 + (10.0 if minute % 2 else 0.0)
        detector.update(
            OHLCVCandle(
                date.replace(hour=14, minute=30) + timedelta(minutes=minute),
                close - 1, close + 5, close - 5, close, 1000,
            ),
            pdc=24200.0,
        )

    signal = detector.update(
        OHLCVCandle(date.replace(hour=14, minute=55), 24409, 24440, 24408, 24430, 1500),
        pdc=24200.0,
    )

    assert signal is not None
    assert signal.timestamp.time() == datetime(2026, 11, 5, 14, 55).time()


def test_internal_vwap_used_when_candle_vwap_is_zero():
    """Verify detector computes and uses its own VWAP when candle.vwap is default 0.0."""
    settings.apply_profile(replace(
        NON_EXPIRY_CONFIG,
        trend_expansion_min_bbw_expansion=0.0,
    ))
    detector = TrendExpansionDetector()

    # Pass candles with default vwap=0.0
    zero_vwap_candles = [
        replace(c, vwap=0.0) for c in _session_candles()
    ]

    result = None
    for candle in zero_vwap_candles:
        result = detector.update(
            candle, pdc=24200.0, levels=[], iv_current=15.0, strike=24450.0
        )
        if result is not None:
            break

    assert result is not None
    assert result.setup_type == SetupType.TREND_EXPANSION_BREAKOUT


def test_midday_gap_does_not_block_rolling_breakout():
    """Verify an isolated 1-minute data drop does not disable later detection."""
    settings.apply_profile(replace(
        NON_EXPIRY_CONFIG,
        trend_expansion_min_bbw_expansion=0.0,
    ))
    detector = TrendExpansionDetector()
    candles = _session_candles()

    # Introduce a 2-minute gap at 11:45 (drop 11:45 candle)
    candles_with_midday_drop = [
        c for c in candles if c.timestamp.strftime("%H:%M") != "11:45"
    ]

    result = None
    for candle in candles_with_midday_drop:
        allow_signal = candle.timestamp.strftime("%H:%M") >= "12:30"
        result = detector.update(
            candle, pdc=24200.0, levels=[], allow_signal=allow_signal,
            iv_current=15.0, strike=24450.0,
        )
        if result is not None:
            break

    assert result is not None
    assert result.setup_type == SetupType.TREND_EXPANSION_BREAKOUT


def test_suppressed_reclaim_resets_state_without_candle_vwap():
    """Verify internal VWAP qualifies a suppressed reclaim and its sweep state resets."""
    start_time = datetime(2026, 11, 5, 9, 15)
    detector = TrendExpansionDetector()
    detector._session_date = start_time.date()
    detector._regime_evaluated = True
    detector._regime_score = 3
    detector._trades_today = 0
    detector._ema9 = 24400.0
    detector._ema20 = 24390.0

    # Add morning candles to establish impulse: 24300 to 24420
    for i in range(61):
        detector._candles.append(
            OHLCVCandle(
                timestamp=start_time + timedelta(minutes=i),
                open=24300.0, high=24420.0, low=24300.0, close=24400.0, volume=1000, vwap=24350.0
            )
        )
    detector._cumulative_volume = 100000.0
    detector._cumulative_tp_volume = 24350.0 * 100000.0

    # Candle dipping below VWAP (sweep low = 24340, close = 24345 < vwap 24350)
    dip_candle = OHLCVCandle(
        timestamp=datetime(2026, 11, 5, 10, 35),
        open=24352.0, high=24352.0, low=24340.0, close=24345.0, volume=1000, vwap=0.0
    )
    detector.update(dip_candle, pdc=24200.0, allow_signal=False)
    assert detector._dipped_below_vwap is True
    assert detector._sweep_low == 24340.0

    # Reclaim candle that meets all criteria, but allow_signal is False (e.g. cooldown active)
    reclaim_candle = OHLCVCandle(
        timestamp=datetime(2026, 11, 5, 10, 45),
        open=24346.0, high=24390.0, low=24346.0, close=24385.0, volume=1000, vwap=0.0
    )
    sig = detector.update(reclaim_candle, pdc=24200.0, allow_signal=False)
    assert sig is None
    # State MUST be reset so it cannot leak and trigger late
    assert detector._dipped_below_vwap is False
    assert detector._sweep_low == float("inf")


def test_trend_expansion_breakout_suppressed_by_vol_crush():
    """Verify breakout is suppressed when IV velocity detects institutional vol crush."""
    settings.apply_profile(replace(
        NON_EXPIRY_CONFIG,
        trend_expansion_min_bbw_expansion=0.0,
        trend_expansion_iv_filter_enabled=True,
    ))
    detector = TrendExpansionDetector()
    candles = _session_candles()

    # Build valid history without consuming the daily signal allowance.
    result = None
    for candle in candles:
        time_str = candle.timestamp.strftime("%H:%M")
        if time_str < "12:30":
            detector.update(
                candle, pdc=24200.0, levels=[], allow_signal=False,
                iv_current=15.0, strike=24450.0,
            )
        elif time_str == "12:30":
            # IV dumps from 15.0 to 14.0 on the candidate candle.
            result = detector.update(candle, pdc=24200.0, levels=[], iv_current=14.0, strike=24450.0)
            break

    # Because IV dumped from 15.0 to 14.0 (d1 = -1.0 <= -0.30), VOL_CRUSH_BULL_TRAP suppresses the signal
    assert result is None


def test_trend_expansion_breakout_fails_closed_without_fresh_iv_history():
    settings.apply_profile(replace(
        NON_EXPIRY_CONFIG,
        trend_expansion_min_bbw_expansion=0.0,
        trend_expansion_iv_filter_enabled=True,
    ))
    detector = TrendExpansionDetector()
    result = None

    for candle in _session_candles():
        if candle.timestamp.strftime("%H:%M") == "12:30":
            result = detector.update(
                candle, pdc=24200.0, iv_current=15.0, strike=24450.0
            )
            break
        detector.update(candle, pdc=24200.0, allow_signal=False)

    assert result is None


@pytest.mark.parametrize("iv_current", [None, 0.0, float("nan"), float("inf")])
def test_trend_expansion_breakout_fails_closed_without_valid_iv(iv_current):
    settings.apply_profile(replace(
        NON_EXPIRY_CONFIG,
        trend_expansion_min_bbw_expansion=0.0,
        trend_expansion_iv_filter_enabled=True,
    ))
    detector = TrendExpansionDetector()
    result = None

    for candle in _session_candles():
        if candle.timestamp.strftime("%H:%M") == "12:30":
            result = detector.update(
                candle, pdc=24200.0, levels=[], iv_current=iv_current
            )
            break
        detector.update(
            candle, pdc=24200.0, levels=[], allow_signal=False,
            iv_current=15.0, strike=24450.0,
        )

    assert result is None
