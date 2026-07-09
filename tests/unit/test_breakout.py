import pytest
from datetime import datetime
from models import OHLCVCandle, SetupType, Direction, ResistanceLevel
from config import settings
from detectors.breakout import FailedBreakoutDetector

@pytest.fixture
def sample_levels():
    return [
        ResistanceLevel(price=24000.0, source="PDL", strength=3),
        ResistanceLevel(price=24100.0, source="PDH", strength=3),
        ResistanceLevel(price=24200.0, source="OI_WALL", strength=2),
    ]

@pytest.fixture
def breakout_detector():
    return FailedBreakoutDetector()

def test_bearish_failed_breakout_dynamic_targets(breakout_detector, sample_levels):
    # Upward breakout cross above 24100
    candle1 = OHLCVCandle(
        timestamp=datetime.now(), open=24090.0, high=24120.0, low=24080.0, close=24110.0, volume=50000
    )
    breakout_detector.update(candle1, 100000.0, 5.0, 100, 100, 100, 100, sample_levels)
    assert breakout_detector.active is not None
    assert breakout_detector.active.level == 24100.0
    
    # Close back below 24100 (Failure)
    candle2 = OHLCVCandle(
        timestamp=datetime.now(), open=24110.0, high=24115.0, low=24080.0, close=24090.0, volume=40000
    )
    # IV crush, options writers hold
    signal = breakout_detector.update(candle2, 100000.0, -15.0, 150, 100, 100, 100, sample_levels)
    
    assert signal is not None
    assert signal.setup_type == SetupType.FAILED_BREAKOUT
    assert signal.direction == Direction.BEARISH
    # SL/T1/T2 are set centrally by engine.apply_per_type_levels (TASK-185).

def test_bullish_failed_breakout_dynamic_targets(breakout_detector, sample_levels):
    # Downward breakdown below 24100
    candle1 = OHLCVCandle(
        timestamp=datetime.now(), open=24110.0, high=24120.0, low=24090.0, close=24080.0, volume=50000
    )
    breakout_detector.update(candle1, 100000.0, 5.0, 100, 100, 100, 100, sample_levels)
    assert breakout_detector.active is not None
    assert breakout_detector.active.level == 24100.0
    
    # Close back above 24100 (Failure)
    candle2 = OHLCVCandle(
        timestamp=datetime.now(), open=24080.0, high=24120.0, low=24075.0, close=24115.0, volume=40000
    )
    # IV crush, options writers hold
    signal = breakout_detector.update(candle2, 100000.0, -15.0, 100, 100, 150, 100, sample_levels)
    
    assert signal is not None
    assert signal.setup_type == SetupType.FAILED_BREAKOUT
    assert signal.direction == Direction.BULLISH
    # SL/T1/T2 are set centrally by engine.apply_per_type_levels (TASK-185).

def test_failed_breakout_confidence_high(breakout_detector, sample_levels):
    # Upward breakout
    candle1 = OHLCVCandle(
        timestamp=datetime.now(), open=24090.0, high=24120.0, low=24080.0, close=24110.0, volume=50000
    )
    breakout_detector.update(candle1, 100000.0, 5.0, 100, 100, 100, 100, sample_levels)

    # Reversal candle: volume average is 100000 (breakout weak volume ratio is 0.75 -> 50000 volume is weak) -> 1 point
    # Close back below 24100 by >= 5 points (close = 24090.0, level = 24100.0 -> diff 10.0 >= 5.0) -> 1 point
    # Options writers CE OI increased from 100 to 110 (growth = 10% >= 10% threshold) -> 1 point
    # IV change is -15.0 < -3.0 (IV crush) -> 1 point
    # (closed_back is the gate and writers_holding is unscored per TASK-174)
    # -> total score = 4 of 4
    candle2 = OHLCVCandle(
        timestamp=datetime.now(), open=24110.0, high=24115.0, low=24080.0, close=24090.0, volume=40000
    )
    signal = breakout_detector.update(candle2, 100000.0, -15.0, 110, 100, 100, 100, sample_levels)
    assert signal is not None
    assert signal.confidence == "HIGH"

def test_failed_breakout_weak_failure_rejected(breakout_detector, sample_levels):
    # closed_back is a hard gate, not a scored point (TASK-172 audit item 8);
    # the min score is 2 (TASK-184 restored the MEDIUM tier). A marginal
    # close-back with no scored conditions (score 0) still produces no signal.
    candle1 = OHLCVCandle(
        timestamp=datetime.now(), open=24090.0, high=24120.0, low=24080.0, close=24110.0, volume=50000
    )
    breakout_detector.update(candle1, 10000.0, 5.0, 100, 100, 100, 100, sample_levels)

    # Reversal candle:
    # Close back marginally below 24100 (close = 24099.0, diff 1.0 < 5.0) -> 0 points
    # Avg volume 10000 -> breakout volume (50000) not weak -> 0 points
    # IV change is 0.0 > -3.0 -> 0 points
    # Options writers CE OI change is 0% -> 0 points
    # Writers held -> unscored (TASK-174); total score = 0 < 2, closed_back not counted
    candle2 = OHLCVCandle(
        timestamp=datetime.now(), open=24110.0, high=24115.0, low=24080.0, close=24099.0, volume=40000
    )
    signal = breakout_detector.update(candle2, 10000.0, 0.0, 100, 100, 100, 100, sample_levels)
    assert signal is None

def test_no_breakout_event(breakout_detector, sample_levels):
    # Candle close does not cross any level (spot is 24050, level is 24100, close is 24060)
    candle = OHLCVCandle(
        timestamp=datetime.now(), open=24040.0, high=24070.0, low=24030.0, close=24060.0, volume=1000
    )
    result = breakout_detector.update(candle, 10000.0, 0.0, 100, 100, 100, 100, sample_levels)
    assert result is None
    assert breakout_detector.active is None

def test_breakout_confirmed_real_breakout(breakout_detector, sample_levels):
    # Breakout candle
    candle1 = OHLCVCandle(
        timestamp=datetime.now(), open=24090.0, high=24120.0, low=24080.0, close=24110.0, volume=50000
    )
    breakout_detector.update(candle1, 100000.0, 5.0, 100, 100, 100, 100, sample_levels)
    assert breakout_detector.active is not None

    # Update with candles remaining above 24100
    for i in range(settings.breakout_confirmation_candles + 1):
        candle = OHLCVCandle(
            timestamp=datetime.now(), open=24110.0, high=24130.0, low=24105.0, close=24115.0, volume=10000
        )
        result = breakout_detector.update(candle, 10000.0, 0.0, 100, 100, 100, 100, sample_levels)
        if i < settings.breakout_confirmation_candles:
            assert result is None
            assert breakout_detector.active is not None
        else:
            # Reached max confirmation candles without closing back -> reset state
            assert result is None
            assert breakout_detector.active is None

# NOTE (TASK-185): the breakout structural target-selection + fixed-points
# fallback tests were removed. SL/T1/T2 are now set centrally by
# engine.apply_per_type_levels (see tests/unit/test_per_type_levels.py).
