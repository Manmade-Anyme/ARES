import pytest
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
import pytz

from poc.iv_breakout_expansion_poc import (
    IVGatedBreakoutEngine,
    IVBreakoutTradeRecord,
    RejectedBreakoutRecord
)

IST = pytz.timezone("Asia/Kolkata")


def generate_mock_session_spot_and_iv():
    """Generates synthetic 1-minute Spot and IV data for a breakout day."""
    start_dt = datetime(2026, 11, 5, 9, 15, tzinfo=IST)
    spot_rows = []
    iv_rows = []

    spot = 24300.0
    iv = 12.0

    for i in range(375):
        current_dt = start_dt + timedelta(minutes=i)
        time_str = current_dt.strftime("%H:%M")

        if time_str <= "10:00":
            spot += 2.0
            iv += 0.02
        elif time_str <= "12:30":
            spot -= 0.5
            iv -= 0.01  # Midday IV compression
        elif time_str <= "14:00":
            spot += 2.5  # Afternoon breakout
            iv += 0.05   # IV expansion
        else:
            spot -= 0.5
            iv -= 0.04

        spot_rows.append({
            "timestamp": int(current_dt.timestamp()),
            "dt": current_dt,
            "time_str": time_str,
            "open": spot - 1.0,
            "high": spot + 2.0,
            "low": spot - 2.0,
            "close": spot + 1.0,
            "volume": 1500000
        })

        iv_rows.append({
            "timestamp": int(current_dt.timestamp()),
            "dt": current_dt,
            "time_str": time_str,
            "spot": spot,
            "strike": 24400.0,
            "open": 100.0,
            "high": 105.0,
            "low": 98.0,
            "close": 102.0,
            "iv": iv,
            "volume": 2000000
        })

    return pd.DataFrame(spot_rows), pd.DataFrame(iv_rows)


def test_spot_enrichment():
    engine = IVGatedBreakoutEngine()
    df_s, _ = generate_mock_session_spot_and_iv()
    enriched = engine.enrich_spot_session(df_s)

    assert "vwap" in enriched.columns
    assert "vwap_zscore" in enriched.columns
    assert "ema9" in enriched.columns
    assert "ema20" in enriched.columns
    assert not enriched["vwap"].isna().all()


def test_iv_filter_vol_crush_rejection():
    engine = IVGatedBreakoutEngine()
    _, df_iv = generate_mock_session_spot_and_iv()

    # Induce a sharp vol-crush right at 13:00
    idx_1300 = df_iv[df_iv["time_str"] == "13:00"].index[0]
    df_iv.loc[idx_1300, "iv"] = 10.50  # Sudden -150 bps crash

    passed, reason, metrics = engine.evaluate_iv_filter("13:00", 1.2, df_iv)

    assert not passed
    assert reason == "VOL_CRUSH_BULL_TRAP"
    assert metrics["d_iv_1m"] <= -0.30


def test_iv_filter_late_stage_exhaustion_rejection():
    engine = IVGatedBreakoutEngine()
    _, df_iv = generate_mock_session_spot_and_iv()

    # Set overextended Z-score (2.5) with flat/declining IV
    idx_1400 = df_iv[df_iv["time_str"] == "14:00"].index[0]
    df_iv.loc[idx_1400, "iv"] = df_iv.loc[idx_1400 - 1, "iv"] - 0.05

    passed, reason, metrics = engine.evaluate_iv_filter("14:00", 2.6, df_iv)

    assert not passed
    assert reason == "LATE_STAGE_EXHAUSTION"


def test_iv_filter_expansion_approval():
    engine = IVGatedBreakoutEngine()
    _, df_iv = generate_mock_session_spot_and_iv()

    # Expanding IV at 12:45
    passed, reason, metrics = engine.evaluate_iv_filter("12:45", 1.1, df_iv)

    assert passed
    assert reason == "IV_EXPANSION_CONFIRMED"
    assert metrics["d_iv_3m"] >= 0.0


def test_end_to_end_session():
    engine = IVGatedBreakoutEngine(enable_iv_filter=True)
    df_s, df_iv = generate_mock_session_spot_and_iv()

    trades, rejections = engine.run_session(df_s, "2026-11-05", df_iv)

    assert isinstance(trades, list)
    assert isinstance(rejections, list)
    if trades:
        t = trades[0]
        assert t.direction == "BULLISH"
        assert t.entry_price > 0
        initial_sl = t.entry_price - t.risk_pts
        assert initial_sl < t.entry_price
        assert t.exit_price is not None
        assert t.pnl_points != 0.0
