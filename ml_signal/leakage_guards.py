import pandas as pd
from typing import List

FORBIDDEN_OUTCOME_FIELDS = {
    "label", "target", "trade_id", "trade_outcome", "trade_pnl",
    "pnl_points", "exit_timestamp", "exit_price", "exit_type",
    "result_state", "realized_pnl", "duration_seconds", "close",
    "raw_candle", "pnl", "pnl_amount", "stop_loss_hit", "target_hit",
    "resolution_timestamp", "signal_id", "signal_setup_type"
}

class DataLeakageError(Exception):
    pass

def assert_no_outcome_leakage(feature_names: List[str]) -> None:
    intersection = set(feature_names).intersection(FORBIDDEN_OUTCOME_FIELDS)
    if intersection:
        raise DataLeakageError(f"Outcome field leakage detected: {intersection}")

def assert_chronological_integrity(df: pd.DataFrame, date_col: str = "timestamp") -> None:
    if not df[date_col].is_monotonic_increasing:
        raise DataLeakageError(f"Dataframe {date_col} is not monotonically increasing.")

def assert_train_test_purged(train_df: pd.DataFrame, test_df: pd.DataFrame, resolution_col: str = "resolution_timestamp") -> None:
    train_max_res = pd.to_datetime(train_df[resolution_col]).max()
    test_min_ts = pd.to_datetime(test_df["timestamp"]).min()
    if train_max_res >= test_min_ts:
        raise DataLeakageError(f"Train resolution {train_max_res} overlaps with test start {test_min_ts}")

def deduplicate_snapshots(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
        
    df_out = df.copy()
    
    # 1. source_candle_ts for background snapshots
    if "timestamp" in df_out.columns:
        source_candle_ts = pd.to_datetime(df_out["timestamp"]).dt.floor("1min")
        df_out["_source_candle_ts"] = source_candle_ts
    
    # feature hash using only non-meta columns
    # We should exclude _META_COLS, but for simplicity here we just use all numeric/object columns that aren't meta
    from ml_signal.dataset import _META_COLS, feature_columns
    
    feat_cols = feature_columns(df_out)
    if not feat_cols:
        # fallback
        feat_cols = [c for c in df_out.columns if c not in _META_COLS and not c.startswith("_")]
    
    # Compute feature hash
    df_out["_feature_hash"] = df_out[feat_cols].apply(lambda x: hash(tuple(x)), axis=1)

    if "trade_id" in df_out.columns and df_out["trade_id"].notna().any():
        # TradeOutcomePipeline deduplication
        # Use trade_id and feature hash
        # If trade_id is missing, fallback to snapshot_uuid if available
        subset = ["_feature_hash"]
        if "trade_id" in df_out.columns:
            subset = ["trade_id"] + subset
        
        df_out = df_out.drop_duplicates(subset=subset, keep="last")
    else:
        # MarketMovementPipeline deduplication
        df_out = df_out.drop_duplicates(subset=["_source_candle_ts", "_feature_hash"], keep="last")
        # Also drop intra-candle updates keeping terminal
        df_out = df_out.sort_values("timestamp")
        df_out = df_out.drop_duplicates(subset=["_source_candle_ts"], keep="last")

    # cleanup temp cols
    if "_source_candle_ts" in df_out.columns:
        df_out = df_out.drop(columns=["_source_candle_ts"])
    if "_feature_hash" in df_out.columns:
        df_out = df_out.drop(columns=["_feature_hash"])
        
    return df_out
