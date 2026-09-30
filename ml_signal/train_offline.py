"""
TASK-183 — offline XGBoost training entrypoint.

Reads `ml_collection` (read-only), self-labels it via ml_signal.dataset, trains
an XGBoost classifier on a chronological (small-data-safe) split, evaluates on
the held-out tail, and writes a model artifact + a metrics/feature-importance
report. This is what actually *enables* XGBoost on the data ARES collects.

Run from the repo root:  python -m ml_signal.train_offline

OFFLINE / ADDITIVE: no Supabase writes, no engine changes. See ADR-183.
"""
import argparse
import os
import sys
import json
from typing import Any, List, Tuple, Dict, Optional

import numpy as np
import pandas as pd
import joblib
import xgboost as xgb
from sklearn.metrics import (
    roc_auc_score, precision_score, recall_score, f1_score, brier_score_loss,
)

from .config import MLConfig, DEFAULT_CONFIG
from .dataset import feature_columns
from .calibration import compute_brier_decomposition, compute_calibration_curve, test_tier_significance
from .calibrator import ProbabilityCalibrator
from .stratified_evaluator import StratifiedCalibrationEvaluator


# NOTE: ml_signal/trainer.py already has train_xgboost/evaluate_model, but it
# imports `optuna` at module top (a heavy tuning dep the offline path doesn't
# need). To keep trainer.py untouched AND avoid that dependency, the equivalent
# minimal helpers are inlined here — same XGBoost params (from MLConfig), same
# metrics. Only xgboost + scikit-learn are required.


def _final_refit_sample_size_reason(n_samples: int, n_splits: int) -> Optional[str]:
    """Return the normal gate-rejection reason when cross-fitting cannot run."""
    if n_samples <= n_splits:
        return (
            "insufficient trade rows for final chronological cross-fitting: "
            f"got {n_samples}, requires more than {n_splits} folds"
        )
    return None


def _final_refit_class_reason(
    df: pd.DataFrame,
    model_name: str,
    embargo_window: pd.Timedelta = pd.Timedelta(minutes=15),
) -> Optional[str]:
    """Return why the reserved training prefix cannot fit a classifier."""
    if df.empty or "label" not in df:
        return f"final {model_name} refit requires labeled rows"
    if "timestamp" not in df:
        return f"final {model_name} refit requires timestamped rows"
    if "resolution_timestamp" not in df:
        return f"final {model_name} refit requires resolution_timestamp"
    train_df, _ = _reserved_refit_split(df, embargo_window=embargo_window)
    if train_df["label"].nunique(dropna=True) < 2:
        return (
            f"final {model_name} refit requires at least two classes in its "
            "reserved training prefix"
        )
    return None


def _stage1_refit_reason(market_df: Optional[pd.DataFrame]) -> Optional[str]:
    """Return why a deployable hybrid Stage 1 model cannot be fitted."""
    if market_df is None or market_df.empty:
        return "no resolved market-movement labels available for final Stage 1 refit"
    if "label" not in market_df or market_df["label"].nunique(dropna=True) < 2:
        return "final Stage 1 refit requires at least two market-movement classes"
    if "resolution_timestamp" not in market_df:
        return "final Stage 1 refit requires resolution_timestamp"
    return None


def _promotion_fold_count(value: str) -> int:
    """Parse a fold count that can satisfy the production promotion gate."""
    folds = int(value)
    if folds < 4:
        raise argparse.ArgumentTypeError("--folds must be at least 4")
    return folds


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


def _reserved_refit_split(
    df: pd.DataFrame,
    train_frac: float = 0.8,
    embargo_window: pd.Timedelta = pd.Timedelta(minutes=15),
    timestamp_col: str = "timestamp",
    resolution_col: str = "resolution_timestamp",
) -> Tuple[pd.DataFrame, pd.DataFrame]:
    """Reserve a chronological tail and purge its overlapping training rows.

    This mirrors ``WalkForwardPurgedCV``: a prefix row is eligible only when
    its label resolves before the evaluation starts and its event timestamp is
    earlier than the configured pre-test embargo boundary. Missing boundary
    timestamps are excluded from the refit rather than treated as safe.
    """
    train_candidate, eval_df = chronological_split(
        df, train_frac=train_frac, date_col=timestamp_col
    )
    if eval_df.empty or resolution_col not in train_candidate.columns:
        return train_candidate.iloc[0:0].copy(), eval_df

    eval_start = pd.to_datetime(
        eval_df[timestamp_col], errors="coerce", utc=True
    ).min()
    return _purge_before_evaluation(
        train_candidate, eval_start, embargo_window, timestamp_col, resolution_col
    ), eval_df


def _purge_before_evaluation(
    df: pd.DataFrame,
    eval_start: pd.Timestamp,
    embargo_window: pd.Timedelta,
    timestamp_col: str = "timestamp",
    resolution_col: str = "resolution_timestamp",
) -> pd.DataFrame:
    """Keep only resolved history outside the evaluation embargo boundary."""
    if pd.isna(eval_start):
        return df.iloc[0:0].copy()

    candidate_timestamps = pd.to_datetime(
        df[timestamp_col], errors="coerce", utc=True
    )
    candidate_resolutions = pd.to_datetime(
        df[resolution_col], errors="coerce", utc=True
    )
    embargo_start = eval_start - embargo_window
    eligible = (
        candidate_timestamps.notna()
        & candidate_resolutions.notna()
        & (candidate_resolutions < eval_start)
        & (candidate_timestamps < embargo_start)
    )
    return df.loc[eligible].reset_index(drop=True)


def _with_persisted_stage1_probability(
    stage1_model,
    eval_df: pd.DataFrame,
    stage1_feature_cols: List[str],
) -> pd.DataFrame:
    """Use the persisted Stage 1 model to build the deployed eval feature."""
    enriched = eval_df.copy()
    enriched["meta_features__market_movement_prob"] = stage1_model.predict_proba(
        enriched.reindex(columns=stage1_feature_cols)
    )[:, 1]
    return enriched


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


def _sharpe_metrics(df: pd.DataFrame, periods_per_year: int = 252) -> Dict[str, object]:
    """Calculate a zero-risk-free-rate Sharpe ratio from daily spot P&L.

    This is a weekly performance diagnostic, not a capital-return Sharpe:
    ARES records NIFTY spot points and does not yet persist capital deployed,
    costs, or option fills. Grouping realized trade P&L by its India exit date
    avoids annualising individual trade observations with unequal hold times.
    """
    metrics: Dict[str, object] = {
        "sharpe_status": "missing_pnl",
        "sharpe_annualized": None,
        "sharpe_daily": None,
        "sharpe_days": 0,
        "sharpe_trades": 0,
        "sharpe_window_start": None,
        "sharpe_window_end": None,
        "sharpe_total_pnl_points": 0.0,
        "sharpe_pnl_unit": "active-trading-day NIFTY spot P&L points",
        "sharpe_day_basis": "active_trading_days",
        "sharpe_missing_exit_timestamps": 0,
        "sharpe_risk_free_rate": 0.0,
    }
    if "pnl_points" not in df:
        return metrics
    if "exit_timestamp" not in df:
        metrics["sharpe_status"] = "missing_exit_timestamp"
        return metrics

    pnl = pd.to_numeric(df["pnl_points"], errors="coerce")
    # Supabase emits both whole-second and fractional-second ISO timestamps.
    # `mixed` preserves both shapes instead of coercing one form to NaT.
    timestamps = pd.to_datetime(
        df["exit_timestamp"], utc=True, errors="coerce", format="mixed",
    )
    entry_timestamps = pd.to_datetime(
        df.get("entry_timestamp"), utc=True, errors="coerce", format="mixed",
    ) if "entry_timestamp" in df else pd.to_datetime(pd.Series(pd.NaT, index=df.index), utc=True)
    
    time_excluded = df.get("time_metrics_excluded", pd.Series(False, index=df.index)).fillna(False).astype(bool)
    
    pnl_valid = pnl.notna() & np.isfinite(pnl)
    metrics["sharpe_missing_exit_timestamps"] = int((pnl_valid & timestamps.isna()).sum())
    
    # ADR says: valid_chronology = (exit_dt >= entry_dt)
    valid_chronology = (timestamps >= entry_timestamps) | entry_timestamps.isna()
    metrics["sharpe_invalid_chronology_count"] = int((timestamps < entry_timestamps).sum())
    
    valid = pnl.notna() & timestamps.notna() & np.isfinite(pnl) & (~time_excluded) & valid_chronology
    if not valid.any():
        if metrics["sharpe_missing_exit_timestamps"]:
            metrics["sharpe_status"] = "missing_exit_timestamp"
        return metrics

    daily = pd.DataFrame({
        "timestamp": timestamps[valid],
        "pnl_points": pnl[valid],
    })
    daily["date"] = daily["timestamp"].dt.tz_convert("Asia/Kolkata").dt.date
    daily = daily.groupby("date", sort=True)["pnl_points"].sum()

    metrics.update({
        "sharpe_trades": int(valid.sum()),
        "sharpe_days": int(len(daily)),
        "sharpe_window_start": daily.index.min().isoformat(),
        "sharpe_window_end": daily.index.max().isoformat(),
        "sharpe_total_pnl_points": round(float(daily.sum()), 6),
    })
    if len(daily) < 2:
        metrics["sharpe_status"] = "insufficient_days"
        return metrics

    daily_std = float(daily.std(ddof=1))
    if not np.isfinite(daily_std) or daily_std == 0.0:
        metrics["sharpe_status"] = "zero_variance"
        return metrics

    daily_sharpe = float(daily.mean()) / daily_std
    metrics.update({
        "sharpe_status": "computed",
        "sharpe_daily": round(daily_sharpe, 6),
        "sharpe_annualized": round(daily_sharpe * np.sqrt(periods_per_year), 6),
    })
    return metrics


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


def _positive_class_base_value(
    base_values,
    n_rows: int,
    class_expected_value: bool = False,
) -> np.ndarray:
    """Normalize SHAP expected/base values to one binary-class value per row."""
    if base_values is None:
        raise ValueError("SHAP expected value is unavailable")
    values = np.asarray(base_values, dtype=float)
    if values.ndim == 0:
        return np.full(n_rows, float(values))
    if values.ndim == 1:
        if class_expected_value and len(values) >= 2:
            return np.full(n_rows, float(values[1]))
        if len(values) == n_rows:
            return values
        if len(values) >= 2:
            return np.full(n_rows, float(values[1]))
    if values.ndim == 2:
        if values.shape == (n_rows, 1):
            return values[:, 0]
        if values.shape[0] == n_rows and values.shape[1] >= 2:
            return values[:, 1]
        if values.shape[1] == n_rows and values.shape[0] >= 2:
            return values[1]
    raise ValueError("SHAP expected value shape mismatch")


def _validate_external_shap_additivity(
    model,
    X_test: pd.DataFrame,
    values: np.ndarray,
    base_values,
    best_iteration: int,
    class_expected_value: bool = False,
) -> None:
    """Verify external TreeSHAP values reconstruct the raw XGBoost margin."""
    raw_margin = np.asarray(model.get_booster().predict(
        xgb.DMatrix(X_test),
        output_margin=True,
        iteration_range=(0, best_iteration + 1),
    ))
    if raw_margin.ndim != 1 or not np.isfinite(raw_margin).all():
        raise ValueError("external SHAP raw margins contain non-finite values")
    base = _positive_class_base_value(
        base_values, len(X_test), class_expected_value=class_expected_value
    )
    if not np.allclose(
        values.sum(axis=1) + base,
        raw_margin,
        rtol=1e-4,
        atol=1e-5,
    ):
        raise ValueError("external SHAP contributions are not additive to raw margin")


def _compute_shap(
    model,
    X_test: pd.DataFrame,
    feature_cols: List[str],
    metrics: Dict[str, object],
):
    """Populate metrics with optional held-out-set SHAP analysis."""
    best_iteration = getattr(model, "best_iteration", None)
    if best_iteration is None:
        best_iteration = getattr(model, "n_estimators", 1) - 1
    best_iteration = int(best_iteration)
    metrics["shap_iteration_range"] = [0, best_iteration + 1]
    import_failure = None
    try:
        import shap
    except ModuleNotFoundError as exc:
        import_failure = exc
    except Exception as exc:
        import_failure = exc

    values = None
    tree_exc = import_failure
    if tree_exc is None:
        try:
            explainer = shap.TreeExplainer(
                model,
                feature_perturbation="tree_path_dependent",
            )
            # `tree_limit` is the SHAP API's equivalent of XGBoost's iteration range.
            explanation = None
            if hasattr(explainer, "shap_values"):
                values = explainer.shap_values(
                    X_test, tree_limit=best_iteration + 1, check_additivity=True,
                )
            else:
                explanation = explainer(
                    X_test, tree_limit=best_iteration + 1, check_additivity=True,
                )
                values = explanation.values
            base_values = getattr(explanation, "base_values", None)
            class_expected_value = base_values is None
            if base_values is None:
                base_values = getattr(explainer, "expected_value", None)
            if isinstance(values, list):
                values = values[1] if len(values) > 1 else values[0]
            values = np.asarray(values, dtype=float)
            
            if values.ndim == 3:
                values = values[:, :, 1] if values.shape[-1] == 2 else values[:, :, -1]
                
            expected_shape = (len(X_test), len(feature_cols))
            if values.shape != expected_shape:
                raise ValueError("SHAP contribution shape mismatch")
            if not np.isfinite(values).all():
                raise ValueError("SHAP contributions contain non-finite values")
            _validate_external_shap_additivity(
                model,
                X_test,
                values,
                base_values,
                best_iteration,
                class_expected_value=class_expected_value,
            )
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
            return None

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
    return values



def _save_shap_beeswarm_plot(
    values: np.ndarray,
    X_test: pd.DataFrame,
    feature_cols: List[str],
    beeswarm_path: Optional[str],
    backend: str,
) -> None:
    if not beeswarm_path:
        return
    if values is None:
        # A version can be rerun after a previously successful SHAP attempt.
        # Do not leave an old beeswarm implying that this run produced one.
        if (
            beeswarm_path.lower().endswith(".png")
            and os.path.isfile(beeswarm_path)
            and not os.path.islink(beeswarm_path)
        ):
            try:
                os.remove(beeswarm_path)
            except OSError as exc:
                print(
                    "[!] Stale SHAP beeswarm could not be removed; continuing "
                    f"({type(exc).__name__})."
                )
        return
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        os.makedirs(os.path.dirname(beeswarm_path) or ".", exist_ok=True)
        fig = None
        try:
            if backend == "shap_tree_explainer":
                try:
                    import shap
                    shap.summary_plot(values, X_test, show=False, max_display=15)
                    fig = plt.gcf()
                except Exception as e:
                    print(f"[!] shap.summary_plot failed ({e}), falling back to Matplotlib beeswarm.")
                    plt.close("all")

            if fig is None:
                # Native fallback beeswarm (Matplotlib directional scatter/jitter)
                fig, ax = plt.subplots(figsize=(10, 6))
                means = np.mean(np.abs(values), axis=0)
                top_indices = np.argsort(means)[-15:][::-1]
                scatter = None
                rng = np.random.default_rng(0)
                for i, feat_idx in enumerate(top_indices):
                    feat_vals = pd.to_numeric(
                        X_test.iloc[:, feat_idx], errors="coerce"
                    ).to_numpy(dtype=float)
                    shap_vals = values[:, feat_idx]

                    # Normalize feature values to [0, 1]
                    finite = np.isfinite(feat_vals)
                    feat_min = feat_vals[finite].min() if finite.any() else 0.0
                    feat_max = feat_vals[finite].max() if finite.any() else 0.0
                    if feat_max > feat_min:
                        norm_vals = np.nan_to_num(
                            (feat_vals - feat_min) / (feat_max - feat_min),
                            nan=0.5,
                        )
                    else:
                        norm_vals = np.full_like(feat_vals, 0.5)

                    jitter = rng.uniform(-0.15, 0.15, size=len(shap_vals))
                    y_pos = np.full_like(shap_vals, i) + jitter

                    scatter = ax.scatter(
                        shap_vals, y_pos, c=norm_vals, cmap="coolwarm",
                        s=10, alpha=0.7,
                    )

                ax.axvline(x=0, color="k", linestyle="-", linewidth=0.5)
                ax.set_yticks(range(len(top_indices)))
                ax.set_yticklabels([feature_cols[idx] for idx in top_indices])
                ax.set_xlabel("SHAP value (impact on model output)")
                ax.set_title("SHAP Beeswarm Plot (Native Fallback)")

                if scatter is not None:
                    cbar = fig.colorbar(scatter, ax=ax)
                    cbar.set_label("Feature value")

            fig.savefig(beeswarm_path, dpi=150, bbox_inches="tight")
            print(f"[+] SHAP beeswarm plot saved -> {beeswarm_path}")
        finally:
            if fig is not None:
                plt.close(fig)
    except Exception as exc:
        print(f"[!] SHAP beeswarm plot could not be saved; continuing ({type(exc).__name__}).")
        if (
            beeswarm_path.lower().endswith(".png")
            and os.path.isfile(beeswarm_path)
            and not os.path.islink(beeswarm_path)
        ):
            try:
                os.remove(beeswarm_path)
            except OSError as cleanup_exc:
                print(
                    "[!] Stale SHAP beeswarm could not be removed after render "
                    f"failure; continuing ({type(cleanup_exc).__name__})."
                )

def audit_shap_stability(
    df: pd.DataFrame,
    feature_cols: List[str],
    model,
    n_windows: int = 4,
    min_window_samples: int = 30,
) -> Dict[str, object]:
    """Audit SHAP stability across rolling training windows to detect feature drift."""
    if "timestamp" not in df.columns:
        return {"status": "missing_timestamp_column"}

    if n_windows < 2 or min_window_samples < 1:
        return {"status": "invalid_window_configuration"}

    n = len(df)
    if n < 2 * min_window_samples:
        return {"status": "insufficient_data"}

    sorted_df = df.assign(
        timestamp=pd.to_datetime(df["timestamp"], errors="coerce", utc=True)
    ).dropna(subset=["timestamp"]).sort_values("timestamp").reset_index(drop=True)
    index_windows = np.array_split(np.arange(len(sorted_df)), n_windows)
    if any(len(indices) < min_window_samples for indices in index_windows):
        return {"status": "insufficient_data"}

    window_shaps = []
    best_iteration = getattr(model, "best_iteration", None)
    if best_iteration is None:
        best_iteration = getattr(model, "n_estimators", 1) - 1
    best_iteration = int(best_iteration)

    for indices in index_windows:
        w_df = sorted_df.iloc[indices]
        # Extract native SHAP for window
        try:
            w_X = w_df[feature_cols]
            w_dtest = xgb.DMatrix(w_X)
            booster = model.get_booster()
            w_contribs = np.asarray(booster.predict(
                w_dtest,
                pred_contribs=True,
                iteration_range=(0, best_iteration + 1),
            ))
            if w_contribs.ndim != 2 or w_contribs.shape[1] != len(feature_cols) + 1:
                continue
            w_values = w_contribs[:, :-1]
            if not np.isfinite(w_values).all():
                continue
            w_mean_abs = np.mean(np.abs(w_values), axis=0)
            window_shaps.append(w_mean_abs)
        except Exception:
            pass

    if len(window_shaps) < 2:
        return {"status": "insufficient_data"}

    correlations = []
    turnover_rates = []
    drift_scores = []
    flagged_features = set()

    for i in range(1, len(window_shaps)):
        prev = window_shaps[i-1]
        curr = window_shaps[i]

        # Rank correlation
        prev_ranks = np.argsort(np.argsort(-prev))
        curr_ranks = np.argsort(np.argsort(-curr))

        d_sq = np.sum((prev_ranks - curr_ranks) ** 2)
        M = len(feature_cols)
        rho = 1 - (6 * d_sq) / (M * (M**2 - 1)) if M > 1 else 1.0
        correlations.append(rho)

        # Top 5 turnover
        prev_top5 = set(np.argsort(-prev)[:5])
        curr_top5 = set(np.argsort(-curr)[:5])
        denom = float(min(5, len(feature_cols)))
        turnover = len(prev_top5 - curr_top5) / denom if denom > 0 else 0.0
        turnover_rates.append(turnover)

        # Drift score
        drift = np.abs(curr - prev) / (prev + 1e-6)
        drift_scores.append(drift)

        # Flag features with > 100% swing in top features
        for j in curr_top5:
            if drift[j] > 1.0:
                flagged_features.add(feature_cols[j])

    mean_rho = float(np.mean(correlations))
    mean_turnover = float(np.mean(turnover_rates))
    
    if flagged_features or mean_rho < 0.65 or mean_turnover > 0.4:
        verdict = "drift_detected"
    else:
        verdict = "stable"
        
    return {
        "status": "computed",
        "mean_rank_correlation": mean_rho,
        "mean_top5_turnover": mean_turnover,
        "max_attribution_drift": float(np.max(drift_scores)),
        "stability_verdict": verdict,
        "flagged_features": sorted(flagged_features),
    }


def _save_shap_plot(metrics: Dict[str, object], shap_plot_path: Optional[str]) -> None:
    """Save the optional headless top-feature SHAP plot without aborting training."""
    if not shap_plot_path:
        return
    if not metrics["shap_computed"]:
        if (
            shap_plot_path.lower().endswith(".png")
            and os.path.isfile(shap_plot_path)
            and not os.path.islink(shap_plot_path)
        ):
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
        if (
            shap_plot_path.lower().endswith(".png")
            and os.path.isfile(shap_plot_path)
            and not os.path.islink(shap_plot_path)
        ):
            try:
                os.remove(shap_plot_path)
            except OSError:
                pass


def _offline_report_paths(repo: str, version: str) -> Tuple[str, str, str, str]:
    """Return canonical, versioned, and SHAP plot report paths."""
    report_dir = os.path.join(repo, "reports", "ml")
    return (
        os.path.join(report_dir, "task183_offline_metrics.json"),
        os.path.join(report_dir, f"{version}_offline_metrics.json"),
        os.path.join(report_dir, f"{version}_shap_summary.png"),
        os.path.join(report_dir, f"{version}_shap_beeswarm.png"),
    )


def _clear_versioned_shap_artifacts(repo: str, version: str) -> None:
    """Remove artifacts that must not survive a skipped SHAP generation."""
    _, report_path, shap_plot_path, shap_beeswarm_path = _offline_report_paths(
        repo, version
    )
    for path in (report_path, shap_plot_path, shap_beeswarm_path):
        if os.path.isfile(path) and not os.path.islink(path):
            try:
                os.remove(path)
            except OSError as exc:
                print(
                    "[!] Versioned SHAP artifact could not be removed; "
                    f"continuing ({type(exc).__name__}): {path}"
                )


def _populate_shap_metrics(
    metrics: Dict[str, object],
    model,
    source_df: pd.DataFrame,
    train_df: pd.DataFrame,
    test_df: pd.DataFrame,
    feature_cols: List[str],
    model_version: Optional[str],
    shap_plot_path: Optional[str] = None,
    shap_beeswarm_path: Optional[str] = None,
) -> None:
    """Explain an already-fitted model on the supplied held-out frame."""
    X_test = test_df[feature_cols]
    values = _compute_shap(model, X_test, feature_cols, metrics)
    _save_shap_plot(metrics, shap_plot_path)
    _save_shap_beeswarm_plot(
        values,
        X_test,
        feature_cols,
        shap_beeswarm_path,
        metrics.get("shap_backend", "none"),
    )

    import hashlib
    schema_hash = hashlib.sha256(",".join(feature_cols).encode()).hexdigest()[:8]

    def _boundary(frame: pd.DataFrame, mode: str) -> Optional[str]:
        if "timestamp" not in frame.columns or frame.empty:
            return None
        timestamps = pd.to_datetime(frame["timestamp"], errors="coerce", utc=True).dropna()
        if timestamps.empty:
            return None
        return getattr(timestamps, mode)().isoformat()

    metrics["shap_metadata"] = {
        "model_version": model_version,
        "feature_schema_version": "v1.0",
        "feature_count": len(feature_cols),
        "feature_schema_hash": schema_hash,
        "training_window_start": _boundary(train_df, "min"),
        "training_window_end": _boundary(train_df, "max"),
        "testing_window_start": _boundary(test_df, "min"),
        "testing_window_end": _boundary(test_df, "max"),
        "sample_size_total": metrics.get("n_samples", len(source_df)),
        "sample_size_train": metrics.get("n_train", len(train_df)),
        "sample_size_test": metrics.get("n_test", len(test_df)),
        "shap_backend": metrics.get("shap_backend", "none"),
        "shap_status": metrics.get("shap_status", "not_attempted"),
        "raw_margin_additivity": metrics.get("shap_raw_margin_additivity", False),
        "generated_at": pd.Timestamp.now(tz="UTC").isoformat(),
    }

    try:
        metrics["shap_stability_audit"] = audit_shap_stability(
            source_df, feature_cols, model
        )
    except Exception as exc:
        print(f"[!] Stability audit failed: {exc}")
        metrics["shap_stability_audit"] = {
            "status": "failed",
            "error": type(exc).__name__,
        }


def _generate_shap_report(
    repo: str,
    version: str,
    model,
    train_df: pd.DataFrame,
    feature_cols: List[str],
    test_df: pd.DataFrame,
) -> Dict[str, object]:
    """Generate a report for an existing model on a reserved held-out frame."""
    _, report_path, shap_plot_path, shap_beeswarm_path = _offline_report_paths(
        repo, version
    )
    if model is None or train_df.empty or test_df.empty or not feature_cols:
        _clear_versioned_shap_artifacts(repo, version)
        metrics = _shap_metrics()
        metrics["shap_status"] = "skipped"
        return metrics

    try:
        metrics = _shap_metrics()
        metrics.update({
            "model_version": version,
            "n_samples": int(len(train_df) + len(test_df)),
            "n_train": int(len(train_df)),
            "n_test": int(len(test_df)),
        })
        _populate_shap_metrics(
            metrics,
            model,
            # The audit sorts chronologically and must include the current
            # holdout; keep the separate frames below for split metadata.
            pd.concat([train_df, test_df], ignore_index=True),
            train_df,
            test_df,
            feature_cols,
            version,
            shap_plot_path,
            shap_beeswarm_path,
        )
        os.makedirs(os.path.dirname(report_path) or ".", exist_ok=True)
        with open(report_path, "w") as report_file:
            json.dump(metrics, report_file, indent=2, default=str)
        print(f"[+] Report saved -> {report_path}")
        return metrics
    except Exception as exc:
        _clear_versioned_shap_artifacts(repo, version)
        metrics = _shap_metrics()
        metrics.update({
            "shap_status": "failed",
            "shap_error_type": type(exc).__name__,
        })
        print(f"[!] SHAP report generation failed; continuing ({type(exc).__name__}).")
        return metrics
def _save_reliability_plot(y_true, y_prob, path: Optional[str], curve=None) -> bool:
    if not path:
        return False
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        curve = curve or compute_calibration_curve(y_true, y_prob)
        fig, ax = plt.subplots(figsize=(6, 6))
        try:
            ax.plot([0, 1], [0, 1], "--", color="gray", label="Perfect calibration")
            ax.plot(curve.bin_confidences, curve.bin_accuracies, "o-", label="Model")
            ax.set(xlim=(0, 1), ylim=(0, 1), xlabel="Mean predicted probability",
                   ylabel="Observed win rate", title="Reliability diagram")
            ax.legend(loc="best")
            fig.tight_layout()
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            fig.savefig(path, dpi=150, bbox_inches="tight")
        finally:
            plt.close(fig)
        return True
    except Exception:  # pragma: no cover
        return False  # pragma: no cover
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
    shap_beeswarm_path: Optional[str] = None,
    model_version: Optional[str] = None,
) -> Tuple[object, Dict[str, object]]:
    """
    Train + evaluate on a chronological split. Degrades gracefully on tiny data
    (warns, marks metrics provisional, still trains), but aborts when anomaly
    exclusion leaves no eligible rows. Optionally persists the model and a JSON
    report. Returns (model, metrics).
    """
    sharpe = _sharpe_metrics(df)

    time_excluded = df.get("time_metrics_excluded", pd.Series(False, index=df.index)).fillna(False).astype(bool)
    if time_excluded.any():
        df_train = df[~time_excluded].copy()
    else:
        df_train = df

    if df_train.empty:
        raise ValueError("No training rows remain after applying time_metrics_excluded")

    n = len(df_train)
    provisional = n < min_samples
    if provisional:
        print(f"[!] PROVISIONAL: {n} labeled samples (< {min_samples}). "
              f"Metrics are directional, not production-grade.")

    train, test = chronological_split(df_train, train_frac=train_frac)

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
    calibrator = ProbabilityCalibrator(method=config.calibrator_method)
    try:
        calibrator.fit_cross_validated(model, X_train, y_train, method=config.calibrator_method)
        calibration_status = calibrator.rejection_reason or "fitted"
    except (ValueError, TypeError):  # pragma: no cover
        calibrator.rejection_reason = calibrator.rejection_reason or "insufficient_oof_data"  # pragma: no cover
        calibration_status = calibrator.rejection_reason  # pragma: no cover
    raw_test_probability = np.asarray(model.predict_proba(X_test)[:, 1], dtype=float)
    candidate_probability = np.asarray(calibrator.transform(raw_test_probability), dtype=float)
    test_brier_before = float(np.mean((np.asarray(y_test, dtype=float) - raw_test_probability) ** 2))
    test_brier_after = float(np.mean((np.asarray(y_test, dtype=float) - candidate_probability) ** 2))
    calibration_validated = bool(
        calibrator.is_calibrated and test_brier_after < test_brier_before
    )
    if calibrator.is_calibrated and not calibration_validated:
        calibration_status = "no_heldout_brier_improvement"
    calibrated_probability = candidate_probability if calibration_validated else raw_test_probability
    calibration = compute_brier_decomposition(y_test, calibrated_probability)
    curve = compute_calibration_curve(y_test, calibrated_probability)
    metrics.update({
        "calibration_status": calibration_status,
        "calibrator_method": calibrator.method,
        "calibrator_brier_before": calibrator.brier_before,
        "calibrator_brier_after_oof": calibrator.brier_after,
        "calibration_brier_before_evaluation": test_brier_before,
        "calibration_brier_after_evaluation": test_brier_after,
        "probability_calibrated": calibration_validated,
        "calibration_brier_decomposition": calibration.__dict__,
        "calibration_curve": curve.__dict__,
        "ece": curve.ece,
        "mce": curve.mce,
    })
    significance = None
    if report_path:
        report_root = os.path.dirname(report_path) or "."
    elif save_path:  # pragma: no cover
        report_root = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "reports", "ml")  # pragma: no cover
    else:
        report_root = None
    if "pnl_points" in test:  # pragma: no cover
        ml_tier_frame = pd.DataFrame({  # pragma: no cover
            "confidence": np.where(calibrated_probability >= config.high_threshold, "HIGH",
                np.where(calibrated_probability >= config.medium_threshold, "MEDIUM", "LOW")),
            "win": np.asarray(y_test, dtype=int),
            "pnl_points": pd.to_numeric(test["pnl_points"], errors="coerce").to_numpy(),
        })
        ml_significance = test_tier_significance(ml_tier_frame, alpha=config.calibration_alpha)  # pragma: no cover
        metrics["ml_tier_significance"] = ml_significance.to_dict()  # pragma: no cover
        historical_tier_col = next(  # pragma: no cover
            (col for col in ("signal_tentative_confidence", "signal_confidence") if col in test),
            None,
        )
        if historical_tier_col is not None:  # pragma: no cover
            tier_frame = test[[historical_tier_col, label_col, "pnl_points"]].rename(  # pragma: no cover
                columns={historical_tier_col: "confidence", label_col: "win"}
            )
        else:
            tier_frame = ml_tier_frame  # pragma: no cover
        significance = test_tier_significance(tier_frame, alpha=config.calibration_alpha)  # pragma: no cover
        metrics["tier_significance"] = significance.to_dict()  # pragma: no cover
        stratified = test.copy()  # pragma: no cover
        stratified["confidence"] = tier_frame["confidence"].values  # pragma: no cover
        stratified["win"] = np.asarray(y_test, dtype=int)  # pragma: no cover
        stratified["probability"] = calibrated_probability  # pragma: no cover
        if report_root is not None:  # pragma: no cover
            StratifiedCalibrationEvaluator(stratified).export_json(  # pragma: no cover
                os.path.join(report_root, "confidence_calibration_stratified_report.json"),
                alpha=config.calibration_alpha,
            )
    version_name = model_version or "model"
    reliability_plot_path = (
        os.path.join(report_root, f"{version_name}_reliability_diagram.png")
        if report_root is not None else None
    )
    metrics["reliability_diagram_saved"] = _save_reliability_plot(
        y_test, calibrated_probability, reliability_plot_path, curve,
    )
    metrics.update({
        "n_samples": int(n),
        "n_train": int(len(train)),
        "n_test": int(len(test)),
        "pos_rate": float(df_train[label_col].mean()) if n else float("nan"),
        "provisional": bool(provisional),
    })
    metrics.update(sharpe)
    metrics.update(_shap_metrics())
    if "feature_version" in df_train.columns:
        missingness_by_ver: Dict[str, Any] = {}
        for ver, vdf in df_train.groupby("feature_version"):
            v_missing = {
                col: round(float(vdf[col].isna().mean()), 4)
                for col in feature_cols
                if vdf[col].isna().any()
            }
            missingness_by_ver[str(ver)] = {
                "n_samples": int(len(vdf)),
                "features_with_missing": v_missing,
            }
        metrics["missingness_by_feature_version"] = missingness_by_ver

    if model_version is not None:
        metrics["model_version"] = model_version

    importance = _importance(model, feature_cols)

    metrics["top_features"] = importance.head(15).to_dict(orient="records")
    _populate_shap_metrics(
        metrics,
        model,
        df_train,
        train,
        test,
        feature_cols,
        model_version,
        shap_plot_path,
        shap_beeswarm_path,
    )


    if save_path:
        os.makedirs(os.path.dirname(save_path) or ".", exist_ok=True)
        joblib.dump(model, save_path)
        print(f"[+] Model saved -> {save_path}")
        calibrator_path = f"{save_path}.calibrator.joblib"
        if calibration_validated:  # pragma: no cover
            joblib.dump(calibrator, calibrator_path)  # pragma: no cover
        elif os.path.exists(calibrator_path):  # pragma: no cover
            os.remove(calibrator_path)  # pragma: no cover

    if report_path:
        os.makedirs(os.path.dirname(report_path) or ".", exist_ok=True)
        with open(report_path, "w") as f:
            json.dump(metrics, f, indent=2, default=str)
        print(f"[+] Report saved -> {report_path}")

    if significance is not None and report_root is not None:  # pragma: no cover
        validation_path = os.path.join(report_root, "calibration_validation_record.json")  # pragma: no cover
        auc_value = metrics.get("auc_roc")  # pragma: no cover
        try:  # pragma: no cover
            auc_value = float(auc_value) if np.isfinite(float(auc_value)) else None  # pragma: no cover
        except (TypeError, ValueError):  # pragma: no cover
            auc_value = None  # pragma: no cover
        validation_record = {  # pragma: no cover
            **significance.to_dict(),
            "tier_significance": significance.to_dict(),
            "ml_tier_significance": metrics.get("ml_tier_significance", {}),
            "auc_roc": auc_value,
            "calibration_status": calibration_status,
            "probability_calibrated": calibration_validated,
            "model_version": model_version,
        }
        with open(validation_path, "w", encoding="utf-8") as f:  # pragma: no cover
            json.dump(validation_record, f, indent=2, allow_nan=False)  # pragma: no cover

    return model, metrics


# ─────────────────────────── live-data driver ───────────────────────────────

def _fetch_ml_collection(supabase, page: int = 1000) -> List[dict]:
    """Read-only, paginated pull of ml_collection ordered by timestamp asc."""
    include_feature_version = True
    try:
        supabase.table("ml_collection").select("feature_version").limit(1).execute()
    except Exception:
        include_feature_version = False

    base_cols = ["timestamp", "raw_candle", "trade_id", "trade_outcome", "trade_pnl", "snapshot_uuid", "signal_id", "signal_setup_type", "signal_direction", "signal_confidence"]
    if include_feature_version:
        base_cols.append("feature_version")
    cols = ",".join(base_cols + [
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


def _fetch_trade_exit_timestamps(supabase, page: int = 1000) -> Dict[str, dict]:
    """Read completed trade exit timestamps keyed by the ml_collection trade id."""
    exits: Dict[str, dict] = {}
    start = 0
    while True:
        batch = (
            supabase.table("trade_analytics")
            .select("id,exit_timestamp,entry_timestamp,time_metrics_excluded")
            .order("exit_timestamp")
            .range(start, start + page - 1)
            .execute()
            .data or []
        )
        for trade in batch:
            trade_id = trade.get("id")
            if trade_id is not None:
                exits[str(trade_id)] = {
                    "exit_timestamp": trade.get("exit_timestamp"),
                    "entry_timestamp": trade.get("entry_timestamp"),
                    "time_metrics_excluded": trade.get("time_metrics_excluded", False)
                }
        if len(batch) < page:
            break
        start += page
    return exits


def _write_walk_forward_calibration_artifacts(
    trade_df: pd.DataFrame,
    probabilities: np.ndarray,
    valid_mask: np.ndarray,
    report_dir: str,
    model_version: str,
    mean_auc: Optional[float],
    alpha: float,
    config: MLConfig,
):
    """Fit calibration on OOF predictions and publish runtime/audit artifacts."""
    os.makedirs(report_dir, exist_ok=True)
    calibrator = ProbabilityCalibrator(method=config.calibrator_method)
    valid_mask = np.asarray(valid_mask, dtype=bool)
    probabilities = np.asarray(probabilities, dtype=float)
    labels = pd.to_numeric(trade_df["label"], errors="coerce").fillna(0).to_numpy(dtype=int)
    valid_mask = valid_mask & np.isfinite(probabilities)
    valid_positions = np.flatnonzero(valid_mask)
    raw_oof = probabilities[valid_mask]
    oof_labels = labels[valid_mask]
    # Fit on the earlier OOF segment and evaluate calibration and tier
    # superiority only on the later segment. This keeps the labels used to
    # fit the mapping out of the evidence used by the runtime gate.
    fit_count = int(len(oof_labels) * 0.7)
    eval_start = fit_count
    fit_probability = raw_oof[:fit_count]
    fit_labels = oof_labels[:fit_count]
    eval_probability = raw_oof[eval_start:]
    eval_labels = oof_labels[eval_start:]
    eval_positions = valid_positions[eval_start:]
    pnl_column = next(
        (column for column in ("pnl_points", "trade_pnl", "pnl") if column in trade_df),
        None,
    )
    eval_pnl = (
        pd.to_numeric(trade_df.iloc[eval_positions][pnl_column], errors="coerce")
        if pnl_column is not None
        else pd.Series(np.nan, index=range(len(eval_positions)), dtype=float)
    )
    calibration_metrics: Dict[str, Any] = {
        "calibration_status": "insufficient_oof_data",
        "calibration_fit_samples": int(len(fit_labels)),
        "calibration_samples": int(len(eval_labels)),
        "probability_calibrated": False,
    }
    if len(fit_labels) >= 2 and len(eval_labels) >= 2:
        try:
            calibrator.fit(fit_labels, fit_probability, method=config.calibrator_method)
            calibration_metrics["calibration_status"] = calibrator.rejection_reason or "fitted"
        except ValueError as exc:  # pragma: no cover
            calibrator.rejection_reason = calibrator.rejection_reason or type(exc).__name__  # pragma: no cover
            calibration_metrics["calibration_status"] = calibrator.rejection_reason  # pragma: no cover
    candidate_oof = calibrator.transform(eval_probability)
    eval_brier_before = float(np.mean((eval_probability - eval_labels) ** 2)) if len(eval_labels) else None
    eval_brier_after = float(np.mean((candidate_oof - eval_labels) ** 2)) if len(eval_labels) else None
    calibration_metrics["probability_calibrated"] = bool(
        calibrator.is_calibrated and eval_brier_before is not None
        and eval_brier_after is not None and eval_brier_after < eval_brier_before
    )
    if calibrator.is_calibrated and not calibration_metrics["probability_calibrated"]:
        calibration_metrics["calibration_status"] = "no_heldout_brier_improvement"
    calibrated_oof = candidate_oof if calibration_metrics["probability_calibrated"] else eval_probability
    calibration_metrics["calibrator_brier_before"] = calibrator.brier_before
    calibration_metrics["calibrator_brier_after_fit"] = calibrator.brier_after
    calibration_metrics["calibration_brier_before_evaluation"] = eval_brier_before
    calibration_metrics["calibration_brier_after_evaluation"] = eval_brier_after

    if len(eval_labels):
        decomposition = compute_brier_decomposition(eval_labels, calibrated_oof)
        curve = compute_calibration_curve(eval_labels, calibrated_oof)
        calibration_metrics.update({
            "calibration_brier_decomposition": decomposition.__dict__,
            "calibration_curve": curve.__dict__,
            "ece": curve.ece,
            "mce": curve.mce,
        })
        _save_reliability_plot(
            eval_labels, calibrated_oof,
            os.path.join(report_dir, f"{model_version}_reliability_diagram.png"), curve,
        )

    ml_tier_frame = pd.DataFrame({
        "confidence": np.where(calibrated_oof >= config.high_threshold, "HIGH",
            np.where(calibrated_oof >= config.medium_threshold, "MEDIUM", "LOW")),
        "win": eval_labels,
        "pnl_points": eval_pnl.to_numpy(),
    }) if len(eval_labels) else pd.DataFrame(columns=["confidence", "win", "pnl_points"])
    ml_significance = test_tier_significance(
        ml_tier_frame, alpha=alpha, min_samples=30,
    )
    calibration_metrics["ml_tier_significance"] = ml_significance.to_dict()

    historical_tier_col = next(
        (col for col in ("signal_tentative_confidence", "signal_confidence") if col in trade_df),
        None,
    )
    if historical_tier_col is not None:
        historical_tiers = trade_df.iloc[eval_positions][[historical_tier_col, "label"]].rename(
            columns={historical_tier_col: "confidence", "label": "win"}
        )
        historical_tiers["pnl_points"] = eval_pnl.to_numpy()
    else:
        historical_tiers = ml_tier_frame
    tier_significance = test_tier_significance(
        historical_tiers, alpha=alpha, min_samples=30,
    )
    calibration_metrics["tier_significance"] = tier_significance.to_dict()

    stratified = trade_df.iloc[eval_positions].copy()
    stratified["pnl_points"] = eval_pnl.to_numpy()
    stratified["confidence"] = (
        stratified[historical_tier_col] if historical_tier_col is not None
        else np.where(
            np.isfinite(eval_probability),
            np.where(eval_probability >= config.high_threshold, "HIGH",
                np.where(eval_probability >= config.medium_threshold, "MEDIUM", "LOW")),
            "LOW",
        )
    )
    stratified["win"] = eval_labels
    stratified["probability"] = calibrated_oof
    StratifiedCalibrationEvaluator(stratified).export_json(
        os.path.join(report_dir, "confidence_calibration_stratified_report.json"), alpha=alpha,
    )

    record = {
        **tier_significance.to_dict(),
        "tier_significance": tier_significance.to_dict(),
        "ml_tier_significance": ml_significance.to_dict(),
        "auc_roc": float(mean_auc) if mean_auc is not None and np.isfinite(mean_auc) else None,
        "calibration_status": calibration_metrics["calibration_status"],
        "probability_calibrated": calibration_metrics["probability_calibrated"],
        "model_version": model_version,
    }
    with open(os.path.join(report_dir, "calibration_validation_record.json"), "w", encoding="utf-8") as handle:
        json.dump(record, handle, indent=2, allow_nan=False)
    calibration_metrics["validation_record_path"] = os.path.join(report_dir, "calibration_validation_record.json")
    calibration_metrics["stratified_report_path"] = os.path.join(
        report_dir, "confidence_calibration_stratified_report.json"
    )
    return calibrator if calibration_metrics["probability_calibrated"] else None, calibration_metrics


from ml_signal.pipeline_market_movement import MarketMovementPipeline
from ml_signal.pipeline_trade_outcomes import TradeOutcomePipeline
from ml_signal.promotion_gate import enforce_promotion_or_raise, ModelPromotionError
from ml_signal.predictor import HybridPredictorBundle


def _fit_market_model(df: pd.DataFrame, feature_cols: List[str], config: MLConfig):
    """Fit the final Stage 1 model on the reserved training portion."""
    model = xgb.XGBClassifier(
        n_estimators=config.n_estimators,
        learning_rate=config.learning_rate,
        max_depth=config.max_depth,
        subsample=config.subsample,
        colsample_bytree=config.colsample_bytree,
        random_state=42,
    )
    model.fit(df[feature_cols], df["label"])
    return model


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description="ARES Offline ML Training")
    parser.add_argument("--pipeline", choices=["market_movement", "trade_outcomes", "all"], default="all")
    parser.add_argument(
        "--folds",
        type=_promotion_fold_count,
        default=5,
        metavar="N",
        help="Walk-forward split count (minimum: 4; default: 5)",
    )
    parser.add_argument("--promote", action="store_true", default=True, help="Promote model if it passes gates")
    parser.add_argument("--no-promote", action="store_false", dest="promote")
    parser.add_argument("--enforce-gate", action="store_true", default=False)
    parser.add_argument("--hybrid", action="store_true", default=True)
    parser.add_argument("--no-hybrid", action="store_false", dest="hybrid")
    parser.add_argument("--metrics-path", type=str, default=os.environ.get("METRICS_PATH", "reports/ml/task183_offline_metrics.json"))
    args = parser.parse_args(argv)

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
    print("[*] Reading ml_collection (read-only)...")
    rows = _fetch_ml_collection(supabase)
    exit_timestamps = _fetch_trade_exit_timestamps(supabase)
    for row in rows:
        trade_id = row.get("trade_id")
        if trade_id and str(trade_id) in exit_timestamps:
            metadata = exit_timestamps[str(trade_id)]
            row["exit_timestamp"] = metadata["exit_timestamp"]
            row["entry_timestamp"] = metadata["entry_timestamp"]
            row["time_metrics_excluded"] = metadata["time_metrics_excluded"]
        else:
            row["exit_timestamp"] = None
            row["entry_timestamp"] = None
            row["time_metrics_excluded"] = False

    from ml_signal.predictor import get_next_model_version_and_path
    models_dir = os.path.join(repo, "ml_signal", "models")
    save_path, next_version = get_next_model_version_and_path(models_dir)
    # Rejected runs reuse the version. Invalidate its prior explanations before
    # any pipeline/refit can fail, including failures before the report guard.
    _clear_versioned_shap_artifacts(repo, next_version)

    stage1_model, stage2_model = None, None
    runtime_calibrator = None
    market_df = None
    promoted = False
    gate_error = None
    leakage_guard_passed = True
    final_metrics = {}
    sharpe_metrics = _sharpe_metrics(pd.DataFrame())
    shap_metrics = _shap_metrics()
    shap_metrics["shap_status"] = "skipped"
    shap_model = None
    shap_source_df = None
    shap_eval_df = None
    shap_feature_cols = []
    market_model_for_save = None

    if args.pipeline in ["market_movement", "all"]:
        print("\n=== Running Market Movement Pipeline ===")
        pipe = MarketMovementPipeline(config)
        market_df = pipe.prepare_dataset(rows)
        stage1_model, metrics = pipe.run_walk_forward(market_df, n_splits=args.folds)
        
        # Save secondary report
        market_report = os.path.join(repo, "reports", "ml", "market_movement_metrics.json")
        os.makedirs(os.path.dirname(market_report), exist_ok=True)
        with open(market_report, "w") as f:
            json.dump(metrics, f, indent=2)
            
        if args.pipeline == "market_movement":
            final_metrics = metrics.copy()
            leakage_guard_passed = metrics.get("leakage_guard_passed", False)
            shap_feature_cols = [
                c for c in feature_columns(market_df)
                if not c.startswith("detector_scores__")
            ]
            if stage1_model is not None and len(market_df) > 1:
                market_train_df, market_eval_df = _reserved_refit_split(
                    market_df, embargo_window=pd.Timedelta(minutes=15)
                )
                market_class_reason = _final_refit_class_reason(
                    market_df,
                    "Stage 1",
                    embargo_window=pd.Timedelta(minutes=15),
                )
                if market_class_reason is None:
                    stage1_model = _fit_market_model(
                        market_train_df, shap_feature_cols, config
                    )
                    market_model_for_save = stage1_model
                    shap_model = stage1_model
                    shap_source_df = market_train_df
                    shap_eval_df = market_eval_df
                else:
                    print(f"[!] Skipping final Stage 1 refit: {market_class_reason}")
                    stage1_model = None

        if args.pipeline == "all" and not args.hybrid:
            market_model_for_save = stage1_model

        if args.promote and market_model_for_save is not None:
            mm_save_path = os.path.join(models_dir, f"market_movement_{next_version}.joblib")
            joblib.dump(market_model_for_save, mm_save_path)

    if args.pipeline in ["trade_outcomes", "all"]:
        print("\n=== Running Trade Outcome Pipeline ===")
        pipe = TradeOutcomePipeline(config, use_hybrid_transfer=args.hybrid)
        trade_df = pipe.prepare_dataset(rows)
        # Sharpe is a realized-trade diagnostic, not a CV model metric.  Keep
        # it sourced from the outcome frame even when promotion is rejected.
        sharpe_metrics = _sharpe_metrics(trade_df)
        
        # We need market_df for cross-fitting if hybrid
        if args.hybrid and market_df is None:
            # Reconstruct just for transfer
            pipe1 = MarketMovementPipeline(config)
            market_df = pipe1.prepare_dataset(rows)
            
        _, metrics = pipe.run_walk_forward(trade_df, market_snapshots_df=market_df, n_splits=args.folds)

        final_metrics = metrics.copy()
        leakage_guard_passed = metrics.get("leakage_guard_passed", False)
        oof_predictions = getattr(pipe, "oof_predictions", None)
        oof_mask = getattr(pipe, "oof_mask", None)
        if not trade_df.empty and oof_predictions is not None and oof_mask is not None:
            runtime_calibrator, calibration_metrics = _write_walk_forward_calibration_artifacts(
                trade_df=trade_df,
                probabilities=oof_predictions,
                valid_mask=oof_mask,
                report_dir=os.path.join(repo, "reports", "ml"),
                model_version=next_version,
                mean_auc=metrics.get("mean_auc"),
                alpha=config.calibration_alpha,
                config=config,
            )
            final_metrics.update(calibration_metrics)
        
        if args.promote:
            from ml_signal.promotion_gate import evaluate_promotion_gate
            passed, reasons = evaluate_promotion_gate(metrics)
            refit_size_reason = _final_refit_sample_size_reason(len(trade_df), args.folds)
            stage2_class_reason = (
                _final_refit_class_reason(
                    trade_df,
                    "Stage 2",
                    embargo_window=pd.Timedelta(minutes=30),
                )
                if refit_size_reason is None else None
            )
            if refit_size_reason:
                passed = False
                reasons = [*reasons, refit_size_reason]
            if stage2_class_reason:
                passed = False
                reasons = [*reasons, stage2_class_reason]
            stage1_refit_reason = _stage1_refit_reason(market_df) if args.hybrid else None
            # Materialize and validate the exact frames used by the final fit.
            # Both stages must respect the trade evaluation boundary.
            if refit_size_reason is None and stage2_class_reason is None:
                trade_train_df, trade_eval_df = _reserved_refit_split(
                    trade_df, embargo_window=pd.Timedelta(minutes=30)
                )
                if args.hybrid and stage1_refit_reason is None:
                    trade_eval_start = pd.to_datetime(
                        trade_eval_df["timestamp"], errors="coerce", utc=True
                    ).min()
                    market_train_df = _purge_before_evaluation(
                        market_df, trade_eval_start, pd.Timedelta(minutes=15)
                    )
                    stage1_refit_reason = _stage1_refit_reason(market_train_df)
            if stage1_refit_reason:
                passed = False
                reasons = [*reasons, stage1_refit_reason]
            promoted = passed
            if not passed:
                gate_error = ModelPromotionError(f"Promotion gate failed: {reasons}", reasons=reasons)
                print(f"[!] Promotion Gate Failed: {reasons}")
                
            # Reserve chronological tails for evaluation before final fitting.
            print("[*] Training final Stage 1 and Stage 2 models for promotion or research...")
            try:
                refit_reason = (
                    refit_size_reason
                    or stage2_class_reason
                    or stage1_refit_reason
                )
                if refit_reason:
                    print(f"[!] Skipping final refit: {refit_reason}")
                if not refit_reason and args.hybrid:
                    stage1_feat_cols = [c for c in feature_columns(market_df) if not c.startswith("detector_scores__")]

                    # Generate historical transfer probabilities with the same
                    # resolution purge and embargo used by the final Stage 1.
                    from sklearn.model_selection import TimeSeriesSplit
                    tscv = TimeSeriesSplit(n_splits=args.folds)
                    
                    # Ensure trade_df is sorted
                    trade_df = trade_df.sort_values("timestamp").reset_index(drop=True)
                    market_probs = pd.Series(index=trade_df.index, dtype=float)
                    for _, test_idx in tscv.split(trade_df):
                        test_start_ts = trade_df.iloc[test_idx]["timestamp"].min()

                        train_market = _purge_before_evaluation(
                            market_df, test_start_ts, pd.Timedelta(minutes=15)
                        )
                        if len(train_market) < 50 or train_market["label"].nunique() < 2:
                            continue
                            
                        fold_model = xgb.XGBClassifier(n_estimators=50, max_depth=3, random_state=42)
                        fold_model.fit(train_market[stage1_feat_cols], train_market["label"])
                        
                        # Predict for test_idx
                        test_trades = trade_df.iloc[test_idx]
                        market_probs.iloc[test_idx] = fold_model.predict_proba(test_trades.reindex(columns=stage1_feat_cols))[:, 1]
                    
                    # Preserve the chronological fallback for unscored early rows.
                    market_probs = market_probs.ffill().fillna(0.5)
                    trade_df["meta_features__market_movement_prob"] = market_probs
                    # Splits are independent frames: refresh after augmentation
                    # so Stage 2 sees the generated probabilities, never stale input.
                    trade_train_df, trade_eval_df = _reserved_refit_split(
                        trade_df, embargo_window=pd.Timedelta(minutes=30)
                    )
                    
                    # Fit final Stage 1 model for deployment
                    stage1_model = xgb.XGBClassifier(n_estimators=50, max_depth=3, random_state=42)
                    stage1_model.fit(market_train_df[stage1_feat_cols], market_train_df["label"])
                    market_model_for_save = stage1_model
                    
                if not refit_reason:
                    feat_cols2 = ["meta_features__market_movement_prob"] if args.hybrid else []
                    feat_cols2 += [
                        c for c in [
                            "structure_features__dist_to_nearest_support",
                            "structure_features__dist_to_nearest_resistance",
                            "oi_features__pcr_oi",
                            "candle_features__body_pct",
                            "iv_features__iv_level",
                            "greek_features__net_delta"
                        ] if c in trade_df.columns
                    ]

                    stage2_model = xgb.XGBClassifier(
                        n_estimators=50,
                        learning_rate=0.03,
                        max_depth=2,
                        reg_lambda=5.0,
                        reg_alpha=1.0,
                        colsample_bytree=0.6,
                        subsample=0.7,
                        random_state=42
                    )
                    stage2_model.fit(trade_train_df[feat_cols2], trade_train_df["label"])
                    shap_model = stage2_model
                    shap_source_df = trade_train_df
                    shap_eval_df = (
                        _with_persisted_stage1_probability(
                            stage1_model, trade_eval_df, stage1_feat_cols
                        )
                        if args.hybrid else trade_eval_df
                    )
                    shap_feature_cols = feat_cols2

                    if not promoted:
                        save_path = save_path.replace(".joblib", "_unpromoted.joblib")

                    if args.hybrid:
                        bundle = HybridPredictorBundle(
                            stage1_model=stage1_model,
                            stage2_model=stage2_model,
                            stage1_feature_names=stage1_feat_cols,
                            stage2_feature_names=feat_cols2,
                            model_version=next_version,
                            created_at=str(pd.Timestamp.utcnow()),
                            metrics_summary=metrics
                        )
                        joblib.dump(bundle, save_path)
                    else:
                        # Persist as legacy standalone model
                        joblib.dump(stage2_model, save_path)

                    calibrator_path = f"{save_path}.calibrator.joblib"
                    if runtime_calibrator is not None:  # pragma: no cover
                        joblib.dump(runtime_calibrator, calibrator_path)  # pragma: no cover
                    elif os.path.exists(calibrator_path):  # pragma: no cover
                        os.remove(calibrator_path)  # pragma: no cover
                        
                    if promoted:
                        print(f"[+] Promoted Model -> {save_path}")
                    else:
                        print(f"[!] Saved unpromoted artifact -> {save_path}")

            except ModelPromotionError as e:
                pass

            if args.promote and args.hybrid and market_model_for_save is not None:
                mm_save_path = os.path.join(models_dir, f"market_movement_{next_version}.joblib")
                joblib.dump(market_model_for_save, mm_save_path)
            
            if not promoted:
                # Write rejection audit
                audit_path = f"reports/ml/{next_version}_rejection_audit.json"
                os.makedirs(os.path.dirname(audit_path) or ".", exist_ok=True)
                with open(audit_path, "w") as f:
                    json.dump({"version": next_version, "reasons": gate_error.reasons if gate_error else [], "metrics": metrics}, f, indent=2, default=str)
                print(f"[!] Rejection audit saved -> {audit_path}")

    if shap_model is not None and shap_source_df is not None and shap_eval_df is not None:
        shap_metrics = _generate_shap_report(
            repo,
            next_version,
            shap_model,
            shap_source_df,
            shap_feature_cols,
            shap_eval_df,
        )
    else:
        # Final refit guards can bypass report generation entirely. Remove the
        # prior version's artifacts so rejected runs cannot publish stale SHAP.
        _clear_versioned_shap_artifacts(repo, next_version)

    # Write summary metrics
    summary = {
        "model_version": next_version,
        "auc_roc": final_metrics.get("mean_auc", 0.0),
        "promoted": promoted,
        "leakage_guard_passed": leakage_guard_passed,
        "gate_reasons": gate_error.reasons if gate_error else []
    }
    summary.update(final_metrics)
    summary.update(sharpe_metrics)
    summary.update(shap_metrics)
    
    os.makedirs(os.path.dirname(args.metrics_path) or ".", exist_ok=True)
    with open(args.metrics_path, "w") as f:
        json.dump(summary, f, indent=2, default=str)
    print(f"[+] Summary report saved -> {args.metrics_path}")
    
    if gate_error and args.enforce_gate:
        raise gate_error

if __name__ == "__main__":
    main()
