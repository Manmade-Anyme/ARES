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
    ok, reason = evaluate_iv_velocity(iv_current=15.0, iv_prev_1m=14.9)
    assert ok is False
    assert reason == "NO_IV_DATA"

    # 1. Normal IV expansion -> Approved
    ok, reason = evaluate_iv_velocity(iv_current=15.0, iv_prev_1m=14.8, iv_prev_3m=14.5, vwap_zscore=1.2)
    assert ok is True
    assert reason == "IV_EXPANSION_CONFIRMED"

    # 2. Severe Vol-Crush (1m dump <= -0.30 IV points) -> Rejected
    ok, reason = evaluate_iv_velocity(iv_current=14.5, iv_prev_1m=14.9, iv_prev_3m=15.0, vwap_zscore=1.0)
    assert ok is False
    assert reason == "VOL_CRUSH_BULL_TRAP"

    # 3. Severe Vol-Crush (3m dump <= -0.40 IV points) -> Rejected
    ok, reason = evaluate_iv_velocity(iv_current=14.5, iv_prev_1m=14.6, iv_prev_3m=15.1, vwap_zscore=1.0)
    assert ok is False
    assert reason == "VOL_CRUSH_BULL_TRAP"

    # 4. Late-stage climax chase (Z > 2.2 with flat/falling IV) -> Rejected
    ok, reason = evaluate_iv_velocity(iv_current=15.0, iv_prev_1m=15.05, iv_prev_3m=15.0, vwap_zscore=2.5)
    assert ok is False
    assert reason == "LATE_STAGE_EXHAUSTION"


def test_iv_breakout_detector_lifecycle():
    """Tests the IVBreakoutDetector dynamic base breakout across any session time."""
    detector = IVBreakoutDetector()
    date_now = datetime(2026, 11, 5, 9, 15, tzinfo=IST)

    # 1. 25 base candles establishing consolidation range (24340 - 24365, range=25 pts)
    for m in range(0, 25):
        t = date_now + timedelta(minutes=m)
        c = OHLCVCandle(
            timestamp=t,
            open=24350.0,
            high=24365.0,
            low=24340.0,
            close=24355.0,
            volume=1000000,
            vwap=24355.0
        )
        sig = detector.update(c, avg_volume=1000000, iv_current=13.0, strike=24350.0)
        assert sig is None  # Establishing 25-bar consolidation base

    prior_time = date_now + timedelta(minutes=25)
    detector.update(
        OHLCVCandle(prior_time, 24350, 24365, 24340, 24355, 1000000, 24355),
        avg_volume=1000000,
        iv_current=13.0,
        strike=24350.0,
    )

    # 2. Breakout candle at 09:41 AM (minute 26) breaking above 24365.0 + 2.0 with volume surge
    t_bo = date_now + timedelta(minutes=26)
    c_bo = OHLCVCandle(
        timestamp=t_bo,
        open=24360.0,
        high=24375.0,
        low=24358.0,
        close=24370.0,
        volume=1300000,
        vwap=24356.0
    )
    sig = detector.update(
        c_bo, avg_volume=1000000, iv_current=13.2, strike=24350.0
    )

    assert sig is not None
    assert sig.setup_type == SetupType.IV_GATED_BREAKOUT
    assert sig.direction == Direction.BULLISH
    assert sig.trigger_price == 24370.0
    assert sig.option_type == "CE"
    assert sig.confidence == "HIGH"
    assert "Dynamic Base Breakout past 24365.00" in sig.reasons[0]


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

    # 25 base candles
    for m in range(0, 25):
        t = date_now + timedelta(minutes=m)
        c = OHLCVCandle(
            timestamp=t, open=24350.0, high=24365.0, low=24340.0, close=24355.0,
            volume=1000000, vwap=24355.0
        )
        atm = ATMStrikes(
            spot_price=24355.0,
            ce=OptionRow(strike=24350, option_type="CE", iv=13.0),
            pe=OptionRow(strike=24350, option_type="PE", iv=13.0)
        )
        engine.tick(c, full_chain=[], atm=atm)

    prior_time = date_now + timedelta(minutes=25)
    prior_candle = OHLCVCandle(
        timestamp=prior_time, open=24350.0, high=24365.0, low=24340.0,
        close=24355.0, volume=1000000, vwap=24355.0,
    )
    prior_atm = ATMStrikes(
        spot_price=24355.0,
        ce=OptionRow(strike=24350, option_type="CE", iv=13.0),
        pe=OptionRow(strike=24350, option_type="PE", iv=13.0),
    )
    engine.tick(prior_candle, full_chain=[], atm=prior_atm)

    # Breakout candle at minute 26 (09:41 AM)
    t_bo = date_now + timedelta(minutes=26)
    c_bo = OHLCVCandle(
        timestamp=t_bo, open=24360.0, high=24375.0, low=24358.0, close=24370.0,
        volume=1300000, vwap=24356.0
    )
    atm_bo = ATMStrikes(
        spot_price=24370.0,
        ce=OptionRow(strike=24350, option_type="CE", iv=13.2),
        pe=OptionRow(strike=24350, option_type="PE", iv=13.2)
    )

    signal = engine.tick(c_bo, full_chain=[], atm=atm_bo)

    assert signal is not None
    assert signal.setup_type == SetupType.IV_GATED_BREAKOUT
    assert signal.direction == Direction.BULLISH
    # Engine levels assigned via apply_per_type_levels
    assert signal.stop_loss == 24370.0 - 30.0  # 24340.0
    assert signal.target_1 == 24370.0 + 35.0   # 24405.0


def test_iv_breakout_fails_closed_when_same_strike_three_minute_sample_is_missing():
    detector = IVBreakoutDetector()
    date_now = datetime(2026, 11, 5, 9, 15, tzinfo=IST)

    for minute in range(25):
        candle = OHLCVCandle(
            timestamp=date_now + timedelta(minutes=minute),
            open=24350.0,
            high=24365.0,
            low=24340.0,
            close=24355.0,
            volume=1000000,
            vwap=24355.0,
        )
        detector.update(candle, 1000000, iv_current=None, strike=24350.0)

    one_minute_before = date_now + timedelta(minutes=25)
    detector.update(
        OHLCVCandle(one_minute_before, 24350, 24360, 24340, 24355, 1000000, 24355),
        1000000,
        iv_current=13.0,
        strike=24350.0,
    )
    breakout_time = date_now + timedelta(minutes=26)
    signal = detector.update(
        OHLCVCandle(breakout_time, 24360, 24375, 24358, 24370, 1300000, 24356),
        1000000,
        iv_current=13.2,
        strike=24350.0,
    )

    assert signal is None
    assert detector.latest_rejection_reason == "NO_IV_DATA"


def test_iv_breakout_defensive_mock_and_string_safety():
    """QA Test: Ensures MagicMock candles or string timestamps never raise TypeError."""
    from unittest.mock import MagicMock
    detector = IVBreakoutDetector()

    mock_candle = MagicMock()
    mock_candle.high = 24450.0
    mock_candle.close = 24440.0
    mock_candle.volume = 100000

    sig = detector.update(mock_candle, avg_volume=100000, iv_current=12.5)
    assert sig is None


def test_iv_breakout_buffer_observed_during_cooldown():
    """QA Test: Engine Step 1 observation preserves candle and IV history during cooldown."""
    from models import OptionRow, ATMStrikes
    engine = AresEngine()
    engine.last_signal_time = datetime.now()  # Put engine into active cooldown

    t = datetime(2026, 11, 5, 10, 0, tzinfo=IST)
    c = OHLCVCandle(timestamp=t, open=24350.0, high=24460.0, low=24340.0, close=24360.0, volume=1000000, vwap=24360.0)
    atm = ATMStrikes(spot_price=24360.0, ce=OptionRow(strike=24350, option_type="CE", iv=13.0), pe=OptionRow(strike=24350, option_type="PE", iv=13.0))

    result = engine.tick(c, full_chain=[], atm=atm)
    assert result is None  # Suppressed by cooldown

    # BUT history was observed and updated
    assert len(engine.iv_breakout_detector.candle_history) == 1
    assert engine.iv_breakout_detector.session_high == 24460.0


def test_iv_breakout_buffer_continuity_under_multi_candle_cooldown_and_recovery():
    """Validates IVBreakoutDetector buffer continuity across multi-candle cooldown and post-cooldown breakout."""
    from models import OptionRow, ATMStrikes
    from config import settings
    engine = AresEngine()
    engine.last_signal_time = datetime.now()  # Active cooldown

    date_now = datetime(2026, 11, 5, 9, 15, tzinfo=IST)
    candles = []
    # 25 base candles fed during engine cooldown
    for m in range(25):
        t = date_now + timedelta(minutes=m)
        c = OHLCVCandle(
            timestamp=t, open=24350.0, high=24365.0, low=24340.0, close=24355.0,
            volume=1000000, vwap=24355.0
        )
        candles.append(c)
        atm = ATMStrikes(
            spot_price=24355.0,
            ce=OptionRow(strike=24350, option_type="CE", iv=13.0),
            pe=OptionRow(strike=24350, option_type="PE", iv=13.0)
        )
        sig = engine.tick(c, full_chain=[], atm=atm)
        assert sig is None  # Gated by cooldown

    # Buffer must have continuously accumulated all 25 candles and IV data points
    assert len(engine.iv_breakout_detector.candle_history) == 25
    assert len(engine.iv_breakout_detector.iv_history) == 25
    assert engine.iv_breakout_detector.candle_history[-1].timestamp == candles[-1].timestamp
    assert engine.iv_breakout_detector.iv_history[-1][0] == candles[-1].timestamp

    # Advance cooldown clock to expire cooldown
    engine.last_signal_time = datetime.now() - timedelta(minutes=settings.signal_cooldown_minutes + 1)

    # Minute 25 consolidation candle
    t_25 = date_now + timedelta(minutes=25)
    c_25 = OHLCVCandle(t_25, 24350.0, 24365.0, 24340.0, 24355.0, 1000000, 24355.0)
    atm_25 = ATMStrikes(
        spot_price=24355.0,
        ce=OptionRow(strike=24350, option_type="CE", iv=13.0),
        pe=OptionRow(strike=24350, option_type="PE", iv=13.0),
    )
    sig_25 = engine.tick(c_25, full_chain=[], atm=atm_25)
    assert sig_25 is None
    assert len(engine.iv_breakout_detector.candle_history) == 26

    # Minute 26 breakout candle: base_candles must accurately evaluate recent consolidation
    t_bo = date_now + timedelta(minutes=26)
    c_bo = OHLCVCandle(t_bo, 24360.0, 24375.0, 24358.0, 24370.0, 1300000, 24356.0)
    atm_bo = ATMStrikes(
        spot_price=24370.0,
        ce=OptionRow(strike=24350, option_type="CE", iv=13.2),
        pe=OptionRow(strike=24350, option_type="PE", iv=13.2),
    )
    signal = engine.tick(c_bo, full_chain=[], atm=atm_bo)
    assert signal is not None
    assert signal.setup_type == SetupType.IV_GATED_BREAKOUT
    assert signal.trigger_price == 24370.0
    # Exactly 27 candles now, without duplicate appends for the breakout candle
    assert len(engine.iv_breakout_detector.candle_history) == 27


def test_iv_breakout_buffer_continuity_under_priority_short_circuiting():
    """Validates that IVBreakoutDetector maintains continuous buffer even when higher-priority detectors fire."""
    from models import OptionRow, ATMStrikes
    from unittest.mock import MagicMock
    engine = AresEngine()
    engine.last_signal_time = None

    t = datetime(2026, 11, 5, 10, 0, tzinfo=IST)
    c = OHLCVCandle(timestamp=t, open=24350.0, high=24380.0, low=24340.0, close=24375.0, volume=1500000, vwap=24360.0)
    atm = ATMStrikes(
        spot_price=24375.0,
        ce=OptionRow(strike=24350, option_type="CE", iv=13.5),
        pe=OptionRow(strike=24350, option_type="PE", iv=13.5),
    )

    # Force breakout_detector to trigger a higher-priority signal
    fake_breakout_signal = AresSignal(
        setup_type=SetupType.FAILED_BREAKOUT,
        direction=Direction.BULLISH,
        trigger_price=24375.0,
        entry_zone=(24370.0, 24380.0),
        stop_loss=24345.0,
        target_1=24410.0,
        target_2=24445.0,
        confidence="HIGH",
        reasons=["Breakout triggered"],
        timestamp=t,
        strike_to_trade=24350,
        option_type="CE",
    )
    engine.breakout_detector.update = MagicMock(return_value=fake_breakout_signal)

    sig = engine.tick(c, full_chain=[], atm=atm)
    # The higher priority breakout signal was returned
    assert sig == fake_breakout_signal

    # But IVBreakoutDetector still observed the tick and updated its state
    assert len(engine.iv_breakout_detector.candle_history) == 1
    assert engine.iv_breakout_detector.candle_history[-1] == c
    assert len(engine.iv_breakout_detector.iv_history) == 1
    assert engine.iv_breakout_detector.iv_history[-1] == (t, 13.5, 24350)


def test_iv_breakout_observe_idempotency_and_timestamp_deduplication():
    """Ensures observe() does not duplicate entries on identical timestamps or repeated calls."""
    detector = IVBreakoutDetector()
    t = datetime(2026, 11, 5, 10, 0, tzinfo=IST)
    c1 = OHLCVCandle(timestamp=t, open=24350.0, high=24360.0, low=24340.0, close=24355.0, volume=1000, vwap=24355.0)
    c2 = OHLCVCandle(timestamp=t, open=24350.0, high=24360.0, low=24340.0, close=24355.0, volume=1000, vwap=24355.0)

    detector.observe(c1, iv_current=14.0, strike=24350.0)
    # Re-call observe with same candle instance
    detector.observe(c1, iv_current=14.0, strike=24350.0)
    # Re-call observe with different candle instance but identical timestamp
    detector.observe(c2, iv_current=14.0, strike=24350.0)

    assert len(detector.candle_history) == 1
    assert len(detector.iv_history) == 1


def test_engine_tick_non_ohlcv_candle_safely_ignored_by_iv_breakout():
    """Validates that engine.tick with a non-OHLCVCandle does not invoke IVBreakoutDetector observe or update."""
    from unittest.mock import MagicMock
    from models import ATMStrikes
    engine = AresEngine()
    engine.iv_breakout_detector.observe = MagicMock()
    engine.iv_breakout_detector.update = MagicMock()
    mock_candle = MagicMock()  # not an instance of OHLCVCandle
    mock_candle.timestamp = datetime(2026, 11, 5, 10, 0, tzinfo=IST)
    mock_candle.volume = 1000
    atm = ATMStrikes(spot_price=24350.0, ce=None, pe=None)

    # Cooldown clear, no higher-priority detector signals
    engine.last_signal_time = None
    engine.breakout_detector.update = MagicMock(return_value=None)
    engine.oi_wall_detector.update = MagicMock(return_value=None)
    engine.trend_expansion_detector.update = MagicMock(return_value=None)
    engine.continuation_detector.update = MagicMock(return_value=None)

    engine.tick(mock_candle, full_chain=[], atm=atm)
    assert not engine.iv_breakout_detector.observe.called
    assert not engine.iv_breakout_detector.update.called
    assert len(engine.iv_breakout_detector.candle_history) == 0


def test_iv_breakout_session_reset_across_dates():
    """Ensures detector resets session state when a new trading date arrives."""
    detector = IVBreakoutDetector()
    t_day1 = datetime(2026, 11, 5, 15, 29, tzinfo=IST)
    c_day1 = OHLCVCandle(t_day1, 24350, 24400, 24340, 24390, 1000, 24370)
    detector.observe(c_day1, iv_current=14.0, strike=24350)
    assert detector.session_high == 24400.0
    assert detector.current_date == t_day1.date()

    t_day2 = datetime(2026, 11, 6, 9, 15, tzinfo=IST)
    c_day2 = OHLCVCandle(t_day2, 24500, 24520, 24490, 24510, 1000, 24505)
    detector.observe(c_day2, iv_current=14.5, strike=24500)
    assert detector.current_date == t_day2.date()
    assert detector.session_high == 24520.0
    assert len(detector.candle_history) == 1
    assert len(detector.iv_history) == 1


def test_iv_breakout_vwap_fallback_and_strike_inference():
    """Covers manual VWAP fallback when candle.vwap is None/zero and strike inference from iv_history."""
    detector = IVBreakoutDetector()
    date_now = datetime(2026, 11, 5, 9, 15, tzinfo=IST)

    # 25 base candles with vwap=0.0
    for m in range(25):
        t = date_now + timedelta(minutes=m)
        c = OHLCVCandle(t, 24350, 24365, 24340, 24355, 1000000, vwap=0.0)
        detector.update(c, avg_volume=1000000, iv_current=13.0, strike=24350.0)

    # 26th candle with vwap=0.0
    prior_time = date_now + timedelta(minutes=25)
    detector.update(
        OHLCVCandle(prior_time, 24350, 24365, 24340, 24355, 1000000, vwap=0.0),
        avg_volume=1000000,
        iv_current=13.0,
        strike=24350.0,
    )

    # Breakout candle: Step 1 observe records strike, then update is called without strike (inferred)
    t_bo = date_now + timedelta(minutes=26)
    c_bo = OHLCVCandle(t_bo, 24360, 24375, 24358, 24370, 1300000, vwap=0.0)
    detector.observe(c_bo, iv_current=13.2, strike=24350.0)
    sig = detector.update(c_bo, avg_volume=1000000, iv_current=13.2, strike=None)
    assert sig is not None
    assert sig.setup_type == SetupType.IV_GATED_BREAKOUT


def test_iv_breakout_fired_today_suppression():
    """Validates that once a signal fires for the day, subsequent breakout candidates are suppressed."""
    detector = IVBreakoutDetector()
    date_now = datetime(2026, 11, 5, 9, 15, tzinfo=IST)

    # 1. Feed 25 base candles
    for m in range(25):
        t = date_now + timedelta(minutes=m)
        c = OHLCVCandle(t, 24350.0, 24365.0, 24340.0, 24355.0, 1000000, 24355.0)
        detector.update(c, avg_volume=1000000, iv_current=13.0, strike=24350.0)

    # 2. Minute 25 consolidation candle
    t_25 = date_now + timedelta(minutes=25)
    c_25 = OHLCVCandle(t_25, 24350.0, 24365.0, 24340.0, 24355.0, 1000000, 24355.0)
    detector.update(c_25, avg_volume=1000000, iv_current=13.0, strike=24350.0)

    # 3. Minute 26 breakout candle emits signal
    t_bo = date_now + timedelta(minutes=26)
    c_bo = OHLCVCandle(t_bo, 24360.0, 24375.0, 24358.0, 24370.0, 1300000, 24356.0)
    sig1 = detector.update(c_bo, avg_volume=1000000, iv_current=13.2, strike=24350.0)
    assert sig1 is not None
    assert sig1.setup_type == SetupType.IV_GATED_BREAKOUT
    assert detector.fired_today is True

    # 4. Minute 27 another breakout candle on same day: must be suppressed by fired_today
    t_27 = date_now + timedelta(minutes=27)
    c_27 = OHLCVCandle(t_27, 24370.0, 24390.0, 24368.0, 24385.0, 1400000, 24357.0)
    sig2 = detector.update(c_27, avg_volume=1000000, iv_current=13.4, strike=24350.0)
    assert sig2 is None

    # Prove suppression was specifically due to fired_today:
    # If fired_today were False, the exact same candle would qualify as a breakout
    detector.fired_today = False
    sig_retry = detector.update(c_27, avg_volume=1000000, iv_current=13.4, strike=24350.0)
    assert sig_retry is not None
    assert sig_retry.setup_type == SetupType.IV_GATED_BREAKOUT


def test_iv_breakout_max_base_range_guard():
    """Validates that a breakout from an oversized base (> 55 pts) is rejected even when IV and volume qualify."""
    detector = IVBreakoutDetector()
    date_now = datetime(2026, 11, 5, 9, 15, tzinfo=IST)

    # 25 base candles with high=24420, low=24340 -> range = 80 pts (> 55 max_base_range_pts)
    for m in range(25):
        t_m = date_now + timedelta(minutes=m)
        c_m = OHLCVCandle(t_m, 24350.0, 24420.0, 24340.0, 24360.0, 1000000, 24360.0)
        detector.update(c_m, avg_volume=1000000, iv_current=13.0, strike=24350.0)

    # Minute 25 consolidation candle providing fresh 1-minute IV sample
    t_25 = date_now + timedelta(minutes=25)
    c_25 = OHLCVCandle(t_25, 24350.0, 24420.0, 24340.0, 24360.0, 1000000, 24360.0)
    detector.update(c_25, avg_volume=1000000, iv_current=13.0, strike=24350.0)

    # Minute 26 breakout candle breaking above base_high (24420) + 2.0 with surge volume and expanding IV
    t_bo = date_now + timedelta(minutes=26)
    c_bo = OHLCVCandle(t_bo, 24410.0, 24430.0, 24405.0, 24425.0, 1300000, 24361.0)
    sig = detector.update(c_bo, avg_volume=1000000, iv_current=13.2, strike=24350.0)

    # Must be rejected strictly because base_range (80 pts) > 55 pts
    assert sig is None

    # Prove that with normal base range (<= 55 pts) but otherwise identical setup, it fires
    normal_detector = IVBreakoutDetector()
    for m in range(25):
        t_m = date_now + timedelta(minutes=m)
        # range = 24365 - 24340 = 25 pts (<= 55 pts)
        c_norm = OHLCVCandle(t_m, 24350.0, 24365.0, 24340.0, 24355.0, 1000000, 24355.0)
        normal_detector.update(c_norm, avg_volume=1000000, iv_current=13.0, strike=24350.0)

    t_norm_25 = date_now + timedelta(minutes=25)
    c_norm_25 = OHLCVCandle(t_norm_25, 24350.0, 24365.0, 24340.0, 24355.0, 1000000, 24355.0)
    normal_detector.update(c_norm_25, avg_volume=1000000, iv_current=13.0, strike=24350.0)

    t_norm_bo = date_now + timedelta(minutes=26)
    c_norm_bo = OHLCVCandle(t_norm_bo, 24360.0, 24375.0, 24358.0, 24370.0, 1300000, 24356.0)
    sig_norm = normal_detector.update(c_norm_bo, avg_volume=1000000, iv_current=13.2, strike=24350.0)
    assert sig_norm is not None
    assert sig_norm.setup_type == SetupType.IV_GATED_BREAKOUT


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


@pytest.mark.asyncio
async def test_iv_breakout_end_to_end_ml_prediction_and_collection():
    """End-to-End Test: Validates IV_GATED_BREAKOUT works through ML Predictor and Collector."""
    import json
    from unittest.mock import MagicMock
    from ml_signal.predictor import SignalPredictor
    from ml_signal.collector import MLCollector
    from models import OptionRow, ATMStrikes, ResistanceLevel

    # 1. Initialize ML Predictor and Load Model
    predictor = SignalPredictor()
    predictor.load_model()
    assert predictor.model is not None

    # 2. Simulate AresEngine generating an IV_GATED_BREAKOUT signal
    engine = AresEngine()
    date_now = datetime(2026, 11, 5, 9, 15, tzinfo=IST)

    # Establish 25-candle base (09:15-09:39) with high at 24365.0
    for m in range(0, 25):
        t = date_now + timedelta(minutes=m)
        c = OHLCVCandle(
            timestamp=t, open=24350.0, high=24365.0, low=24340.0, close=24355.0,
            volume=1000000, vwap=24352.0
        )
        atm = ATMStrikes(
            spot_price=24355.0,
            ce=OptionRow(strike=24350, option_type="CE", iv=13.0, oi=5000000, delta=0.52),
            pe=OptionRow(strike=24350, option_type="PE", iv=13.0, oi=4000000, delta=-0.48)
        )
        engine.tick(c, full_chain=[], atm=atm)

    prior_time = date_now + timedelta(minutes=25)
    prior_candle = OHLCVCandle(
        timestamp=prior_time, open=24350.0, high=24365.0, low=24340.0,
        close=24355.0, volume=1000000, vwap=24352.0,
    )
    prior_atm = ATMStrikes(
        spot_price=24355.0,
        ce=OptionRow(strike=24400, option_type="CE", iv=13.1, oi=5000000, delta=0.52),
        pe=OptionRow(strike=24400, option_type="PE", iv=13.1, oi=4000000, delta=-0.48),
    )
    engine.tick(prior_candle, full_chain=[], atm=prior_atm)

    # Breakout candle at minute 26 (09:41 AM) breaking 24365.0 base high
    t_bo = date_now + timedelta(minutes=26)
    c_bo = OHLCVCandle(
        timestamp=t_bo, open=24365.0, high=24410.0, low=24360.0, close=24405.0,
        volume=1500000, vwap=24370.0
    )
    engine.iv_breakout_detector.iv_history.append(
        (t_bo - timedelta(minutes=3), 13.0, 24400.0)
    )

    levels = [ResistanceLevel(price=24500.0, source="OI_WALL", strength=3), ResistanceLevel(price=24350.0, source="SWING_LOW", strength=2)]
    atm_bo = ATMStrikes(
        spot_price=24405.0,
        ce=OptionRow(strike=24400, option_type="CE", iv=13.2, oi=6000000, delta=0.55, gamma=0.002, theta=-12.0, vega=8.0),
        pe=OptionRow(strike=24400, option_type="PE", iv=13.0, oi=4000000, delta=-0.45, gamma=0.002, theta=-11.0, vega=7.5)
    )

    signal = engine.tick(c_bo, full_chain=[], atm=atm_bo, levels=levels)
    assert signal is not None
    assert signal.setup_type == SetupType.IV_GATED_BREAKOUT
    assert signal.direction == Direction.BULLISH

    # 3. Predict from Raw Market Features (Same call pattern as main.py)
    candle_dict = MLCollector._candle_to_dict(c_bo)
    atm_ce_dict = MLCollector._option_row_to_dict(atm_bo.ce)
    atm_pe_dict = MLCollector._option_row_to_dict(atm_bo.pe)
    level_prices = MLCollector._levels_to_prices(levels)

    pred = predictor.predict_from_raw(
        candle=candle_dict,
        volume_history=[1000000, 1200000, 1500000],
        iv_history=[12.8, 13.0, 13.2],
        atm_ce=atm_ce_dict,
        atm_pe=atm_pe_dict,
        total_ce_oi=20000000,
        total_pe_oi=18000000,
        all_ce_oi=[6000000, 5000000],
        all_pe_oi=[4000000, 3000000],
        levels=level_prices,
        timestamp=t_bo,
        spot=24405.0,
        pdh=24450.0,
        pdl=24300.0,
        dte=5,
        is_expiry=False,
        direction=signal.direction.value
    )

    # Validate ML Prediction Outputs
    assert "probability" in pred
    assert 0.0 <= pred["probability"] <= 1.0
    assert pred["confidence_tier"] in ("HIGH", "MEDIUM", "LOW")
    assert "features" in pred
    feats = pred["features"]
    assert feats["structure_features__dist_to_target_wall"] == pytest.approx(95.0)  # 24500 - 24405
    assert feats["candle_features__aligns_direction"] == 1.0
    assert feats["greek_features__aligned_net_delta"] == pytest.approx(0.10)  # 0.55 - 0.45

    # 4. Attach prediction to signal (main.py pattern)
    from main import _attach_ml_prediction_to_signal
    _attach_ml_prediction_to_signal(signal, pred)
    assert signal.ml_prediction == pred

    # 5. ML Data Collection Snapshot Persistence
    collector = MLCollector("https://dummy.supabase.co", "dummy_key")
    mock_supabase = MagicMock()
    collector.supabase = mock_supabase
    mock_table = MagicMock()
    mock_supabase.table.return_value = mock_table

    saved_records = []
    def fake_upsert(record, **kwargs):
        saved_records.append(record)
        m = MagicMock()
        m.execute.return_value.data = [record]
        return m
    mock_table.upsert.side_effect = fake_upsert

    await collector.snapshot(
        signal=signal,
        candle=c_bo,
        atm=atm_bo,
        full_chain=[],
        levels=levels,
        spot=24405.0,
        pdh=24450.0,
        pdl=24300.0,
        is_expiry=False,
        dte=5,
        timestamp=t_bo,
    )

    assert len(saved_records) == 1
    rec = saved_records[0]
    assert rec["signal_setup_type"] == "IV_GATED_BREAKOUT"
    assert rec["signal_direction"] == "BULLISH"
    scores = json.loads(rec["detector_scores"])
    assert scores["iv_gated_breakout"] == 1
    assert scores["failed_breakout"] == 0
    assert scores["oi_wall_rejection"] == 0


def test_iv_breakout_strike_roll_without_baseline_fails_closed():
    """A new ATM strike cannot use the old strike as its IV comparison baseline."""
    detector = IVBreakoutDetector()
    date_now = datetime(2026, 11, 5, 9, 15, tzinfo=IST)

    # 1. 25 base candles around 24350 with ATM CE strike 24350 (IV 14.5)
    for m in range(25):
        t = date_now + timedelta(minutes=m)
        c = OHLCVCandle(
            timestamp=t,
            open=24350.0 + (m % 5) - 2,
            high=24365.0,
            low=24340.0,
            close=24350.0 + (m % 7) - 3,
            volume=1000000,
            vwap=24352.0
        )
        detector.update(c, avg_volume=1000000, iv_current=14.5, strike=24350.0)

    # 2. Breakout candle at minute 26 (09:41 AM) with a new ATM strike.
    t_bo = date_now + timedelta(minutes=26)
    c_bo = OHLCVCandle(
        timestamp=t_bo,
        open=24360.0,
        high=24375.0,
        low=24358.0,
        close=24368.0,
        volume=1300000,
        vwap=24360.0
    )

    sig = detector.update(c_bo, avg_volume=1000000, iv_current=13.8, strike=24400.0)

    assert sig is None
    assert detector.latest_rejection_reason == "NO_IV_DATA"

    detector.iv_history.append((t_bo - timedelta(minutes=3), 13.7, 24400.0))
    detector.iv_history.append((t_bo - timedelta(minutes=1), 13.8, 24400.0))
    sig = detector.update(c_bo, avg_volume=1000000, iv_current=13.8, strike=24400.0)

    assert sig is not None
    assert sig.setup_type == SetupType.IV_GATED_BREAKOUT


def test_iv_breakout_missing_iv_fails_closed():
    """Validates that missing, non-finite, or non-positive IV suppresses breakout signal with NO_IV_DATA reason."""
    detector = IVBreakoutDetector()
    date_now = datetime(2026, 11, 5, 9, 15, tzinfo=IST)

    # Establish base
    for m in range(25):
        t = date_now + timedelta(minutes=m)
        c = OHLCVCandle(
            timestamp=t,
            open=24350.0 + (m % 5) - 2,
            high=24365.0,
            low=24340.0,
            close=24350.0 + (m % 7) - 3,
            volume=1000000,
            vwap=24352.0,
        )
        detector.update(c, avg_volume=1000000, iv_current=14.5, strike=24350.0)

    # Breakout candle at minute 26
    t_bo = date_now + timedelta(minutes=26)
    c_bo = OHLCVCandle(
        timestamp=t_bo,
        open=24360.0,
        high=24375.0,
        low=24358.0,
        close=24368.0,
        volume=1300000,
        vwap=24360.0,
    )

    # 1. Test None IV
    sig = detector.update(c_bo, avg_volume=1000000, iv_current=None, strike=24350.0)
    assert sig is None
    assert detector.latest_rejection_reason == "NO_IV_DATA"

    # 2. Test 0.0 IV
    sig_zero = detector.update(c_bo, avg_volume=1000000, iv_current=0.0, strike=24350.0)
    assert sig_zero is None
    assert detector.latest_rejection_reason == "NO_IV_DATA"

    # 3. Test NaN / inf IV
    sig_nan = detector.update(c_bo, avg_volume=1000000, iv_current=float("nan"), strike=24350.0)
    assert sig_nan is None
    assert detector.latest_rejection_reason == "NO_IV_DATA"
