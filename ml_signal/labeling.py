from typing import Optional, List

import pandas as pd
import numpy as np


def classify_ares_outcome(result_state: str, t1_is_win: bool = True) -> Optional[int]:
    """Pure function to map an ARES result state to a binary ML label."""
    if result_state == "T2_HIT":
        return 1
    if result_state in ("T1_HIT", "STOPPED_OUT_AT_BE"):
        return 1 if t1_is_win else 0
    if result_state in ("SL_HIT", "STOPPED_OUT", "TIME_STOP"):
        # Note: TIME_STOP is retained strictly as a legacy loss (0) for historical
        # ml_collection records during offline retraining. No new TIME_STOP
        # outcomes are emitted by live runtime trades.
        return 0
    return None


def label_from_ares_outcome(
    trade_analytics_records: List[dict],
    t1_is_win: bool = True,
) -> pd.DataFrame:
    rows = []
    for rec in trade_analytics_records:
        result = rec.get("result_state", "")
        label = classify_ares_outcome(result, t1_is_win=t1_is_win)
        
        if label is None:
            continue

        rows.append({
            "timestamp": rec["entry_timestamp"],
            "spot": rec.get("entry_price", 0),
            "setup_type": rec.get("setup_type", ""),
            "direction": rec.get("direction", ""),
            "label": label,
            "outcome": result,
            "features": rec.get("market_context", {}),
            "oi_data": rec.get("oi_data", {}),
        })

    return pd.DataFrame(rows)


def label_candle_forward(
    df_candles: pd.DataFrame,
    lookforward: int = 5,
    tp_pct: float = 0.15,
    sl_pct: float = 0.10,
    exclude_inconclusive: bool = True,
) -> pd.DataFrame:
    closes = df_candles["close"].values
    labels = np.full(len(closes), -1, dtype=int)

    for i in range(len(closes) - lookforward):
        entry = closes[i]
        tp = entry * (1 + tp_pct / 100)
        sl = entry * (1 - sl_pct / 100)
        future = closes[i + 1 : i + lookforward + 1]

        tp_hit = np.where(future >= tp)[0]
        sl_hit = np.where(future <= sl)[0]

        if len(tp_hit) > 0 and (len(sl_hit) == 0 or tp_hit[0] < sl_hit[0]):
            labels[i] = 1
        elif len(sl_hit) > 0 and (len(tp_hit) == 0 or sl_hit[0] < tp_hit[0]):
            labels[i] = 0
        else:
            labels[i] = -1 if exclude_inconclusive else 0

    df = df_candles.copy()
    df["label"] = labels
    df["tp_level"] = df["close"] * (1 + tp_pct / 100)
    df["sl_level"] = df["close"] * (1 - sl_pct / 100)

    if exclude_inconclusive:
        df = df[df["label"] != -1].copy()

    return df


def label_candle_forward_bidirectional(
    df_candles: pd.DataFrame,
    lookforward: int = 5,
    tp_bull_pct: float = 0.15,
    sl_bull_pct: float = 0.10,
    tp_bear_pct: float = 0.15,
    sl_bear_pct: float = 0.10,
    exclude_inconclusive: bool = True,
) -> pd.DataFrame:
    closes = df_candles["close"].values
    labels = np.full(len(closes), -1, dtype=int)

    for i in range(len(closes) - lookforward):
        entry = closes[i]
        future = closes[i + 1 : i + lookforward + 1]

        bull_tp = entry * (1 + tp_bull_pct / 100)
        bull_sl = entry * (1 - sl_bull_pct / 100)
        bear_tp = entry * (1 - tp_bear_pct / 100)
        bear_sl = entry * (1 + sl_bear_pct / 100)
        high = np.max(future)
        low = np.min(future)

        bull_won = high >= bull_tp
        bull_lost = low <= bull_sl
        bear_won = low <= bear_tp
        bear_lost = high >= bear_sl

        if bull_won and not bull_lost:
            labels[i] = 1
        elif bear_won and not bear_lost:
            labels[i] = 1
        elif bull_lost and not bull_won:
            labels[i] = 0
        elif bear_lost and not bear_won:
            labels[i] = 0
        else:
            labels[i] = -1 if exclude_inconclusive else 0

    df = df_candles.copy()
    df["label"] = labels
    return df[df["label"] != -1].copy() if exclude_inconclusive else df
