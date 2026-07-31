"""
TASK-183 — offline XGBoost training entrypoint.

Reads `ml_collection` (read-only), self-labels it via ml_signal.dataset, trains
an XGBoost classifier on a chronological (small-data-safe) split, evaluates on
the held-out tail, and writes a model artifact + a metrics/feature-importance
report. This is what actually *enables* XGBoost on the data ARES collects.

Run from the repo root:  python -m ml_signal.train_offline

OFFLINE / ADDITIVE: no Supabase writes, no engine changes. See ADR-183.
"""
import os
import sys
import json
from typing import List, Tuple, Dict, Optional

import numpy as np
import pandas as pd
import joblib
import xgboost as xgb
from sklearn.metrics import (
    roc_auc_score, precision_score, recall_score, f1_score, brier_score_loss,
)

from .config import MLConfig, DEFAULT_CONFIG
from .dataset import build_labeled_frame, feature_columns


# NOTE: ml_signal/trainer.py already has train_xgboost/evaluate_model, but it
# imports `optuna` at module top (a heavy tuning dep the offline path doesn't
# need). To keep trainer.py untouched AND avoid that dependency, the equivalent
# minimal helpers are inlined here — same XGBoost params (from MLConfig), same
# metrics. Only xgboost + scikit-learn are required.

def _train_xgb(X_tr, y_tr, X_val, y_val, config: MLConfig):
    scale_pos_weight = (y_tr == 0).sum() / max((y_tr == 1).sum(), 1)
    model = xgb.XGBClassifier(
        n_estimators=config.n_estimators,
        learning_rate=config.learning_rate,
        max_depth=config.max_depth,
        subsample=config.subsample,
        colsample_bytree=config.colsample_bytree,
        gamma=config.gamma,
        reg_lambda=config.reg_lambda,
        scale_pos_weight=scale_pos_weight,
        eval_metric="logloss",
        early_stopping_rounds=config.early_stopping_rounds,
        tree_method="hist",
        random_state=42,
        n_jobs=-1,
    )
    model.fit(X_tr, y_tr, eval_set=[(X_val, y_val)], verbose=False)
    return model


def _precision_at_top_k(y_true, y_proba, top_k_frac: float) -> float:
    n_top = max(1, int(len(y_true) * top_k_frac))
    top = np.argsort(y_proba)[-n_top:]
    return float(np.asarray(y_true)[top].mean())


def _evaluate(model, X_test, y_test) -> Dict[str, float]:
    y_pred = model.predict(X_test)
    y_proba = model.predict_proba(X_test)[:, 1]
    return {
        "auc_roc": float(roc_auc_score(y_test, y_proba)),
        "precision": float(precision_score(y_test, y_pred, zero_division=0)),
        "recall": float(recall_score(y_test, y_pred, zero_division=0)),
        "f1": float(f1_score(y_test, y_pred, zero_division=0)),
        "brier_score": float(brier_score_loss(y_test, y_proba)),
        "accuracy": float((y_pred == y_test).mean()),
        "precision_top20": _precision_at_top_k(y_test, y_proba, 0.2),
    }


def _importance(model, feature_names: List[str]) -> pd.DataFrame:
    return pd.DataFrame(
        {"feature": feature_names, "gain": model.feature_importances_}
    ).sort_values("gain", ascending=False)


def chronological_split(
    df: pd.DataFrame,
    train_frac: float = 0.8,
    date_col: str = "timestamp",
) -> Tuple[pd.DataFrame, pd.DataFrame]:
    """
    Time-ordered split: earliest `train_frac` rows train, remainder test. No
    shuffling — prevents look-ahead leakage. Guarantees a non-empty test set
    when there are >= 2 rows.
    """
    df = df.sort_values(date_col).reset_index(drop=True)
    n = len(df)
    k = max(1, int(n * train_frac))
    if n >= 2 and k >= n:
        k = n - 1
    return df.iloc[:k].reset_index(drop=True), df.iloc[k:].reset_index(drop=True)


def _safe_metrics(model, X_test: pd.DataFrame, y_test: pd.Series) -> Dict[str, float]:
    """_evaluate, but tolerate a single-class test slice (roc_auc undefined)."""
    try:
        return _evaluate(model, X_test, y_test)
    except ValueError:
        y_pred = model.predict(X_test)
        return {
            "auc_roc": float("nan"),  # single-class test window
            "accuracy": float((y_pred == y_test).mean()),
            "precision": float("nan"),
            "recall": float("nan"),
            "f1": float("nan"),
            "brier_score": float("nan"),
            "precision_top20": float("nan"),
        }


def run_training(
    df: pd.DataFrame,
    feature_cols: List[str],
    label_col: str = "label",
    config: MLConfig = DEFAULT_CONFIG,
    min_samples: int = 200,
    train_frac: float = 0.8,
    save_path: Optional[str] = None,
    report_path: Optional[str] = None,
) -> Tuple[object, Dict[str, float]]:
    """
    Train + evaluate on a chronological split. Degrades gracefully on tiny data
    (warns, marks metrics provisional, still trains). Optionally persists the
    model and a JSON report. Returns (model, metrics).
    """
    n = len(df)
    provisional = n < min_samples
    if provisional:
        print(f"[!] PROVISIONAL: {n} labeled samples (< {min_samples}). "
              f"Metrics are directional, not production-grade.")

    train, test = chronological_split(df, train_frac=train_frac)

    X_train, y_train = train[feature_cols], train[label_col]
    # Internal chronological val split for early stopping.
    vi = max(1, int(len(X_train) * 0.8))
    X_tr, y_tr = X_train.iloc[:vi], y_train.iloc[:vi]
    X_val, y_val = X_train.iloc[vi:], y_train.iloc[vi:]
    if len(X_val) == 0:
        X_val, y_val = X_tr, y_tr

    model = _train_xgb(X_tr, y_tr, X_val, y_val, config)

    X_test, y_test = test[feature_cols], test[label_col]
    metrics = _safe_metrics(model, X_test, y_test)
    metrics.update({
        "n_samples": int(n),
        "n_train": int(len(train)),
        "n_test": int(len(test)),
        "pos_rate": float(df[label_col].mean()) if n else float("nan"),
        "provisional": bool(provisional),
    })

    importance = _importance(model, feature_cols)
    metrics["top_features"] = importance.head(15).to_dict(orient="records")

    if save_path:
        os.makedirs(os.path.dirname(save_path) or ".", exist_ok=True)
        joblib.dump(model, save_path)
        print(f"[+] Model saved -> {save_path}")

    if report_path:
        os.makedirs(os.path.dirname(report_path) or ".", exist_ok=True)
        with open(report_path, "w") as f:
            json.dump(metrics, f, indent=2, default=str)
        print(f"[+] Report saved -> {report_path}")

    return model, metrics


# ─────────────────────────── live-data driver ───────────────────────────────

def _fetch_ml_collection(supabase, page: int = 1000) -> List[dict]:
    """Read-only, paginated pull of ml_collection ordered by timestamp asc."""
    cols = "timestamp,raw_candle,trade_outcome," + ",".join([
        "candle_features", "volume_features", "iv_features", "oi_features",
        "greek_features", "structure_features", "meta_features",
    ])
    rows: List[dict] = []
    start = 0
    while True:
        batch = (
            supabase.table("ml_collection")
            .select(cols)
            .order("timestamp")
            .range(start, start + page - 1)
            .execute()
            .data or []
        )
        rows.extend(batch)
        if len(batch) < page:
            break
        start += page
    return rows


def main() -> None:
    # repo root on path so `config` (the app settings, with .env creds) imports.
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if repo not in sys.path:
        sys.path.insert(0, repo)
    try:
        from dotenv import load_dotenv
        load_dotenv(os.path.join(repo, ".env"))
    except Exception:
        pass

    try:
        from config import settings
        url, key = settings.supabase_url, settings.supabase_key
    except Exception:
        url = os.environ.get("SUPABASE_URL", "")
        key = os.environ.get("SUPABASE_KEY", "")
    if not url or not key:
        print("[-] SUPABASE_URL / SUPABASE_KEY not available. Aborting.")
        return

    from supabase import create_client
    supabase = create_client(url, key)

    config = DEFAULT_CONFIG
    print(f"[*] Reading ml_collection (read-only)...")
    rows = _fetch_ml_collection(supabase)
    
    from ml_signal.dataset import build_real_outcome_frame
    print(f"[*] {len(rows)} rows fetched. Filtering for real trade outcomes...")

    df = build_real_outcome_frame(
        rows,
        t1_is_win=True, # Predict probability of hitting T1 (Win=1)
    )
    if df.empty:
        print("[-] No valid real trade outcomes found. "
              "Collect more live trades, then rerun.")
        return

    fcols = feature_columns(df)
    print(f"[*] {len(df)} labeled samples, {len(fcols)} features, "
          f"positive-rate={df['label'].mean():.3f}")

    report_path = os.path.join(repo, "reports", "ml", "task183_offline_metrics.json")
    model, metrics = run_training(
        df, fcols,
        config=config,
        save_path=os.path.join(repo, config.model_path),
        report_path=report_path,
    )

    print("\n=== OFFLINE TRAINING RESULT ===")
    print(f"  samples={metrics['n_samples']} (train={metrics['n_train']}, test={metrics['n_test']})  "
          f"pos_rate={metrics['pos_rate']:.3f}  provisional={metrics['provisional']}")
    auc = metrics["auc_roc"]
    print(f"  AUC-ROC={auc:.3f}" if auc == auc else "  AUC-ROC=n/a (single-class test window)")
    print(f"  precision={metrics['precision']}  recall={metrics['recall']}  "
          f"precision@20%={metrics['precision_top20']}")
    print("  top features:")
    for feat in metrics["top_features"][:8]:
        print(f"    {feat['feature']:32s} gain={feat['gain']:.4f}")
    print("\n  Interpretation: AUC > 0.55 => the collected features carry real")
    print("  short-horizon predictive signal. ~0.5 => not yet (more/other data).")


if __name__ == "__main__":
    main()
