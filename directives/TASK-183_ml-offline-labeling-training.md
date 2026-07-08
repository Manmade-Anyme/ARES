# TASK-183 ML Offline Labeling & XGBoost Training Pipeline
**Date:** 2026-07-08
**Status:** ready

## Goal
Make the data ARES already collects in `ml_collection` *usable* — labeled and trainable — and actually run XGBoost on it so we can measure whether the collected features carry predictive signal. Today `ml_collection` grows ~350 rows/trading day but is never labeled and never read by the trainer; XGBoost is dormant (no model artifact, `ml_predictions=0`). This task closes that gap **without touching a single line of the live-running system.**

## Non-Negotiable Constraint
**The live implementation is untouched.** No edits to `collector.py`, `engine.py`, `main.py`, `storage.py`, `position_manager.py`, `config_profiles.py`, detectors, or the deployed fly.io behavior. No schema changes to existing tables. All new code is **offline, read-only, batch** — it reads `ml_collection` and writes only a model artifact + a metrics report to disk.

## Inputs
- `ml_collection` Supabase table (read-only): per-cycle rows with 7 feature-group JSON columns + `raw_candle` (OHLCV) + `raw_atm_oi`.
- Existing pure helpers in `ml_signal/trainer.py` (`train_xgboost`, `evaluate_model`, `get_feature_importance`) — reused, not modified.
- `MLConfig` labeling knobs (`lookforward_candles`, `tp_points`, `sl_points`).

## Tools / Scripts to Use
- `supabase` client (read-only SELECT).
- `xgboost` 3.x, `scikit-learn` (already in `ml_signal/requirements.txt`).
- `pandas` / `numpy`.

## Expected Output
1. **`ml_signal/dataset.py` (NEW)** — read `ml_collection`, flatten the 7 feature-group JSON columns into a numeric feature matrix, and self-label every row via forward-return (see ADR-183 for label definition).
2. **`ml_signal/train_offline.py` (NEW)** — chronological (small-data-safe) train/test split, train XGBoost via the existing `trainer.py` helpers, evaluate, save model to `ml_signal/models/`, write a metrics + feature-importance report to `reports/ml/`. Runnable as `python -m ml_signal.train_offline`.
3. **`tests/unit/test_task183_ml_offline.py` (NEW)** — unit tests for the flattener, the forward-points labeler (incl. day-boundary handling), and the chronological split. Fully mocked; no live DB or network.
4. **Metrics report** proving whether the data adds value (AUC / precision@20% / positive-class rate / feature importance).

## Acceptance Criteria
- [ ] `ml_signal/dataset.py` flattens all 7 feature groups + labels rows; day-bounded forward window (never labels across the overnight gap).
- [ ] `ml_signal/train_offline.py` trains an XGBoost model on the labeled set, prints AUC / precision / positive-rate, saves model + report. Degrades gracefully (warns, still runs) on a small dataset.
- [ ] Reuses `trainer.py` helpers unchanged; adds a chronological split instead of the 6-month walk-forward (which needs 7+ months and would yield 0 folds today).
- [ ] **Zero diffs** to any live-path file. Verified by `git diff --stat` touching only `ml_signal/dataset.py`, `ml_signal/train_offline.py`, `tests/`, `directives/`, `docs/`, `CHANGELOG.md`.
- [ ] `pytest` green (new tests + full existing suite unaffected).
- [ ] A real training run on current `ml_collection` produces a model + report.

## Edge Cases
- **Tiny dataset (~2 weeks):** proceed with a warning; report metrics as provisional. Do not crash.
- **Class imbalance:** carry `scale_pos_weight` (already in `train_xgboost`); report positive-class rate.
- **Inconclusive rows** (neither TP nor SL within lookforward): dropped from training (`label = -1`).
- **Missing/partial feature keys** in a row: fill with 0.0, keep column order stable.
- **Day boundaries:** forward-return window must not span 15:30→next 09:15; label per trading day.

## Definition of Done
- New modules + tests merged; full suite green.
- Sample model + metrics report generated from live `ml_collection` and inspected.
- Docs synced (Build Log, CHANGELOG, Obsidian); ADR-183 recorded.
- Live system confirmed untouched (git diff scoped).
- Follow-up (live inference deployment) explicitly deferred to a future task, pending a model that clears an AUC bar.
