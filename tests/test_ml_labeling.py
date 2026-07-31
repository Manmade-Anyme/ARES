import pytest
import pandas as pd
from ml_signal.labeling import label_from_ares_outcome

def test_label_from_ares_outcome():
    records = [
        {
            "entry_timestamp": "2026-07-30T10:00:00Z",
            "entry_price": 24000.0,
            "setup_type": "EXHAUSTION",
            "direction": "LONG",
            "result_state": "T2_HIT",
            "market_context": {"candle_features__body_pct": 5.0},
            "oi_data": {"ce": 100}
        },
        {
            "entry_timestamp": "2026-07-30T10:05:00Z",
            "entry_price": 24010.0,
            "setup_type": "OI_WALL_REJECTION",
            "direction": "SHORT",
            "result_state": "SL_HIT",
            "market_context": {"candle_features__body_pct": 2.0},
            "oi_data": {"pe": 200}
        },
        {
            "entry_timestamp": "2026-07-30T10:10:00Z",
            "entry_price": 24020.0,
            "setup_type": "EXHAUSTION",
            "direction": "LONG",
            "result_state": "T1_HIT",
            "market_context": {"candle_features__body_pct": 1.0},
            "oi_data": {}
        },
        {
            "entry_timestamp": "2026-07-30T10:15:00Z",
            "entry_price": 24030.0,
            "setup_type": "CONTINUATION",
            "direction": "LONG",
            "result_state": "TIME_STOP",
            "market_context": {},
            "oi_data": {}
        },
        {
            "entry_timestamp": "2026-07-30T10:20:00Z",
            "entry_price": 24040.0,
            "setup_type": "CONTINUATION",
            "direction": "LONG",
            "result_state": "OPEN",  # Should be ignored
            "market_context": {},
            "oi_data": {}
        }
    ]

    # Testing with t1_is_win=False (so T1_HIT and TIME_STOP count as losses/0)
    df = label_from_ares_outcome(records, t1_is_win=False)
    
    assert len(df) == 4
    
    # T2_HIT -> win
    assert df.iloc[0]["label"] == 1
    assert df.iloc[0]["outcome"] == "T2_HIT"
    
    # SL_HIT -> loss
    assert df.iloc[1]["label"] == 0
    assert df.iloc[1]["outcome"] == "SL_HIT"
    
    # T1_HIT -> loss
    assert df.iloc[2]["label"] == 0
    assert df.iloc[2]["outcome"] == "T1_HIT"
    
    # TIME_STOP -> loss
    assert df.iloc[3]["label"] == 0
    assert df.iloc[3]["outcome"] == "TIME_STOP"

    # Testing with t1_is_win=True
    df_t1_win = label_from_ares_outcome(records, t1_is_win=True)
    assert df_t1_win.iloc[2]["label"] == 1  # T1_HIT is win
    assert df_t1_win.iloc[3]["label"] == 0  # TIME_STOP is still loss
