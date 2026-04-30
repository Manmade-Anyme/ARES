import pytest
from datetime import datetime
from models import OHLCVCandle, SetupType, Direction, ResistanceLevel
from config import settings
from detectors.exhaustion import ExhaustionDetector

@pytest.fixture
def sample_levels():
    return [
        ResistanceLevel(price=24000.0, source="PDL", strength=3),
        ResistanceLevel(price=24100.0, source="PDH", strength=3),
        ResistanceLevel(price=24200.0, source="OI_WALL", strength=2),
    ]

@pytest.fixture
def exhaustion_detector():
    return ExhaustionDetector()

def test_bearish_exhaustion_dynamic_targets(exhaustion_detector, sample_levels):
    # Bullish push up, ending in a green doji at the top (close > open)
    candle = OHLCVCandle(
        timestamp=datetime.now(),
        open=24150.0,
        high=24160.0,
        low=24140.0,
        close=24155.0,
        volume=1000000
    )
    
    # Fill volume history with low volume to ensure climax
    for _ in range(20):
        exhaustion_detector.volume_history.append(100000)
        
    signal = exhaustion_detector.update(candle=candle, iv_current=20.0, iv_prev=15.0, levels=sample_levels)
    
    assert signal is not None
    assert signal.setup_type == SetupType.EXHAUSTION_REVERSAL
    assert signal.direction == Direction.BEARISH
    
    # Target 1 should be the nearest support below spot (24155.0) -> 24100.0
    assert signal.target_1 == 24100.0
    # Target 2 should be the next support -> 24000.0
    assert signal.target_2 == 24000.0

def test_bullish_exhaustion_dynamic_targets(exhaustion_detector, sample_levels):
    # Bearish push down, ending in a red doji at the bottom (close < open)
    candle = OHLCVCandle(
        timestamp=datetime.now(),
        open=24050.0,
        high=24060.0,
        low=24040.0,
        close=24045.0,
        volume=1000000
    )
    
    # Fill volume history with low volume to ensure climax
    for _ in range(20):
        exhaustion_detector.volume_history.append(100000)
        
    signal = exhaustion_detector.update(candle=candle, iv_current=20.0, iv_prev=15.0, levels=sample_levels)
    
    assert signal is not None
    assert signal.setup_type == SetupType.EXHAUSTION_REVERSAL
    assert signal.direction == Direction.BULLISH
    
    # Target 1 should be nearest resistance above spot (24045.0) -> 24100.0
    assert signal.target_1 == 24100.0
    # Target 2 should be next resistance -> 24200.0
    assert signal.target_2 == 24200.0

def test_target_fallback_mechanism(exhaustion_detector):
    # Level too close (< 15 pts) -> Should use fixed offset
    levels = [ResistanceLevel(price=24150.0, source="PDH", strength=3)]
    
    candle = OHLCVCandle(
        timestamp=datetime.now(),
        open=24142.0,
        high=24155.0,
        low=24125.0,
        close=24138.0, # Target 1 (24150) is only 12 pts away, fallback expected
        volume=1000000
    )
    
    # Bullish Exhaustion (Red Doji)
    for _ in range(20):
        exhaustion_detector.volume_history.append(100000)
        
    signal = exhaustion_detector.update(candle=candle, iv_current=20.0, iv_prev=15.0, levels=levels)
    
    assert signal is not None
    assert signal.target_1 == candle.close + settings.target_1_pts
    assert signal.target_2 == candle.close + settings.target_2_pts
