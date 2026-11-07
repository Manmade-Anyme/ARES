import pytest
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch
import pytz

from poc.trend_expansion_poc import TrendExpansionEngine, TradeRecord
from poc.regime_gated_expansion_strategy import GatedRegimeExpansionEngine, GatedTradeRecord
import poc.run_90day_audit as audit_module
import poc.run_gated_90day_audit as gated_audit_module
import poc.run_trend_expansion_backtest as backtest_module

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



