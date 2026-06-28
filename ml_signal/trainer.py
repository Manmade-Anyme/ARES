from datetime import datetime
from typing import Optional, Tuple, List, Dict

import numpy as np
import pandas as pd
import xgboost as xgb
from sklearn.model_selection import TimeSeriesSplit
from sklearn.metrics import roc_auc_score, precision_score, recall_score, f1_score, brier_score_loss
from sklearn.calibration import CalibratedClassifierCV

import joblib
import optuna

from .config import MLConfig, DEFAULT_CONFIG


def walk_forward_split(
    df: pd.DataFrame,
    date_col: str = "timestamp",
    n_splits: int = 4,
    train_months: int = 6,
    test_months: int = 1,
) -> List[Tuple[pd.DataFrame, pd.DataFrame]]:
    df = df.sort_values(date_col).reset_index(drop=True)
    df["_date"] = pd.to_datetime(df[date_col])

    min_date = df["_date"].min()
    max_date = df["_date"].max()

    splits = []
    current_start = min_date

    while True:
        train_end = current_start + pd.DateOffset(months=train_months)
        test_end = train_end + pd.DateOffset(months=test_months)

        if test_end > max_date:
            break

        train = df[(df["_date"] >= current_start) & (df["_date"] < train_end)]
        test = df[(df["_date"] >= train_end) & (df["_date"] < test_end)]

        if len(train) > 0 and len(test) > 0:
            splits.append((train, test))

        current_start = current_start + pd.DateOffset(months=test_months)

    return splits


def train_xgboost(
    X_train: pd.DataFrame,
    y_train: pd.Series,
    X_val: pd.DataFrame,
    y_val: pd.Series,
    config: MLConfig = DEFAULT_CONFIG,
) -> xgb.XGBClassifier:
    scale_pos_weight = (y_train == 0).sum() / max((y_train == 1).sum(), 1)

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

    model.fit(
        X_train, y_train,
        eval_set=[(X_val, y_val)],
        verbose=50,
    )

    return model


def evaluate_model(
    model,
    X_test: pd.DataFrame,
    y_test: pd.Series,
) -> Dict[str, float]:
    y_pred = model.predict(X_test)
    y_proba = model.predict_proba(X_test)[:, 1]

    return {
        "auc_roc": roc_auc_score(y_test, y_proba),
        "precision": precision_score(y_test, y_pred, zero_division=0),
        "recall": recall_score(y_test, y_pred, zero_division=0),
        "f1": f1_score(y_test, y_pred, zero_division=0),
        "brier_score": brier_score_loss(y_test, y_proba),
        "accuracy": (y_pred == y_test).mean(),
        "precision_top20": _precision_at_top_k(y_test, y_proba, 0.2),
    }


def _precision_at_top_k(
    y_true: pd.Series,
    y_proba: np.ndarray,
    top_k_frac: float,
) -> float:
    n_top = max(1, int(len(y_true) * top_k_frac))
    top_indices = np.argsort(y_proba)[-n_top:]
    return y_true.iloc[top_indices].mean()


def get_feature_importance(model, feature_names: List[str]) -> pd.DataFrame:
    importance = model.feature_importances_
    df = pd.DataFrame({
        "feature": feature_names,
        "gain": importance,
    }).sort_values("gain", ascending=False)
    return df


def objective(
    trial,
    X_train: pd.DataFrame,
    y_train: pd.Series,
    X_val: pd.DataFrame,
    y_val: pd.Series,
) -> float:
    params = {
        "n_estimators": trial.suggest_int("n_estimators", 200, 800, step=50),
        "learning_rate": trial.suggest_float("learning_rate", 0.01, 0.1, log=True),
        "max_depth": trial.suggest_int("max_depth", 3, 8),
        "subsample": trial.suggest_float("subsample", 0.6, 1.0),
        "colsample_bytree": trial.suggest_float("colsample_bytree", 0.5, 1.0),
        "gamma": trial.suggest_float("gamma", 0, 5),
        "reg_lambda": trial.suggest_float("reg_lambda", 0, 5),
        "min_child_weight": trial.suggest_int("min_child_weight", 1, 10),
    }

    scale_pos_weight = (y_train == 0).sum() / max((y_train == 1).sum(), 1)
    params["scale_pos_weight"] = scale_pos_weight

    model = xgb.XGBClassifier(
        **params,
        eval_metric="logloss",
        early_stopping_rounds=30,
        tree_method="hist",
        random_state=42,
        n_jobs=-1,
    )

    model.fit(
        X_train, y_train,
        eval_set=[(X_val, y_val)],
        verbose=0,
    )

    y_proba = model.predict_proba(X_val)[:, 1]
    return _precision_at_top_k(y_val, y_proba, 0.2)


def run_hyperparameter_tuning(
    X_train: pd.DataFrame,
    y_train: pd.Series,
    X_val: pd.DataFrame,
    y_val: pd.Series,
    n_trials: int = 50,
) -> optuna.Study:
    study = optuna.create_study(direction="maximize")
    study.optimize(
        lambda trial: objective(trial, X_train, y_train, X_val, y_val),
        n_trials=n_trials,
    )
    return study


def train_pipeline(
    df: pd.DataFrame,
    feature_cols: List[str],
    label_col: str = "label",
    config: MLConfig = DEFAULT_CONFIG,
    run_optuna: bool = False,
    n_optuna_trials: int = 50,
    calibrate: bool = False,
    save_path: Optional[str] = None,
) -> Tuple[xgb.XGBClassifier, Dict[str, float]]:
    splits = walk_forward_split(
        df,
        n_splits=4,
        train_months=config.train_size_walk_forward,
        test_months=config.test_size_walk_forward,
    )

    all_metrics = []
    best_model = None
    best_auc = 0

    for fold, (train, test) in enumerate(splits):
        X_train = train[feature_cols]
        y_train = train[label_col]
        X_test = test[feature_cols]
        y_test = test[label_col]

        train_val_split_idx = int(len(X_train) * 0.8)
        X_tr = X_train.iloc[:train_val_split_idx]
        y_tr = y_train.iloc[:train_val_split_idx]
        X_val = X_train.iloc[train_val_split_idx:]
        y_val = y_train.iloc[train_val_split_idx:]

        if run_optuna:
            study = run_hyperparameter_tuning(X_tr, y_tr, X_val, y_val, n_trials=n_optuna_trials)
            model = xgb.XGBClassifier(
                **study.best_params,
                eval_metric="logloss",
                tree_method="hist",
                random_state=42,
                n_jobs=-1,
            )
            scale_pos_weight = (y_tr == 0).sum() / max((y_tr == 1).sum(), 1)
            model.set_params(scale_pos_weight=scale_pos_weight)
            model.fit(X_tr, y_tr, eval_set=[(X_val, y_val)], verbose=0)
        else:
            model = train_xgboost(X_tr, y_tr, X_val, y_val, config)

        if calibrate:
            model = CalibratedClassifierCV(model, cv=3, method="sigmoid")
            model.fit(X_train, y_train)
            X_test_for_pred = X_test
        else:
            X_test_for_pred = X_test

        metrics = evaluate_model(model, X_test_for_pred, y_test)
        metrics["fold"] = fold + 1
        all_metrics.append(metrics)

        if metrics["auc_roc"] > best_auc:
            best_auc = metrics["auc_roc"]
            best_model = model

    summary = {
        f"avg_{k}": np.mean([m[k] for m in all_metrics])
        for k in all_metrics[0] if k != "fold"
    }
    summary["n_folds"] = len(splits)

    print("=== Walk-Forward Validation Results ===")
    for m in all_metrics:
        print(f"  Fold {m['fold']}: AUC={m['auc_roc']:.3f}  Prec={m['precision']:.3f}  "
              f"Prec@20%={m['precision_top20']:.3f}")
    print(f"  Average: AUC={summary['avg_auc_roc']:.3f}  Prec={summary['avg_precision']:.3f}  "
          f"Prec@20%={summary['avg_precision_top20']:.3f}")

    if save_path:
        joblib.dump(best_model, save_path)
        print(f"Model saved to {save_path}")

    return best_model, summary
