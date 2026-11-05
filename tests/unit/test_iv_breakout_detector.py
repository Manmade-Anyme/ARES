import pytest
from datetime import datetime, timedelta
import pytz

from models import OHLCVCandle, AresSignal, SetupType, Direction
from options_math import evaluate_iv_velocity
from detectors.iv_breakout import IVBreakoutDetector
from config_profiles import SetupLevels, _PER_TYPE_LEVELS_DEFAULT
from engine import AresEngine

IST = pytz.timezone("Asia/Kolkata")


def test_evaluate_iv_velocity_approvals_and_rejections():
    """Validates the calibrated Asymmetric Anti-Crush Gate rules."""
    # 1. Normal IV expansion -> Approved
    ok, reason = evaluate_iv_velocity(iv_current=15.0, iv_prev_1m=14.8, iv_prev_3m=14.5, vwap_zscore=1.2)
    assert ok is True
    assert reason == "IV_EXPANSION_CONFIRMED"

    # 2. Severe Vol-Crush (1m dump <= -0.30%) -> Rejected
    ok, reason = evaluate_iv_velocity(iv_current=14.5, iv_prev_1m=14.9, iv_prev_3m=15.0, vwap_zscore=1.0)
    assert ok is False
    assert reason == "VOL_CRUSH_BULL_TRAP"

    # 3. Severe Vol-Crush (3m dump <= -0.40%) -> Rejected
    ok, reason = evaluate_iv_velocity(iv_current=14.5, iv_prev_1m=14.6, iv_prev_3m=15.1, vwap_zscore=1.0)
    assert ok is False
    assert reason == "VOL_CRUSH_BULL_TRAP"

    # 4. Late-stage climax chase (Z > 2.2 with flat/falling IV) -> Rejected
    ok, reason = evaluate_iv_velocity(iv_current=15.0, iv_prev_1m=15.05, iv_prev_3m=15.0, vwap_zscore=2.5)
    assert ok is False
    assert reason == "LATE_STAGE_EXHAUSTION"


def test_iv_breakout_detector_lifecycle():
    """Tests the IVBreakoutDetector across session progression."""
    detector = IVBreakoutDetector()
    date_now = datetime(2026, 11, 5, 9, 15, tzinfo=IST)

    # 1. Morning candles establishing anchor high at 24400.0
    for m in range(0, 195):  # 09:15 to 12:30
        t = date_now + timedelta(minutes=m)
        c = OHLCVCandle(
            timestamp=t,
            open=24350.0,
            high=24400.0 if m == 30 else 24380.0,
            low=24340.0,
            close=24360.0,
            volume=1000000,
            vwap=24360.0
        )
        sig = detector.update(c, avg_volume=1000000, iv_current=13.0)
        assert sig is None  # No entries before 12:30

    assert detector.anchor_high == 24400.0

    # 2. Breakout candle at 13:00 above anchor high (24405 > 24402) with rising IV
    t_bo = date_now + timedelta(minutes=225)  # 13:00
    c_bo = OHLCVCandle(
        timestamp=t_bo,
        open=24395.0,
        high=24408.0,
        low=24390.0,
        close=24405.0,
        volume=1500000,
        vwap=24370.0
    )
    # Provide expanding IV
    detector.iv_history.append((t_bo - timedelta(minutes=3), 12.8))
    detector.iv_history.append((t_bo - timedelta(minutes=1), 13.0))

    sig = detector.update(c_bo, avg_volume=1000000, iv_current=13.2)

    assert sig is not None
    assert sig.setup_type == SetupType.IV_GATED_BREAKOUT
    assert sig.direction == Direction.BULLISH
    assert sig.trigger_price == 24405.0
    assert sig.option_type == "CE"
    assert sig.confidence == "HIGH"


def test_iv_breakout_levels_configured():
    """Ensures SetupType.IV_GATED_BREAKOUT has valid per-type levels."""
    assert SetupType.IV_GATED_BREAKOUT.value in _PER_TYPE_LEVELS_DEFAULT
    lv = _PER_TYPE_LEVELS_DEFAULT[SetupType.IV_GATED_BREAKOUT.value]
    assert lv.stop_pts == 30.0
    assert lv.target_1_pts == 35.0
    assert lv.target_2_fallback_pts == 70.0


def test_ares_engine_emits_iv_breakout_signal():
    """Tests AresEngine integration with IVBreakoutDetector and level assignment."""
    from models import OptionRow, ATMStrikes
    engine = AresEngine()
    date_now = datetime(2026, 11, 5, 9, 15, tzinfo=IST)

    # Prime anchor high
    for m in range(0, 195):
        t = date_now + timedelta(minutes=m)
        c = OHLCVCandle(
            timestamp=t, open=24350.0, high=24400.0, low=24340.0, close=24360.0,
            volume=1000000, vwap=24360.0
        )
        atm = ATMStrikes(
            spot_price=24360.0,
            ce=OptionRow(strike=24350, option_type="CE", iv=13.0),
            pe=OptionRow(strike=24350, option_type="PE", iv=13.0)
        )
        engine.tick(c, full_chain=[], atm=atm)

    # Breakout candle at 13:00
    t_bo = date_now + timedelta(minutes=225)
    c_bo = OHLCVCandle(
        timestamp=t_bo, open=24395.0, high=24408.0, low=24390.0, close=24405.0,
        volume=1500000, vwap=24370.0
    )
    # Give expanding IV
    engine.iv_breakout_detector.iv_history.append((t_bo - timedelta(minutes=3), 12.8))
    engine.iv_breakout_detector.iv_history.append((t_bo - timedelta(minutes=1), 13.0))
    atm_bo = ATMStrikes(
        spot_price=24405.0,
        ce=OptionRow(strike=24400, option_type="CE", iv=13.2),
        pe=OptionRow(strike=24400, option_type="PE", iv=13.2)
    )

    signal = engine.tick(c_bo, full_chain=[], atm=atm_bo)

    assert signal is not None
    assert signal.setup_type == SetupType.IV_GATED_BREAKOUT
    assert signal.direction == Direction.BULLISH
    # Engine levels assigned via apply_per_type_levels
    assert signal.stop_loss == 24405.0 - 30.0  # 24375.0
    assert signal.target_1 == 24405.0 + 35.0   # 24440.0

