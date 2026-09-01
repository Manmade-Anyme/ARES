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


def _shap_metrics() -> Dict[str, object]:
    """Return the stable, explicit schema for optional offline SHAP output."""
    return {
        "shap_computed": False,
        "shap_top_features": [],
        "shap_output_unit": "raw_margin_log_odds",
        "shap_explained_rows": 0,
        "shap_backend": "none",
        "shap_status": "not_attempted",
        "shap_plot_saved": False,
        "shap_plot_status": "not_requested",
        "shap_plot_error_type": None,
        "shap_fallback_reason": None,
        "shap_error_type": None,
        "shap_iteration_range": None,
        "shap_raw_margin_additivity": False,
    }


def _native_shap_values(model, X_test: pd.DataFrame, best_iteration: int):
    """Compute exact raw-margin TreeSHAP values through XGBoost itself."""
    iteration_range = (0, best_iteration + 1)
    dtest = xgb.DMatrix(X_test)
    booster = model.get_booster()
    contributions = np.asarray(booster.predict(
        dtest, pred_contribs=True, iteration_range=iteration_range,
    ))
    if contributions.ndim != 2 or contributions.shape[1] != X_test.shape[1] + 1:
        raise ValueError("native SHAP contribution shape mismatch")
    if not np.isfinite(contributions).all():
        raise ValueError("native SHAP contributions contain non-finite values")
    raw_margin = np.asarray(booster.predict(
        dtest, output_margin=True, iteration_range=iteration_range,
    ))
    if raw_margin.ndim != 1 or not np.isfinite(raw_margin).all():
        raise ValueError("native SHAP raw margins contain non-finite values")
    if not np.allclose(contributions.sum(axis=1), raw_margin, rtol=1e-5, atol=1e-6):
        raise ValueError("native SHAP contributions are not additive to raw margin")
    return contributions[:, :-1], iteration_range


def _compute_shap(
    model,
    X_test: pd.DataFrame,
    feature_cols: List[str],
    metrics: Dict[str, object],
):
    """Populate metrics with optional held-out-set SHAP analysis."""
    best_iteration = int(getattr(model, "best_iteration", model.n_estimators - 1))
    metrics["shap_iteration_range"] = [0, best_iteration + 1]
    import_failure = None
    try:
        import shap
    except ModuleNotFoundError as exc:
        if exc.name == "shap":
            print("[!] SHAP unavailable; skipping offline explanation and continuing.")
            metrics["shap_status"] = "shap_unavailable"
            metrics["shap_error_type"] = type(exc).__name__
            return
        import_failure = exc
    except Exception as exc:
        import_failure = exc

    values = None
    tree_exc = import_failure
    if tree_exc is None:
        try:
            explainer = shap.TreeExplainer(model)
            # `tree_limit` is the SHAP API's equivalent of XGBoost's iteration range.
            if hasattr(explainer, "shap_values"):
                values = explainer.shap_values(
                    X_test, tree_limit=best_iteration + 1, check_additivity=True,
                )
            else:
                values = explainer(
                    X_test, tree_limit=best_iteration + 1, check_additivity=True,
                ).values
            if isinstance(values, list):
                values = values[-1]
            values = np.asarray(values, dtype=float)
            expected_shape = (len(X_test), len(feature_cols))
            if values.shape != expected_shape:
                raise ValueError("SHAP contribution shape mismatch")
            if not np.isfinite(values).all():
                raise ValueError("SHAP contributions contain non-finite values")
            backend = "shap_tree_explainer"
            iteration_range = [0, best_iteration + 1]
            metrics["shap_raw_margin_additivity"] = True
        except Exception as exc:
            tree_exc = exc

    if tree_exc is not None:
        reason = str(tree_exc).strip().replace("\n", " ")[:200]
        metrics["shap_fallback_reason"] = type(tree_exc).__name__ + (
            f": {reason}" if reason else ""
        )
        try:
            values, native_range = _native_shap_values(model, X_test, best_iteration)
            expected_shape = (len(X_test), len(feature_cols))
            if values.shape != expected_shape or not np.isfinite(values).all():
                raise ValueError("native SHAP contribution shape mismatch")
            backend = "xgboost_pred_contribs"
            iteration_range = list(native_range)
            metrics["shap_raw_margin_additivity"] = True
        except Exception as native_exc:
            metrics["shap_status"] = "failed"
            metrics["shap_error_type"] = type(native_exc).__name__
            return

    means = np.mean(np.abs(values), axis=0)
    ranked = sorted(zip(feature_cols, means), key=lambda item: item[1], reverse=True)
    metrics["shap_top_features"] = [
        {"feature": feature, "mean_abs_shap": round(float(value), 6)}
        for feature, value in ranked[:15]
    ]
    metrics["shap_computed"] = True
    metrics["shap_explained_rows"] = int(len(X_test))
    metrics["shap_backend"] = backend
    metrics["shap_status"] = "computed"
    metrics["shap_iteration_range"] = iteration_range


def _save_shap_plot(metrics: Dict[str, object], shap_plot_path: Optional[str]) -> None:
    """Save the optional headless top-feature SHAP plot without aborting training."""
    if not shap_plot_path:
        return
    if not metrics["shap_computed"]:
        if os.path.isfile(shap_plot_path) and not os.path.islink(shap_plot_path):
            try:
                os.remove(shap_plot_path)
            except OSError as exc:
                metrics["shap_plot_status"] = "stale_cleanup_failed"
                metrics["shap_plot_error_type"] = type(exc).__name__
                return
        metrics["shap_plot_status"] = "not_saved_no_shap"
        return
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        top = metrics["shap_top_features"]
        labels = [row["feature"] for row in reversed(top)]
        values = [row["mean_abs_shap"] for row in reversed(top)]
        fig, ax = plt.subplots(figsize=(10, 6))
        try:
            ax.barh(labels, values)
            ax.set_xlabel("Mean absolute SHAP value (raw margin / log-odds)")
            ax.set_title("Offline SHAP feature importance")
            fig.tight_layout()
            os.makedirs(os.path.dirname(shap_plot_path) or ".", exist_ok=True)
            fig.savefig(shap_plot_path, dpi=150, bbox_inches="tight")
        finally:
            plt.close(fig)
        metrics["shap_plot_saved"] = True
        metrics["shap_plot_status"] = "saved"
        print(f"[+] SHAP plot saved -> {shap_plot_path}")
    except Exception as exc:
        metrics["shap_plot_status"] = "failed"
        metrics["shap_plot_error_type"] = type(exc).__name__
        print(f"[!] SHAP plot could not be saved; continuing ({type(exc).__name__}).")


def _offline_report_paths(repo: str, version: str) -> Tuple[str, str, str]:
    """Return canonical, versioned, and SHAP plot report paths."""
    report_dir = os.path.join(repo, "reports", "ml")
    return (
        os.path.join(report_dir, "task183_offline_metrics.json"),
        os.path.join(report_dir, f"{version}_offline_metrics.json"),
        os.path.join(report_dir, f"{version}_shap_summary.png"),
    )


def _print_shap_summary(metrics: Dict[str, object]) -> None:
    """Print the labeled raw-margin/log-odds SHAP summary."""
    print("=== SHAP Feature Importance (mean |SHAP| on test set; raw margin/log-odds) ===")
    if metrics["shap_computed"]:
        for feat in metrics["shap_top_features"][:8]:
            print(f"    {feat['feature']:32s} mean_abs_shap={feat['mean_abs_shap']:.6f}")
    else:
        print(f"    unavailable ({metrics['shap_status']})")


def run_training(
    df: pd.DataFrame,
    feature_cols: List[str],
    label_col: str = "label",
    config: MLConfig = DEFAULT_CONFIG,
    min_samples: int = 200,
    train_frac: float = 0.8,
    save_path: Optional[str] = None,
    report_path: Optional[str] = None,
    shap_plot_path: Optional[str] = None,
    model_version: Optional[str] = None,
) -> Tuple[object, Dict[str, object]]:
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
    metrics.update(_shap_metrics())
    if model_version is not None:
        metrics["model_version"] = model_version

    importance = _importance(model, feature_cols)
    metrics["top_features"] = importance.head(15).to_dict(orient="records")
    _compute_shap(model, X_test, feature_cols, metrics)
    _save_shap_plot(metrics, shap_plot_path)

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
        "detector_scores",   # TASK-4e: one-hot setup-detector dict
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

    url = (os.environ.get("SUPABASE_URL") or "").strip().strip('"\'')
    key = (os.environ.get("SUPABASE_KEY") or "").strip().strip('"\'')
    if not url or not key:
        try:
            from config import settings
            url = url or (getattr(settings, "supabase_url", "") or "").strip().strip('"\'')
            key = key or (getattr(settings, "supabase_key", "") or "").strip().strip('"\'')
        except Exception:
            pass

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

    from ml_signal.predictor import get_next_model_version_and_path
    models_dir = os.path.join(repo, "ml_signal", "models")
    save_path, next_version = get_next_model_version_and_path(models_dir)
    print(f"[*] Incrementing model version -> {next_version} ({save_path})")

    report_path, versioned_report_path, shap_plot_path = _offline_report_paths(
        repo, next_version,
    )
    model, metrics = run_training(
        df, fcols,
        config=config,
        save_path=save_path,
        report_path=report_path,
        shap_plot_path=shap_plot_path,
        model_version=next_version,
    )
    with open(versioned_report_path, "w") as f:
        json.dump(metrics, f, indent=2, default=str)

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
    _print_shap_summary(metrics)
    print("\n  Interpretation: AUC > 0.55 => the collected features carry real")
    print("  short-horizon predictive signal. ~0.5 => not yet (more/other data).")


if __name__ == "__main__":
    main()
