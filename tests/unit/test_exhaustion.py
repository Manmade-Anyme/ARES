import pytest
from datetime import datetime
from models import OHLCVCandle, ResistanceLevel
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

# NOTE (TASK-185): the per-detector "dynamic target selection", the fixed
# target_1_pts/target_2_pts fallback, and the T1/T2 ordering-swap were removed
# from the detectors — SL/T1/T2 are now set centrally by
# engine.apply_per_type_levels and covered by tests/unit/test_per_type_levels.py.
# The old target/fallback/sorting-swap tests were deleted accordingly.

def test_exhaustion_confidence_high(exhaustion_detector, sample_levels):
    # Climax volume: 1000000 (avg 100000, mult 2.5, extreme 1.5 * 2.5 * 100000 = 375000) -> 1 point
    # Extreme doji body ratio: open=24100.0, close=24101.0 (body = 1.0), high=24120.0, low=24080.0 (range=40.0), body/range = 1/40 = 0.025 < 0.5 * 0.35 = 0.175 -> 1 point
    # Panic IV spike: iv_current=25.0, iv_prev=15.0 (diff 10.0 > threshold 3.0) -> 1 point
    # Near structural level: high 24120.0 is within 10 pts of 24100 level (abs(24100 - 24120) = 20... Wait, 24200 level is 80 pts away. 
    # Let's adjust candle high/low to be within 10 pts of a level. 
    # Let's add a level at 24125.0 or adjust candle high to 24195 (near 24200) or low to 24095 (near 24100).
    # If candle high is 24195.0, range is 24195.0 - 24080.0 = 115.0. body/range = 1.0/115.0 = 0.008 < 0.175.
    # Level 24200.0 is within 10 pts of high (abs(24200.0 - 24195.0) = 5.0 <= 10.0) -> 1 point
    # Total score = 4
    levels = [
        ResistanceLevel(price=24200.0, source="OI_WALL", strength=2)
    ]
    candle = OHLCVCandle(
        timestamp=datetime.now(),
        open=24100.0,
        high=24195.0,
        low=24080.0,
        close=24101.0,
        volume=1000000
    )
    for _ in range(20):
        exhaustion_detector.volume_history.append(100000)
    signal = exhaustion_detector.update(candle=candle, iv_current=25.0, iv_prev=15.0, levels=levels)
    assert signal is not None
    assert signal.confidence == "HIGH"

def test_exhaustion_confidence_medium(exhaustion_detector):
    # Standard climax volume (1000000 vs avg 100000 * 2.5 = 250000, but not extreme 1.5x) -> 0 points (mult 2.5 * 1.5 * 100000 = 375000, wait, 1000000 is still > 375000, so it gets 1 point)
    # Let's use volume = 300000 to be climax but not extreme -> 0 points
    # Body ratio: open=24100.0, close=24110.0, high=24130.0, low=24090.0 (range=40.0, body=10.0, body/range = 0.25. settings.body_ratio is 0.35. 0.25 < 0.35, but not < 0.5 * 0.35 = 0.175) -> 0 points
    # IV change is 0.0 -> 0 points
    # No structural levels -> 0 points
    # Total score = 0 < 2 -> MEDIUM
    candle = OHLCVCandle(
        timestamp=datetime.now(),
        open=24100.0,
        high=24130.0,
        low=24090.0,
        close=24110.0,
        volume=300000
    )
    for _ in range(20):
        exhaustion_detector.volume_history.append(100000)
    signal = exhaustion_detector.update(candle=candle, iv_current=15.0, iv_prev=15.0, levels=[])
    assert signal is not None
    assert signal.confidence == "MEDIUM"

def test_exhaustion_not_enough_history(exhaustion_detector):
    # Queue is not warmed up (empty)
    candle = OHLCVCandle(
        timestamp=datetime.now(), open=24100.0, high=24110.0, low=24090.0, close=24095.0, volume=1000000
    )
    signal = exhaustion_detector.update(candle=candle, iv_current=20.0, iv_prev=15.0, levels=[])
    assert signal is None

def test_exhaustion_zero_range_candle(exhaustion_detector):
    # Candle high == low (range is 0)
    candle = OHLCVCandle(
        timestamp=datetime.now(), open=24100.0, high=24100.0, low=24100.0, close=24100.0, volume=1000000
    )
    for _ in range(20):
        exhaustion_detector.volume_history.append(100000)
    signal = exhaustion_detector.update(candle=candle, iv_current=20.0, iv_prev=15.0, levels=[])
    assert signal is None

def test_exhaustion_no_signal_conditions(exhaustion_detector):
    # Volume is weak (volume = 100000, which is equal to avg volume 100000, not climax)
    candle = OHLCVCandle(
        timestamp=datetime.now(), open=24100.0, high=24120.0, low=24080.0, close=24101.0, volume=100000
    )
    for _ in range(20):
        exhaustion_detector.volume_history.append(100000)
    signal = exhaustion_detector.update(candle=candle, iv_current=20.0, iv_prev=15.0, levels=[])
    assert signal is None

def test_exhaustion_bullish_near_level(exhaustion_detector):
    # Bullish setup (Red Doji, close < open) where candle low is within 10 pts of a level
    levels = [
        ResistanceLevel(price=24000.0, source="SUP_1", strength=2)
    ]
    # Candle low is 24005.0 (within 5 pts of 24000.0) -> near_level is True
    candle = OHLCVCandle(
        timestamp=datetime.now(),
        open=24050.0,
        high=24090.0,
        low=24005.0,
        close=24049.0,
        volume=1000000
    )
    for _ in range(20):
        exhaustion_detector.volume_history.append(100000)
        
    signal = exhaustion_detector.update(candle=candle, iv_current=20.0, iv_prev=15.0, levels=levels)
    assert signal is not None
    assert any("Exhaustion occurred at a significant key structural level" in r for r in signal.reasons)
