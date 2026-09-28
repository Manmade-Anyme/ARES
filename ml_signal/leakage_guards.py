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
    df_out["_feature_hash"] = df_out[feat_cols].apply(lambda x: hash(tuple(x.fillna(-9999))), axis=1)

    has_trade_ids = "trade_id" in df_out and df_out["trade_id"].notna().any()
    has_snapshot_ids = (
        "snapshot_uuid" in df_out and df_out["snapshot_uuid"].notna().any()
    )
    if has_trade_ids or has_snapshot_ids:
        # TradeOutcomePipeline deduplication
        # Choose the canonical identity per row. A frame can legitimately mix
        # linked trades and snapshot-only historical rows, so selecting one
        # identity column for the whole frame would collapse all null values.
        trade_ids = df_out.get("trade_id", pd.Series(index=df_out.index, dtype=object))
        snapshot_ids = df_out.get(
            "snapshot_uuid", pd.Series(index=df_out.index, dtype=object)
        )
        canonical_ids = trade_ids.map(
            lambda value: f"trade:{value}" if pd.notna(value) else None
        )
        snapshot_fallback = snapshot_ids.map(
            lambda value: f"snapshot:{value}" if pd.notna(value) else None
        )
        canonical_ids = canonical_ids.fillna(snapshot_fallback)
        # With no identity, preserving the outcome is safer than silently
        # merging distinct executions merely because their features match.
        missing = canonical_ids.isna()
        canonical_ids.loc[missing] = [
            f"unidentified-row:{idx}" for idx in canonical_ids.index[missing]
        ]
        df_out["_canonical_identity"] = canonical_ids
        df_out = df_out.drop_duplicates(
            subset=["_canonical_identity", "_feature_hash"], keep="last"
        )
    else:
        # MarketMovementPipeline deduplication
        df_out = df_out.sort_values("timestamp")
        # Polling can capture several evolving feature states for the same
        # candle.  A feature hash only removes identical retries; retaining
        # changed states would make those polls consume extra forward-label
        # positions.  Canonicalize each candle to its terminal snapshot.
        df_out = df_out.drop_duplicates(subset=["_source_candle_ts"], keep="last")

    # cleanup temp cols
    if "_source_candle_ts" in df_out.columns:
        df_out = df_out.drop(columns=["_source_candle_ts"])
    if "_feature_hash" in df_out.columns:
        df_out = df_out.drop(columns=["_feature_hash"])
    if "_canonical_identity" in df_out.columns:
        df_out = df_out.drop(columns=["_canonical_identity"])
        
    return df_out
