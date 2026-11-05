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


def test_iv_breakout_defensive_mock_and_string_safety():
    """QA Test: Ensures MagicMock candles or string timestamps never raise TypeError."""
    from unittest.mock import MagicMock
    detector = IVBreakoutDetector()

    mock_candle = MagicMock()
    mock_candle.high = 24450.0
    mock_candle.close = 24440.0
    mock_candle.volume = 100000

    # Should safely return None without throwing TypeError: '<='
    sig = detector.update(mock_candle, avg_volume=100000, iv_current=12.5)
    assert sig is None


def test_iv_breakout_anchor_observed_during_cooldown():
    """QA Test: Engine Step 1 observation preserves morning anchor high during cooldown."""
    from models import OptionRow, ATMStrikes
    engine = AresEngine()
    engine.last_signal_time = datetime.now()  # Put engine into active cooldown

    t = datetime(2026, 11, 5, 10, 0, tzinfo=IST)
    c = OHLCVCandle(timestamp=t, open=24350.0, high=24460.0, low=24340.0, close=24360.0, volume=1000000, vwap=24360.0)
    atm = ATMStrikes(spot_price=24360.0, ce=OptionRow(strike=24350, option_type="CE", iv=13.0), pe=OptionRow(strike=24350, option_type="PE", iv=13.0))

    result = engine.tick(c, full_chain=[], atm=atm)
    assert result is None  # Suppressed by cooldown

    # BUT anchor high was observed and updated
    assert engine.iv_breakout_detector.anchor_high == 24460.0


def test_reports_includes_iv_gated_breakout():
    """QA Test: Verifies reports module tracks IV_GATED_BREAKOUT metrics."""
    from reports import SETUP_TYPES, SETUP_LABELS, compute_metrics
    assert "IV_GATED_BREAKOUT" in SETUP_TYPES
    assert "IV_GATED_BREAKOUT" in SETUP_LABELS

    trades = [{
        "setup_type": "IV_GATED_BREAKOUT",
        "pnl_points": 35.0,
        "net_pnl_points": 35.0,
        "entry_price": 24405.0,
        "exit_price": 24440.0,
        "exit_reason": "T1_HIT",
        "option_entry_price": 100.0,
        "option_exit_price": 135.0,
    }]
    metrics = compute_metrics(trades)
    assert "IV_GATED_BREAKOUT" in metrics["per_setup"]
    assert metrics["per_setup"]["IV_GATED_BREAKOUT"]["trades"] == 1
    assert metrics["per_setup"]["IV_GATED_BREAKOUT"]["net_points"] == 35.0


