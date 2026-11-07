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
        elif time_str <= "12:30":
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
        candle=candle, pdc=95.0, pdl=90.0, levels=[], allow_signal=True
    )
    engine.continuation_detector.update.assert_not_called()
    assert engine.last_signal_time is not None
