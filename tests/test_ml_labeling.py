import pytest
import pandas as pd
from ml_signal.labeling import classify_ares_outcome, label_from_ares_outcome

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
            "entry_timestamp": "2026-07-30T10:20:00Z",
            "entry_price": 24040.0,
            "setup_type": "CONTINUATION",
            "direction": "LONG",
            "result_state": "OPEN",  # Should be ignored
            "market_context": {},
            "oi_data": {}
        }
    ]

    # Testing with t1_is_win=False (so T1_HIT counts as loss/0)
    df_loss = label_from_ares_outcome(records, t1_is_win=False)
    
    assert len(df_loss) == 3
    
    # T2_HIT -> win
    assert df_loss.iloc[0]["label"] == 1
    assert df_loss.iloc[0]["outcome"] == "T2_HIT"
    
    # SL_HIT -> loss
    assert df_loss.iloc[1]["label"] == 0
    assert df_loss.iloc[1]["outcome"] == "SL_HIT"
    
    # T1_HIT -> loss
    assert df_loss.iloc[2]["label"] == 0
    assert df_loss.iloc[2]["outcome"] == "T1_HIT"

    # Testing with t1_is_win=True (now the DEFAULT)
    df_t1_win = label_from_ares_outcome(records)
    assert df_t1_win.iloc[2]["label"] == 1  # T1_HIT is win

def test_build_real_outcome_frame_regression():
    from ml_signal.dataset import build_real_outcome_frame
    
    # Realistic ml_collection rows
    rows = [
        {
            "timestamp": "2026-07-30T10:00:00Z",
            "trade_outcome": "T2_HIT",
            "raw_candle": {"close": 100},
            "candle_features": {"f1": 1.0}
        },
        {
            "timestamp": "2026-07-30T10:01:00Z",
            "trade_outcome": "OPEN", # Should be ignored
            "raw_candle": {"close": 100},
            "candle_features": {"f1": 1.0}
        },
        {
            "timestamp": "2026-07-30T10:02:00Z",
            "trade_outcome": None, # Should be ignored
            "raw_candle": {"close": 100},
            "candle_features": {"f1": 1.0}
        },
        {
            "timestamp": "2026-07-30T10:03:00Z",
            "trade_outcome": "SL_HIT",
            "raw_candle": {"close": 100},
            "candle_features": {"f1": 1.0}
        }
    ]
    
    df = build_real_outcome_frame(rows, t1_is_win=True)
    assert len(df) == 2
    assert "label" in df.columns
    assert df.iloc[0]["label"] == 1
    assert df.iloc[1]["label"] == 0
    assert "candle_features__f1" in df.columns


def test_stopped_out_at_be_keeps_the_existing_t1_win_label():
    assert classify_ares_outcome("STOPPED_OUT_AT_BE") == 1
