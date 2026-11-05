import pytest
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
import pytz

from poc.trend_expansion_poc import TrendExpansionEngine, TradeRecord

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

