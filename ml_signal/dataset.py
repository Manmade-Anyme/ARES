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
from typing import List, Dict, Any, Sequence
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
_META_COLS = {"timestamp", "date", "close", "label", "pnl_points"}


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

        groups: Dict[str, Dict[str, float]] = {}
        for g in FEATURE_GROUPS:
            nd = _numeric_only(_load(r.get(g)))
            groups[g] = nd
            keys_by_group[g].update(nd.keys())
        parsed.append((ts, close, groups))

    feature_cols = sorted(
        f"{g}__{k}" for g in FEATURE_GROUPS for k in keys_by_group[g]
    )

    records = []
    for ts, close, groups in parsed:
        row: Dict[str, Any] = {
            "timestamp": ts,
            "date": ts.date() if ts is not None and not pd.isna(ts) else None,
            "close": close,
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

    ordered = ["timestamp", "date", "close"] + feature_cols
    df = pd.DataFrame.from_records(records)
    if df.empty:
        return pd.DataFrame(columns=ordered)
    return df[ordered]


def label_forward_points(
    closes: Sequence[float],
    lookforward: int,
    tp_points: float,
    sl_points: float,
) -> np.ndarray:
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

    for i in range(n):
        entry = closes[i]
        end = min(i + lookforward, n - 1)
        future = closes[i + 1 : end + 1]
        if not future:
            labels[i] = -1
            continue

        bull_res = None  # "win" | "loss"
        bear_res = None
        result = -1

        for c in future:
            delta = c - entry
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
                break

        if result != 1:
            result = 0 if (bull_res == "loss" or bear_res == "loss") else -1
        labels[i] = result

    return labels


def build_labeled_frame(
    rows: Sequence[Dict[str, Any]],
    lookforward: int,
    tp_points: float,
    sl_points: float,
) -> pd.DataFrame:
    """
    Flatten + label, grouped by trading date so a forward window never spans the
    overnight gap. Inconclusive rows (label == -1) and unlabeled tails are
    dropped. Returns feature columns + `label`.
    """
    df = flatten_features(rows)
    if df.empty:
        return df.assign(label=pd.Series(dtype=int))

    df = df.sort_values("timestamp").reset_index(drop=True)
    labeled_parts = []
    for _, day in df.groupby("date", sort=True):
        day = day.sort_values("timestamp").reset_index(drop=True)
        day = day.copy()
        day["label"] = label_forward_points(
            day["close"].tolist(), lookforward, tp_points, sl_points
        )
        labeled_parts.append(day)

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
    
    for r in rows:
        outcome = r.get("trade_outcome")
        if not outcome:
            continue
            
        label = classify_ares_outcome(outcome, t1_is_win=t1_is_win)
        if label is not None:
            valid_rows.append(r)
            labels.append(label)
            pnl_points.append(r.get("trade_pnl"))

    if not valid_rows:
        df = flatten_features([])
        return df.assign(label=pd.Series(dtype=int))

    df = flatten_features(valid_rows)
    df["label"] = labels
    # Performance reporting consumes realized spot P&L separately from model
    # inputs. `feature_columns` explicitly excludes this column, preventing
    # outcome leakage into the reliability model.
    df["pnl_points"] = pd.to_numeric(pnl_points, errors="coerce")
    
    return df
