import pytest
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
import pytz

from poc.trend_expansion_poc import TrendExpansionEngine, TradeRecord
from poc.regime_gated_expansion_strategy import (
    GatedRegimeExpansionEngine,
    GatedTradeRecord,
    RegimeThresholds,
)
import poc.run_90day_audit as audit_module
import poc.run_gated_90day_audit as gated_audit_module
import poc.run_trend_expansion_backtest as backtest_module
from poc.position_management import ReclaimSetupState, manage_long_position

IST = pytz.timezone("Asia/Kolkata")


def generate_mock_session_df():
    """Generates synthetic 1-minute candle dataframe for a trend day."""
    start_dt = datetime(2026, 11, 5, 9, 15, tzinfo=IST)
    rows = []
    
    price = 24300.0
    for i in range(375):
        current_dt = start_dt + timedelta(minutes=i)
        time_str = current_dt.strftime("%H:%M")
        
        # Morning impulse 09:15 to 10:00: rally to 24420
        if time_str <= "10:00":
            price += 2.5
        # Retracement 10:01 to 10:45: flush to 24350
        elif time_str <= "10:45":
            price -= 1.6
        # Rebound 10:46 to 12:30: back above 24400
        elif time_str <= "12:30":
            price += 0.5
        # Afternoon breakout 12:31 to 14:00: rally to 24550
        elif time_str <= "14:00":
            price += 1.8
        # Climax/consolidation 14:01 to 15:30
        else:
            price -= 0.2

        high = price + 2.0
        low = price - 2.0
        open_p = price - 0.5
        close_p = price + 0.5
        vol = 1000000

        rows.append({
            "timestamp": int(current_dt.timestamp()),
            "dt": current_dt,
            "time_str": time_str,
            "open": open_p,
            "high": high,
            "low": low,
            "close": close_p,
            "volume": vol
        })

    return pd.DataFrame(rows)


def test_compute_indicators():
    engine = TrendExpansionEngine()
    df = generate_mock_session_df()
    enriched = engine.compute_indicators(df)

    assert "vwap" in enriched.columns
    assert "ema9" in enriched.columns
    assert "ema20" in enriched.columns
    assert "atr14" in enriched.columns
    assert not enriched["vwap"].isna().all()


def test_session_execution_generates_trades():
    engine = TrendExpansionEngine()
    df = generate_mock_session_df()
    trades = engine.run_session(df, "2026-11-05")

    assert isinstance(trades, list)
    assert len(trades) > 0
    first_trade = trades[0]
    assert isinstance(first_trade, TradeRecord)
    assert first_trade.strategy in ["VWAP_RECLAIM", "AFTERNOON_BREAKOUT"]
    assert first_trade.exit_price is not None
    assert first_trade.exit_reason in ["SL_HIT", "EMA20_TRAIL", "EOD_CLOSE"]


def test_run_90day_audit_annotations():
    """Verify Dict and List annotations in audit module resolve cleanly without NameError."""
    annotations = audit_module.load_all_cached_sessions.__annotations__
    assert "return" in annotations


def test_afternoon_breakout_volume_confirmation():
    """Verify that low-volume afternoon breakouts are rejected when volume < vol_sma20."""
    engine = TrendExpansionEngine(min_volume_ratio=1.0)
    df = generate_mock_session_df()

    # Suppress volume on all afternoon breakout candles post 12:30
    df_low_vol = df.copy()
    afternoon_mask = df_low_vol["time_str"] > "12:30"
    df_low_vol.loc[afternoon_mask, "volume"] = 100  # low volume

    trades = engine.run_session(df_low_vol, "2026-11-05")
    breakout_trades = [t for t in trades if t.strategy == "AFTERNOON_BREAKOUT"]
    assert len(breakout_trades) == 0


def test_gated_engine_vwap_dispersion_around_session_mean():
    """Verify that VWAP dispersion is computed around current session VWAP, not historical running means."""
    engine = GatedRegimeExpansionEngine()
    test_df = pd.DataFrame({
        "timestamp": [1000, 1060, 1120],
        "time_str": ["09:15", "09:16", "09:17"],
        "open": [100.0, 105.0, 110.0],
        "high": [101.0, 106.0, 111.0],
        "low": [99.0, 104.0, 109.0],
        "close": [100.0, 105.0, 110.0],
        "volume": [1000, 1500, 2000]
    })
    enriched = engine.enrich_session(test_df)

    # Brute-force calculate session standard deviation around current VWAP at final candle
    w = test_df["volume"].values
    tp = enriched["typical_price"].values
    final_vwap = enriched["vwap"].iloc[-1]
    expected_var = np.sum(w * ((tp - final_vwap) ** 2)) / np.sum(w)
    expected_std = np.sqrt(expected_var)

    actual_std = enriched["vwap_std"].iloc[-1]
    assert np.isclose(actual_std, expected_std, rtol=1e-5)


def test_gated_afternoon_bbw_expansion_filter():
    """Verify that afternoon breakout is gated by BBW expansion ratio >= min_bbw_expansion."""
    engine = GatedRegimeExpansionEngine(min_bbw_expansion=1.5)
    df = generate_mock_session_df()

    # Pre-market context enabling regime qualification
    pdc = 24200.0  # +100 gap extension factor
    pdh = 24250.0
    pdl = 24150.0

    # With high min_bbw_expansion (1.5), afternoon breakout should be filtered
    trades = engine.run_session(df, "2026-11-05", pdc, pdh, pdl)
    breakout_trades = [t for t in trades if t.strategy == "AFTERNOON_BAND_EXPANSION"]
    assert len(breakout_trades) == 0


def test_supabase_audit_query_bounded():
    """Verify that Supabase queries are bounded to both start_date and end_date."""
    mock_supabase = MagicMock()
    mock_table = MagicMock()
    mock_select = MagicMock()
    mock_gte = MagicMock()
    mock_lte = MagicMock()

    mock_supabase.table.return_value = mock_table
    mock_table.select.return_value = mock_select
    mock_select.gte.return_value = mock_gte
    mock_gte.lte.return_value = mock_lte
    mock_lte.execute.return_value.data = [
        {"entry_timestamp": "2026-08-08T10:00:00+05:30", "pnl_points": 10.0, "setup_type": "TEST", "result_state": "WIN"},
        {"entry_timestamp": "2026-11-05T14:00:00+05:30", "pnl_points": 25.0, "setup_type": "TEST", "result_state": "WIN"},
        # Outside window (after 2026-11-05):
        {"entry_timestamp": "2026-11-06T10:00:00+05:30", "pnl_points": -50.0, "setup_type": "TEST", "result_state": "LOSS"}
    ]

    with patch("poc.run_gated_90day_audit.create_client", return_value=mock_supabase), \
         patch("poc.run_gated_90day_audit.storage.load_dhan_credentials_from_supabase"):
        df = gated_audit_module.load_supabase_ares_trades(start_date="2026-08-07", end_date="2026-11-05")

        mock_select.gte.assert_called_with("entry_timestamp", "2026-08-07")
        mock_gte.lte.assert_called_with("entry_timestamp", "2026-11-05T23:59:59")
        # Ensure post-filter excluded the trade on 2026-11-06
        assert len(df) == 2
        assert "2026-11-06" not in df["date"].values

    # Also test audit_module.load_supabase_ares_trades bounding
    with patch("poc.run_90day_audit.create_client", return_value=mock_supabase), \
         patch("poc.run_90day_audit.storage.load_dhan_credentials_from_supabase"):
        df_raw = audit_module.load_supabase_ares_trades(start_date="2026-08-07", end_date="2026-11-05")
        assert len(df_raw) == 2
        assert "2026-11-06" not in df_raw["date"].values

    # Test backtest_module.load_supabase_ares_pnl with until_date
    mock_supabase_backtest = MagicMock()
    mock_tbl = MagicMock()
    mock_sel = MagicMock()
    mock_gte_b = MagicMock()
    mock_lte_b = MagicMock()
    mock_supabase_backtest.table.return_value = mock_tbl
    mock_tbl.select.return_value = mock_sel
    mock_sel.gte.return_value = mock_gte_b
    mock_gte_b.lte.return_value = mock_lte_b
    mock_lte_b.execute.return_value.data = [
        {"entry_timestamp": "2026-08-08T10:00:00+05:30", "pnl_points": 10.0},
        {"entry_timestamp": "2026-11-05T14:00:00+05:30", "pnl_points": 25.0},
        {"entry_timestamp": "2026-11-06T10:00:00+05:30", "pnl_points": -50.0}
    ]
    with patch("poc.run_trend_expansion_backtest.create_client", return_value=mock_supabase_backtest):
        pnl_map = backtest_module.load_supabase_ares_pnl("2026-08-07", until_date="2026-11-05")
        mock_gte_b.lte.assert_called_with("entry_timestamp", "2026-11-05T23:59:59")
        assert "2026-11-06" not in pnl_map
        assert "2026-11-05" in pnl_map


def test_gated_afternoon_bbw_expansion_triggered_when_ratio_sufficient():
    """Verify that afternoon breakout triggers when BBW expansion ratio >= min_bbw_expansion."""
    engine = GatedRegimeExpansionEngine(min_bbw_expansion=0.5)
    df = generate_mock_session_df()

    pdc = 24200.0  # +100 gap extension factor
    pdh = 24250.0
    pdl = 24150.0

    trades = engine.run_session(df, "2026-11-05", pdc, pdh, pdl)
    assert len(trades) > 0
    first_trade = trades[0]
    assert isinstance(first_trade, GatedTradeRecord)
    assert first_trade.bbw_expansion >= 0.0


def test_vwap_reclaim_retracement_bounds():
    """Verify that VWAP reclaim enforces both min and max retracement ratio bounds."""
    # min_retrace_ratio = 0.35, max_retrace_ratio = 0.68
    engine = TrendExpansionEngine(min_retrace_ratio=0.35, max_retrace_ratio=0.68)

    # Base candle template
    start_dt = datetime(2026, 11, 5, 9, 15, tzinfo=IST)
    def create_session(sweep_price: float):
        rows = []
        for i in range(375):  # 09:15 to 15:30
            cur_dt = start_dt + timedelta(minutes=i)
            t = cur_dt.strftime("%H:%M")
            # Morning impulse: 24300 to 24400 (impulse_len = 100)
            if t <= "10:15":
                p = 24300.0 + (i / 60.0) * 100.0
            # Pullback to sweep_price between 10:16 and 11:00
            elif t <= "11:00":
                p = sweep_price
            # Reclaim VWAP (> 24350) with upward momentum
            else:
                p = 24370.0

            rows.append({
                "timestamp": int(cur_dt.timestamp()),
                "dt": cur_dt,
                "time_str": t,
                "open": p - 0.5,
                "high": p + 1.0,
                "low": p - 1.0,
                "close": p + 0.5,
                "volume": 500000
            })
        return pd.DataFrame(rows)

    # Case 1: Shallow pullback: sweep = 24380 (retrace = 20 pts, 20% < 35%) -> NO TRADE
    df_shallow = create_session(sweep_price=24380.0)
    trades_shallow = engine.run_session(df_shallow, "2026-11-05")
    reclaims_shallow = [t for t in trades_shallow if t.strategy == "VWAP_RECLAIM"]
    assert len(reclaims_shallow) == 0

    # Case 2: Deep breakdown: sweep = 24320 (retrace = 80 pts, 80% > 68%) -> NO TRADE
    df_deep = create_session(sweep_price=24320.0)
    trades_deep = engine.run_session(df_deep, "2026-11-05")
    reclaims_deep = [t for t in trades_deep if t.strategy == "VWAP_RECLAIM"]
    assert len(reclaims_deep) == 0

    # Case 3: Golden zone pullback: sweep = 24350 (retrace = 50 pts, 50% in [35%, 68%]) -> TRIGGERS
    df_valid = create_session(sweep_price=24350.0)
    trades_valid = engine.run_session(df_valid, "2026-11-05")
    reclaims_valid = [t for t in trades_valid if t.strategy == "VWAP_RECLAIM"]
    assert len(reclaims_valid) == 1
    assert reclaims_valid[0].entry_price > 24360.0


def test_sweep_low_reset_on_breakdown():
    """Verify that sweep_low and dipped_below_vwap reset if price flushes beneath morning low."""
    engine = TrendExpansionEngine()
    start_dt = datetime(2026, 11, 5, 9, 15, tzinfo=IST)
    rows = []
    for i in range(200):
        cur_dt = start_dt + timedelta(minutes=i)
        t = cur_dt.strftime("%H:%M")
        # Morning impulse: open 24300, low 24300, high 24400
        if t <= "10:15":
            p = 24300.0 + (i / 60.0) * 100.0
        # Severe crash below morning low: 24280 (< 24300 m_low)
        elif t <= "10:45":
            p = 24280.0
        # Rebound back above VWAP
        else:
            p = 24360.0

        rows.append({
            "timestamp": int(cur_dt.timestamp()),
            "dt": cur_dt,
            "time_str": t,
            "open": p - 0.5,
            "high": p + 1.0,
            "low": p - 1.0,
            "close": p + 0.5,
            "volume": 500000
        })
    df = pd.DataFrame(rows)
    trades = engine.run_session(df, "2026-11-05")
    reclaims = [t for t in trades if t.strategy == "VWAP_RECLAIM"]
    # Must NOT enter because morning low was violated
    assert len(reclaims) == 0


def test_load_cached_sessions_and_pdl_context_isolation():
    """Verify context-only session is excluded from audited sessions in load_cached_sessions_and_pdl."""
    mock_files = [
        "scratch/data_cache/2026-08-07.parquet",
        "scratch/data_cache/2026-08-10.parquet",
        "scratch/data_cache/2026-08-11.parquet",
    ]
    mock_df = pd.DataFrame({
        "timestamp": [1700000000],
        "open": [24000.0],
        "high": [24100.0],
        "low": [23900.0],
        "close": [24050.0],
        "volume": [1000]
    })

    with patch("poc.run_gated_90day_audit.glob.glob", return_value=mock_files), \
         patch("poc.run_gated_90day_audit.pd.read_parquet", side_effect=lambda f: mock_df.copy()):
        # Requesting audit window from 2026-08-10 to 2026-08-11
        sessions, pdl_map = gated_audit_module.load_cached_sessions_and_pdl(start_date="2026-08-10", end_date="2026-08-11")

        # 2026-08-07 should NOT be in sessions or pdl_map
        assert "2026-08-07" not in sessions
        assert "2026-08-07" not in pdl_map

        # 2026-08-10 should be in sessions and have 2026-08-07 close (24050.0) as pdc
        assert "2026-08-10" in sessions
        assert "2026-08-10" in pdl_map
        assert pdl_map["2026-08-10"]["pdc"] == 24050.0

        # 2026-08-11 should be in sessions
        assert "2026-08-11" in sessions


def test_load_all_cached_sessions_date_filtering():
    """Verify load_all_cached_sessions restricts files strictly to [start_date, end_date]."""
    mock_files = [
        "scratch/data_cache/2026-08-01.parquet",
        "scratch/data_cache/2026-08-10.parquet",
        "scratch/data_cache/2026-11-10.parquet",
    ]
    mock_df = pd.DataFrame({
        "timestamp": [1700000000],
        "open": [24000.0],
        "high": [24100.0],
        "low": [23900.0],
        "close": [24050.0],
        "volume": [1000]
    })

    with patch("poc.run_90day_audit.glob.glob", return_value=mock_files), \
         patch("poc.run_90day_audit.pd.read_parquet", side_effect=lambda f: mock_df.copy()):
        sessions = audit_module.load_all_cached_sessions(start_date="2026-08-07", end_date="2026-11-05")
        assert "2026-08-01" not in sessions
        assert "2026-11-10" not in sessions
        assert "2026-08-10" in sessions


def test_ratcheted_stop_waits_until_candle_after_ambiguous_target_candle():
    """A same-candle T1/ratcheted-stop overlap cannot establish intrabar event order."""
    engine = TrendExpansionEngine()
    start_dt = datetime(2026, 11, 5, 9, 15, tzinfo=IST)
    rows = []

    # 1. Morning impulse (09:15-10:15): 24300 to 24400 (impulse = 100 pts)
    for i in range(61):
        cur_dt = start_dt + timedelta(minutes=i)
        t = cur_dt.strftime("%H:%M")
        p = 24300.0 + (i / 60.0) * 100.0
        rows.append({
            "timestamp": int(cur_dt.timestamp()), "dt": cur_dt, "time_str": t,
            "open": p - 0.5, "high": p + 1.0, "low": p - 1.0, "close": p + 0.5, "volume": 500000
        })

    # 2. Pullback below VWAP (10:16-10:45): sweep to 24340 (retrace 60 pts, 60% in [35%, 68%])
    for i in range(61, 91):
        cur_dt = start_dt + timedelta(minutes=i)
        t = cur_dt.strftime("%H:%M")
        p = 24340.0
        rows.append({
            "timestamp": int(cur_dt.timestamp()), "dt": cur_dt, "time_str": t,
            "open": p - 0.5, "high": p + 1.0, "low": p - 1.0, "close": p + 0.5, "volume": 500000
        })

    # 3. Entry candle at 10:46: reclaim VWAP at close 24365
    cur_dt = start_dt + timedelta(minutes=91)
    t = cur_dt.strftime("%H:%M")
    rows.append({
        "timestamp": int(cur_dt.timestamp()), "dt": cur_dt, "time_str": t,
        "open": 24355.0, "high": 24368.0, "low": 24350.0, "close": 24365.0, "volume": 800000
    })

    # 4. Ambiguous spike candle at 10:47: High hits T1 (24405 >= 24400) and
    # Low crosses the newly ratcheted stop (24360 <= 24367), but not the old stop.
    # Note: Initial SL = sweep_low - 3.0 = 24339.0 - 3.0 = 24336.0 (Low 24360 > 24336)
    # Ratcheted SL = entry_price + 2.0 = 24365.0 + 2.0 = 24367.0
    cur_dt = start_dt + timedelta(minutes=92)
    t = cur_dt.strftime("%H:%M")
    rows.append({
        "timestamp": int(cur_dt.timestamp()), "dt": cur_dt, "time_str": t,
        "open": 24370.0, "high": 24410.0, "low": 24360.0, "close": 24390.0, "volume": 1200000
    })

    # 5. The ratcheted stop is effective from the next candle.
    cur_dt = start_dt + timedelta(minutes=93)
    t = cur_dt.strftime("%H:%M")
    rows.append({
        "timestamp": int(cur_dt.timestamp()), "dt": cur_dt, "time_str": t,
        "open": 24390.0, "high": 24392.0, "low": 24360.0, "close": 24365.0, "volume": 500000
    })

    # Remaining day flat to 15:30
    for i in range(94, 375):
        cur_dt = start_dt + timedelta(minutes=i)
        t = cur_dt.strftime("%H:%M")
        rows.append({
            "timestamp": int(cur_dt.timestamp()), "dt": cur_dt, "time_str": t,
            "open": 24390.0, "high": 24392.0, "low": 24388.0, "close": 24390.0, "volume": 200000
        })

    df = pd.DataFrame(rows)
    trades = engine.run_session(df, "2026-11-05")

    assert len(trades) == 1
    trade = trades[0]
    assert trade.t1_hit is True
    assert trade.t1_time == "10:47"
    assert trade.exit_time == "10:48"
    assert trade.exit_reason == "SL_HIT"
    assert trade.exit_price == trade.entry_price + 2.0
    assert trade.pnl_points == 2.0

    # Both engines use the same conservative lifecycle rule.
    gated_engine = GatedRegimeExpansionEngine(min_regime_score=0)
    gated_trades = gated_engine.run_session(df, "2026-11-05", pdc=24200.0, pdh=24400.0, pdl=24150.0)
    assert len(gated_trades) == 1
    gated_trade = gated_trades[0]
    assert gated_trade.t1_hit is True
    assert gated_trade.t1_time == "10:47"
    assert gated_trade.exit_time == "10:48"
    assert gated_trade.exit_reason == "SL_HIT"
    assert gated_trade.exit_price == gated_trade.entry_price + 2.0
    assert gated_trade.pnl_points == 2.0


def test_gated_regime_thresholds_are_configurable():
    """The four regime factor thresholds can be tuned without changing defaults."""
    df = generate_mock_session_df()
    assert RegimeThresholds() == RegimeThresholds(70.0, 85.0, 25.0, 45.0)
    thresholds = RegimeThresholds(
        gap_extension_pts=1000.0,
        opening_range_pts=1000.0,
        vwap_dispersion_pts=1000.0,
        directional_displacement_pts=1000.0,
    )
    engine = GatedRegimeExpansionEngine(regime_thresholds=thresholds)

    is_expansion, score, factors, _ = engine.evaluate_regime(
        df, pdc=df.iloc[0]["open"], pdh=24400.0, pdl=24150.0
    )

    assert engine.regime_thresholds == thresholds
    assert score == 0
    assert is_expansion is False
    assert not any(factors.values())


def test_ema20_trailing_exit_resets_reclaim_setup_state():
    trade = SimpleNamespace(
        entry_price=100.0,
        stop_loss=102.0,
        target_1=110.0,
        t1_hit=True,
        t1_time="12:00",
        exit_time=None,
        exit_price=None,
        exit_reason=None,
        pnl_points=0.0,
    )
    candle = pd.Series({
        "time_str": "13:30",
        "low": 103.0,
        "high": 105.0,
        "close": 99.0,
        "ema20": 100.0,
    })
    reclaim_state = ReclaimSetupState(dipped_below_vwap=True, sweep_low=95.0)

    result = manage_long_position(
        trade, candle, candle_index=20, cooldown_until_idx=0,
        eod_exit_time="15:15", reclaim_state=reclaim_state,
    )

    assert result.exited is True
    assert result.cooldown_until_idx == 30
    assert trade.exit_reason == "EMA20_TRAIL"
    assert (reclaim_state.dipped_below_vwap, reclaim_state.sweep_low) == (False, 999999.0)


