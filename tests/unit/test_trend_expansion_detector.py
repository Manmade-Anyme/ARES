from dataclasses import replace
from datetime import datetime, timedelta
from unittest.mock import MagicMock

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


def test_trend_expansion_emits_dynamic_breakout_signal_after_qualified_regime():
    settings.apply_profile(replace(
        NON_EXPIRY_CONFIG,
        trend_expansion_min_bbw_expansion=0.0,
    ))
    detector = TrendExpansionDetector()
    result = None

    for candle in _session_candles():
        result = detector.update(candle, pdc=24200.0, levels=[])
        if result is not None:
            break

    assert result is not None
    assert result.setup_type == SetupType.TREND_EXPANSION_BREAKOUT
    assert result.direction == Direction.BULLISH
    assert result.use_detector_levels is True
    assert result.stop_loss < result.trigger_price < result.target_1 < result.target_2
    assert result.market_context["regime_score"] >= 2
    assert result.market_context["trend_expansion_setup"] == "AFTERNOON_BREAKOUT"


def test_trend_expansion_requires_verified_prior_close_and_complete_session():
    settings.apply_profile(NON_EXPIRY_CONFIG)
    detector = TrendExpansionDetector()

    for candle in _session_candles():
        result = detector.update(candle, pdc=None, levels=[])

    assert result is None

    late_start = datetime(2026, 11, 5, 9, 16)
    detector = TrendExpansionDetector()
    for candle in _session_candles(late_start):
        result = detector.update(candle, pdc=24200.0, levels=[])

    assert result is None
    assert detector.session_complete is False


def test_missing_pdc_does_not_consume_regime_evaluation():
    settings.apply_profile(replace(
        NON_EXPIRY_CONFIG,
        trend_expansion_min_bbw_expansion=0.0,
    ))
    detector = TrendExpansionDetector()
    candles = _session_candles()

    for candle in candles:
        detector.update(candle, pdc=None)
        if candle.timestamp.time() >= datetime(2026, 11, 5, 10, 30).time():
            break
    assert detector._regime_evaluated is False

    detector.update(
        OHLCVCandle(datetime(2026, 11, 5, 10, 31), 24400, 24401, 24399, 24400, 1000),
        pdc=24200.0,
    )

    assert detector._regime_evaluated is True
    assert detector._regime_score >= 2


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


def test_missing_1030_candle_followed_by_1031_fails_closed():
    """Verify that a missing 10:30 candle (gap from 10:29 to 10:31) marks session incomplete and fails closed."""
    settings.apply_profile(replace(
        NON_EXPIRY_CONFIG,
        trend_expansion_min_bbw_expansion=0.0,
    ))
    detector = TrendExpansionDetector()
    candles = _session_candles()

    # Drop exact 10:30 candle (10:29 -> 10:31 gap)
    candles_with_gap = [c for c in candles if c.timestamp.strftime("%H:%M") != "10:30"]

    result = None
    for candle in candles_with_gap:
        signal = detector.update(candle, pdc=24200.0, levels=[])
        if signal is not None:
            result = signal
            break

    assert detector.session_complete is False
    assert detector._regime_evaluated is False
    assert result is None


def test_regime_evaluation_survives_1030_timestamp_jitter():
    """Verify regime is evaluated when 10:30 candle arrives with sub-minute seconds."""
    settings.apply_profile(replace(
        NON_EXPIRY_CONFIG,
        trend_expansion_min_bbw_expansion=0.0,
    ))
    jittered_detector = TrendExpansionDetector()
    jittered_candles = [
        replace(c, timestamp=c.timestamp + timedelta(seconds=15))
        if c.timestamp.strftime("%H:%M") == "10:30"
        else c
        for c in _session_candles()
    ]
    result = None
    for candle in jittered_candles:
        result = jittered_detector.update(candle, pdc=24200.0, levels=[])
        if result is not None:
            break

    assert jittered_detector.session_complete is True
    assert jittered_detector._regime_evaluated is True
    assert jittered_detector._regime_score >= 2
    assert result is not None
    assert result.setup_type == SetupType.TREND_EXPANSION_BREAKOUT


def test_regime_opening_range_excludes_0945_candle():
    settings.apply_profile(replace(
        NON_EXPIRY_CONFIG,
        trend_expansion_gap_extension_pts=10000.0,
        trend_expansion_opening_range_pts=100.0,
        trend_expansion_vwap_dispersion_pts=10000.0,
        trend_expansion_directional_displacement_pts=10000.0,
    ))
    detector = TrendExpansionDetector()
    detector._session_complete = True
    detector._candles = [
        OHLCVCandle(datetime(2026, 11, 5, 9, 15), 24300, 24301, 24299, 24300, 1000),
        OHLCVCandle(datetime(2026, 11, 5, 9, 44), 24300, 24301, 24299, 24300, 1000),
        OHLCVCandle(datetime(2026, 11, 5, 9, 45), 24300, 24450, 24299, 24300, 1000),
        OHLCVCandle(datetime(2026, 11, 5, 10, 30), 24300, 24301, 24299, 24300, 1000),
    ]
    detector._cumulative_volume = 4000.0
    detector._cumulative_tp_volume = 24300.0 * 4000.0
    detector._cumulative_tp2_volume = 24300.0 ** 2 * 4000.0

    detector._evaluate_regime(pdc=24300.0)

    assert detector._regime_factors["range_expansion"] is False


def test_reclaim_morning_range_excludes_1015_candle():
    settings.apply_profile(NON_EXPIRY_CONFIG)
    detector = TrendExpansionDetector()
    detector._candles = [
        OHLCVCandle(datetime(2026, 11, 5, 9, 15), 100, 131, 90, 100, 1000),
        OHLCVCandle(datetime(2026, 11, 5, 10, 14), 100, 130, 90, 100, 1000),
        OHLCVCandle(datetime(2026, 11, 5, 10, 15), 100, 200, 90, 100, 1000),
    ]
    current = OHLCVCandle(datetime(2026, 11, 5, 10, 30), 100, 101, 97, 100, 1000, vwap=100)

    detector._check_reclaim(current, levels=[], allow_signal=False)

    assert detector._dipped_below_vwap is False


def test_breakout_window_includes_1230_and_excludes_1415():
    settings.apply_profile(replace(
        NON_EXPIRY_CONFIG,
        trend_expansion_min_bbw_expansion=0.0,
    ))
    candles = _session_candles()

    breakout_detector = TrendExpansionDetector()
    for candle in candles:
        if candle.timestamp.time() < datetime(2026, 11, 5, 12, 30).time():
            breakout_detector.update(candle, pdc=24200.0, allow_signal=False)
        else:
            signal = breakout_detector.update(candle, pdc=24200.0)
            break
    assert signal is not None
    assert signal.timestamp.time() == datetime(2026, 11, 5, 12, 30).time()

    cutoff_detector = TrendExpansionDetector()
    for candle in candles:
        if candle.timestamp.time() < datetime(2026, 11, 5, 14, 15).time():
            cutoff_detector.update(candle, pdc=24200.0, allow_signal=False)
        else:
            signal = cutoff_detector.update(candle, pdc=24200.0)
            break
    assert signal is None


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
        result = detector.update(candle, pdc=24200.0, levels=[])
        if result is not None:
            break

    assert result is not None
    assert result.setup_type == SetupType.TREND_EXPANSION_BREAKOUT


def test_midday_gap_after_1030_does_not_block_afternoon_breakout():
    """Verify an isolated 1-minute data drop during midday chop does not kill afternoon breakout."""
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
        result = detector.update(candle, pdc=24200.0, levels=[])
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
    detector._session_complete = True
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


def test_trend_expansion_afternoon_breakout_suppressed_by_vol_crush():
    """Verify that afternoon breakout is suppressed when IV velocity detects institutional vol crush."""
    settings.apply_profile(replace(
        NON_EXPIRY_CONFIG,
        trend_expansion_min_bbw_expansion=0.0,
        trend_expansion_iv_filter_enabled=True,
    ))
    detector = TrendExpansionDetector()
    candles = _session_candles()

    # Pass candles up to breakout with crushing IV on the breakout candle at 12:30
    result = None
    for candle in candles:
        time_str = candle.timestamp.strftime("%H:%M")
        if time_str < "12:30":
            # Normal stable IV
            detector.update(candle, pdc=24200.0, levels=[], iv_current=15.0, strike=24450.0)
        elif time_str == "12:30":
            # Breakout candle fires at 12:30, but IV dumps from 15.0 to 14.0
            result = detector.update(candle, pdc=24200.0, levels=[], iv_current=14.0, strike=24450.0)
            break

    # Because IV dumped from 15.0 to 14.0 (d1 = -1.0 <= -0.30), VOL_CRUSH_BULL_TRAP suppresses the signal
    assert result is None


