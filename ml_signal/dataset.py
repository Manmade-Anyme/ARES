"""
TASK-183 — offline dataset assembly for the ML training pipeline.

Reads the `ml_collection` snapshots (rich per-cycle features that the live
system already logs), flattens the 8 feature-group JSON columns into a numeric
matrix, and self-labels every row by its own forward price path — so the data
ARES already collects becomes trainable *today*, without waiting on the trickle
of realized trades and without touching any live-path code.

READ-ONLY / OFFLINE: nothing here writes to Supabase or mutates engine state.
See ADR-183 for the label definition and rationale.
"""
from typing import List, Dict, Any, Sequence, Tuple
import json

import numpy as np
import pandas as pd


# The 8 feature-group JSON columns written by ml_signal/collector.py.
FEATURE_GROUPS = [
    "candle_features",
    "volume_features",
    "iv_features",
    "oi_features",
    "greek_features",
    "structure_features",
    "meta_features",
    "detector_scores",   # TASK-4e: one-hot dict keyed by SetupType.value.lower()
]

# Non-feature bookkeeping columns produced by flatten_features().
_META_COLS = {
    "timestamp",
    "date",
    "close",
    "label",
    "pnl_points",
    "exit_timestamp",
    "entry_timestamp",
    "time_metrics_excluded",
    "feature_version",
    "trade_id",
    "snapshot_uuid",
    "signal_id",
    "signal_setup_type",
    "resolution_timestamp",
}


def _load(v: Any) -> Dict[str, Any]:
    """Accept a dict (already parsed) or a JSON string; return a dict."""
    if v is None:
        return {}
    if isinstance(v, dict):
        return v
    try:
        parsed = json.loads(v)
        return parsed if isinstance(parsed, dict) else {}
    except (json.JSONDecodeError, TypeError):
        return {}


def infer_feature_version_from_timestamp(ts: Any) -> int:
    """Infer feature version epoch from timestamp if feature_version column is not yet populated."""
    if ts is None:
        return 4
    try:
        ts_dt = pd.to_datetime(ts)
        if ts_dt.tzinfo is None:
            ts_utc = ts_dt.tz_localize("UTC")
        else:
            ts_utc = ts_dt.tz_convert("UTC")

        v1_cutoff = pd.Timestamp("2026-07-28T06:32:37Z")
        v2_cutoff = pd.Timestamp("2026-07-31T13:14:34Z")
        v3_cutoff = pd.Timestamp("2026-08-21T05:46:35Z")

        if ts_utc < v1_cutoff:
            return 1
        elif ts_utc < v2_cutoff:
            return 2
        elif ts_utc < v3_cutoff:
            return 3
        else:
            return 4
    except Exception:
        return 4


def _numeric_only(d: Dict[str, Any]) -> Dict[str, float]:

    """Keep numeric values only (bools excluded — they are not features here)."""
    out = {}
    for k, v in d.items():
        if isinstance(v, bool):
            continue
        if isinstance(v, (int, float)):
            out[k] = float(v)
    return out


def flatten_features(rows: Sequence[Dict[str, Any]]) -> pd.DataFrame:
    """
    Flatten ml_collection rows into a numeric feature matrix.

    Each of the 7 feature-group dicts becomes columns named ``<group>__<key>``.
    Columns are the stable union of keys seen across all rows (sorted); a key
    missing from a given row is filled with NaN, which XGBoost consumes as
    missing. Do not "restore" a 0.0 fill here — 0.0 is a real reading for most
    of these features, and for a structure distance it asserts that spot is
    exactly at support/resistance (TASK-199). A genuine 0.0 is preserved.
    `close` is taken from `raw_candle`; `timestamp`/`date` support day-bounded
    labeling and chronological splitting.

    Returns a DataFrame with columns: timestamp, date, close, and one column
    per flattened feature.
    """
    parsed = []
    keys_by_group: Dict[str, set] = {g: set() for g in FEATURE_GROUPS}

    for r in rows:
        ts = pd.to_datetime(r.get("timestamp"))
        rc = _load(r.get("raw_candle"))
        close = float(rc.get("close", 0) or 0)
        raw_ver = r.get("feature_version")
        if raw_ver is not None:
            try:
                feature_version = int(raw_ver)
            except (ValueError, TypeError):
                feature_version = infer_feature_version_from_timestamp(r.get("timestamp"))
        else:
            feature_version = infer_feature_version_from_timestamp(r.get("timestamp"))

        groups: Dict[str, Dict[str, float]] = {}
        for g in FEATURE_GROUPS:
            nd = _numeric_only(_load(r.get(g)))
            groups[g] = nd
            keys_by_group[g].update(nd.keys())
        parsed.append((ts, close, feature_version, groups))

    feature_cols = sorted(
        f"{g}__{k}" for g in FEATURE_GROUPS for k in keys_by_group[g]
    )

    records = []
    for ts, close, feature_version, groups in parsed:
        row: Dict[str, Any] = {
            "timestamp": ts,
            "date": ts.date() if ts is not None and not pd.isna(ts) else None,
            "close": close,
            "feature_version": feature_version,
        }
        # NaN, not 0.0. _numeric_only drops a None value, so an unknown feature
        # reaches here as an absent key. Filling 0.0 made every unknown
        # structure distance read as "spot is exactly at support/resistance" —
        # the strongest structural state there is, and a worse lie than the
        # 100.0 sentinel TASK-194/195 removed to get here. It affected 28% of
        # the rows train_offline would build. XGBoost treats NaN as missing
        # natively; a genuine 0.0 distance still arrives as 0.0.
        for col in feature_cols:
            row[col] = np.nan
        for g in FEATURE_GROUPS:
            for k, v in groups[g].items():
                row[f"{g}__{k}"] = v
        records.append(row)

    indicator_cols = [
        "structure__has_nearest_support",
        "structure__has_nearest_resistance",
        "greek__has_net_delta",
    ]

    df = pd.DataFrame.from_records(records)
    if not df.empty:
        if "structure_features__dist_to_nearest_support" in df.columns:
            df["structure__has_nearest_support"] = (~df["structure_features__dist_to_nearest_support"].isna()).astype(float)
        else:
            df["structure__has_nearest_support"] = 0.0

        if "structure_features__dist_to_nearest_resistance" in df.columns:
            df["structure__has_nearest_resistance"] = (~df["structure_features__dist_to_nearest_resistance"].isna()).astype(float)
        else:
            df["structure__has_nearest_resistance"] = 0.0

        if "greek_features__net_delta" in df.columns:
            df["greek__has_net_delta"] = (~df["greek_features__net_delta"].isna()).astype(float)
        else:
            df["greek__has_net_delta"] = 0.0

    ordered = ["timestamp", "date", "close", "feature_version"] + feature_cols + indicator_cols
    if df.empty:
        return pd.DataFrame(columns=ordered)
    return df[ordered]



def label_forward_points(
    closes: Sequence[float],
    timestamps: Sequence[Any],
    lookforward: int,
    tp_points: float,
    sl_points: float,
) -> Tuple[np.ndarray, np.ndarray]:
    """
    Order-aware, bidirectional triple-barrier label over a single contiguous
    close series (one trading day).

    From each row i, walk the next `lookforward` closes in order:
      - a clean +tp_points move (before the -sl_points stop) OR a clean
        -tp_points move (before the +sl_points stop) => label 1 ("tradeable
        move imminent", either direction),
      - the opposing stop hit first with no clean target => label 0,
      - neither resolved within the window (chop) => label -1 (inconclusive).

    tp/sl are in index POINTS to mirror the engine's point-based targets.
    """
    closes = [float(c) for c in closes]
    n = len(closes)
    labels = np.full(n, -1, dtype=int)
    resolutions = np.full(n, None, dtype=object)

    for i in range(n):
        entry = closes[i]
        end = min(i + lookforward, n - 1)
        future = closes[i + 1 : end + 1]
        future_ts = timestamps[i + 1 : end + 1]
        if not future:
            labels[i] = -1
            continue

        bull_res = None  # "win" | "loss"
        bear_res = None
        result = -1
        res_ts = None
        
        # We need to track the first target hit or the double stop hit.
        for j, c in enumerate(future):
            delta = c - entry
            ts = future_ts[j]
            
            if bull_res is None:
                if delta >= tp_points:
                    bull_res = "win"
                elif delta <= -sl_points:
                    bull_res = "loss"
            if bear_res is None:
                if delta <= -tp_points:
                    bear_res = "win"
                elif delta >= sl_points:
                    bear_res = "loss"
                    
            if bull_res == "win" or bear_res == "win":
                result = 1
                res_ts = ts
                break
            
            if bull_res == "loss" and bear_res == "loss":
                result = 0
                res_ts = ts
                break

        if result == -1:
            if bull_res == "loss" or bear_res == "loss":
                result = 0
            else:
                result = -1
            res_ts = future_ts[-1] if len(future_ts) > 0 else timestamps[i]
            
        labels[i] = result
        resolutions[i] = res_ts

    return labels, resolutions


def build_labeled_frame(
    rows: Sequence[Dict[str, Any]],
    lookforward: int,
    tp_points: float,
    sl_points: float,
) -> pd.DataFrame:
    """
    Flatten + label, grouped by trading date so a forward window never spans the
    overnight gap. Inconclusive rows (label == -1) and unlabeled tails are
    dropped. Returns feature columns + `label` and `resolution_timestamp`.
    """
    df = flatten_features(rows)
    if df.empty:
        return df.assign(label=pd.Series(dtype=int), resolution_timestamp=pd.Series(dtype=object))

    df = df.sort_values("timestamp").reset_index(drop=True)
    labeled_parts = []
    for _, day in df.groupby("date", sort=True):
        day = day.sort_values("timestamp").reset_index(drop=True)
        day = day.copy()
        
        labels, res_ts = label_forward_points(
            day["close"].tolist(),
            day["timestamp"].tolist(),
            lookforward, tp_points, sl_points
        )
        day["label"] = labels
        day["resolution_timestamp"] = res_ts
        labeled_parts.append(day)

    if not labeled_parts:
        return df.iloc[0:0].assign(label=pd.Series(dtype=int), resolution_timestamp=pd.Series(dtype=object))
    res = pd.concat(labeled_parts, ignore_index=True)
    res = res[res["label"] != -1].reset_index(drop=True)
    return res


def feature_columns(df: pd.DataFrame) -> List[str]:
    """The flattened feature columns (everything except bookkeeping/label)."""
    return [c for c in df.columns if c not in _META_COLS]

def build_real_outcome_frame(
    rows: Sequence[Dict[str, Any]],
    t1_is_win: bool = True,
) -> pd.DataFrame:
    """
    Flatten + label using real historical ARES trade outcomes.
    Filters the dataset to only include rows where `trade_outcome` is a definitive win or loss.
    """
    from ml_signal.labeling import classify_ares_outcome
    
    valid_rows = []
    labels = []
    pnl_points = []
    exit_timestamps = []
    entry_timestamps = []
    time_metrics_excluded = []
    
    for r in rows:
        outcome = r.get("trade_outcome")
        if not outcome:
            continue
            
        label = classify_ares_outcome(outcome, t1_is_win=t1_is_win)
        if label is not None:
            valid_rows.append(r)
            labels.append(label)
            pnl_points.append(r.get("trade_pnl"))
            exit_timestamps.append(r.get("exit_timestamp"))
            entry_timestamps.append(r.get("entry_timestamp"))
            time_metrics_excluded.append(r.get("time_metrics_excluded", False))

    if not valid_rows:
        df = flatten_features([])
        return df.assign(label=pd.Series(dtype=int))

    df = flatten_features(valid_rows)
    df["label"] = labels
    # Performance reporting consumes realized spot P&L separately from model
    # inputs. `feature_columns` explicitly excludes this column, preventing
    # outcome leakage into the reliability model.
    df["pnl_points"] = pd.to_numeric(pnl_points, errors="coerce")
    df["exit_timestamp"] = exit_timestamps
    df["entry_timestamp"] = entry_timestamps
    df["time_metrics_excluded"] = time_metrics_excluded
    
    return df
