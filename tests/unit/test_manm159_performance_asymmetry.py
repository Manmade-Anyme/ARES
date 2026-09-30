"""MANM-159 directional level, regime, and continuation regressions."""

from datetime import datetime, timedelta
from unittest.mock import MagicMock

import pytest

from config import settings
from config_profiles import NON_EXPIRY_CONFIG, TuningConfig
from detectors.continuation import TrendContinuationDetector
from engine import AresEngine, apply_per_type_levels
from models import AresSignal, ATMStrikes, Direction, OHLCVCandle, SetupType


def candle(close=24000.0, vwap=24020.0, volume=100000, minute=0):
    return OHLCVCandle(
        timestamp=datetime(2026, 9, 30, 9, 15) + timedelta(minutes=minute),
        open=close - 2,
        high=close + 3,
        low=close - 3,
        close=close,
        volume=volume,
        vwap=vwap,
    )


def signal(setup_type, direction):
    return AresSignal(
        setup_type=setup_type,
        direction=direction,
        trigger_price=24000.0,
        entry_zone=(23995.0, 24005.0),
        stop_loss=23990.0,
        target_1=24020.0,
        target_2=24040.0,
        confidence="HIGH",
        reasons=["test"],
        timestamp=datetime(2026, 9, 30, 9, 15),
        strike_to_trade=24000,
        option_type="CE" if direction == Direction.BULLISH else "PE",
    )


def test_directional_continuation_geometry_preserves_bearish_runner():
    config = TuningConfig()
    bullish = signal(SetupType.TREND_CONTINUATION, Direction.BULLISH)
    bearish = signal(SetupType.TREND_CONTINUATION, Direction.BEARISH)

    apply_per_type_levels(bullish, config)
    apply_per_type_levels(bearish, config)

    assert (bullish.stop_loss, bullish.target_1, bullish.target_2) == (23980, 24020, 24050)
    assert (bearish.stop_loss, bearish.target_1, bearish.target_2) == (24025, 23975, 23920)


@pytest.mark.parametrize("setup_type", [SetupType.FAILED_BREAKOUT, SetupType.OI_WALL_REJECTION])
def test_downtrend_suppresses_bullish_fades_and_allows_next_detector(setup_type):
    settings.apply_profile(NON_EXPIRY_CONFIG)
    engine = AresEngine()
    candidate = signal(setup_type, Direction.BULLISH)
    engine.breakout_detector.update = MagicMock(return_value=candidate if setup_type == SetupType.FAILED_BREAKOUT else None)
    engine.oi_wall_detector.update = MagicMock(return_value=candidate if setup_type == SetupType.OI_WALL_REJECTION else None)
    engine.continuation_detector.update = MagicMock(return_value=None)
    exhaustion = signal(SetupType.EXHAUSTION_REVERSAL, Direction.BULLISH)
    engine.exhaustion_detector.update = MagicMock(return_value=exhaustion)
    atm = MagicMock(spec=ATMStrikes)
    atm.ce = atm.pe = None
    atm.spot_price = 24000.0

    result = engine.tick(candle(), [], atm, pdh=24100.0, pdl=23900.0)

    assert result is exhaustion
    engine.exhaustion_detector.update.assert_called_once()


def test_bullish_fade_allowed_when_regime_unknown():
    settings.apply_profile(NON_EXPIRY_CONFIG)
    engine = AresEngine()
    candidate = signal(SetupType.FAILED_BREAKOUT, Direction.BULLISH)
    engine.breakout_detector.update = MagicMock(return_value=candidate)
    engine.oi_wall_detector.update = MagicMock(return_value=None)
    atm = MagicMock(spec=ATMStrikes)
    atm.ce = atm.pe = None
    atm.spot_price = 24000.0

    assert engine.tick(candle(), [], atm, pdh=None, pdl=None) is candidate


@pytest.mark.parametrize(
    ("close", "volume", "emits"),
    [(24155.0, 130000, False), (24120.0, 150000, False), (24155.0, 150000, True)],
)
def test_bullish_continuation_needs_volume_and_close_above_ma20(close, volume, emits):
    settings.apply_profile(NON_EXPIRY_CONFIG)
    detector = TrendContinuationDetector()
    for i in range(20):
        detector.update(candle(close=24150.0, vwap=24100.0, minute=i), 100000, [], 24200, 24000)
    detector.update(candle(close=24100.0, vwap=24100.0, minute=20), 100000, [], 24200, 24000)
    detector.update(candle(close=24110.0, vwap=24100.0, volume=150000, minute=21), 100000, [], 24200, 24000)

    result = detector.update(candle(close=close, vwap=24100.0, volume=volume, minute=22), 100000, [], 24200, 24000)
    assert (result is not None) is emits
