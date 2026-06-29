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
    
    # After sorting, T1 will be the closer target
    assert signal.target_1 == 24090.0 - settings.target_2_pts  # 24020.0 is closer to 24090 than 24000
    assert signal.target_2 == 24000.0

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
    
    # After sorting, T1 will be the closer target
    assert signal.target_1 == 24115.0 + settings.target_2_pts # 24185.0 is closer to 24115 than 24200
    assert signal.target_2 == 24200.0

def test_failed_breakout_confidence_high(breakout_detector, sample_levels):
    # Upward breakout
    candle1 = OHLCVCandle(
        timestamp=datetime.now(), open=24090.0, high=24120.0, low=24080.0, close=24110.0, volume=50000
    )
    breakout_detector.update(candle1, 100000.0, 5.0, 100, 100, 100, 100, sample_levels)

    # Reversal candle: volume average is 100000 (breakout weak volume ratio is 0.75 -> 50000 volume is weak) -> 1 point
    # Close back below 24100 by >= 5 points (close = 24090.0, level = 24100.0 -> diff 10.0 >= 5.0) -> 1 point
    # Options writers CE OI increased from 100 to 110 (growth = 10% >= 3%) -> 1 point
    # IV change is -15.0 < -3.0 (IV crush) -> 1 point
    # Writers held (110 >= 100) -> 1 point
    # Closed back -> 1 point (total score = 6)
    candle2 = OHLCVCandle(
        timestamp=datetime.now(), open=24110.0, high=24115.0, low=24080.0, close=24090.0, volume=40000
    )
    signal = breakout_detector.update(candle2, 100000.0, -15.0, 110, 100, 100, 100, sample_levels)
    assert signal is not None
    assert signal.confidence == "HIGH"

def test_failed_breakout_confidence_medium(breakout_detector, sample_levels):
    # Upward breakout
    candle1 = OHLCVCandle(
        timestamp=datetime.now(), open=24090.0, high=24120.0, low=24080.0, close=24110.0, volume=50000
    )
    breakout_detector.update(candle1, 10000.0, 5.0, 100, 100, 100, 100, sample_levels)

    # Reversal candle:
    # Close back marginally below 24100 (close = 24099.0, diff 1.0 < 5.0) -> 0 points
    # Avg volume 10000 -> breakout volume (50000) not weak -> 0 points
    # IV change is 0.0 > -3.0 -> 0 points
    # Options writers CE OI change is 0% -> 0 points
    # Writers held -> 1 point
    # Closed back -> 1 point (total score = 2 < 4)
    candle2 = OHLCVCandle(
        timestamp=datetime.now(), open=24110.0, high=24115.0, low=24080.0, close=24099.0, volume=40000
    )
    signal = breakout_detector.update(candle2, 10000.0, 0.0, 100, 100, 100, 100, sample_levels)
    assert signal is not None
    assert signal.confidence == "MEDIUM"

