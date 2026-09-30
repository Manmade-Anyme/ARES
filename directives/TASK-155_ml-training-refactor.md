# TASK-155 Refactor ML training: decouple pipelines and implement walk-forward validation
**Date:** 2026-09-12
**Status:** in-progress

## Goal
Decouple the ML training pipelines for self-labeled market-movement data and realized-trade outcomes, implement robust time-series walk-forward cross-validation, and enforce strict data leakage guards.

## Inputs
- `ml_signal/train_offline.py`
- Current ML data pipeline and model training configuration.
- Problem statement constraints from issue MANM-155.

## Tools / Scripts to Use
- Standard python/ML libraries (e.g. `scikit-learn` for time-series splits, if applicable).
- ARES test suite (pytest).

## Expected Output
- ADR detailing the decoupled architecture and validation strategy.
- Refactored training scripts separating the two pipelines.
- Implemented walk-forward cross-validation with comprehensive reporting (metrics, folds, confidence intervals).
- Strict data guards implemented against data leakage, lookahead bias, and overlapping trade windows.

## Acceptance Criteria
- Two distinct training pipelines exist: one for forward-price snapshots, one for realized trades.
- Walk-forward cross-validation is used instead of a single chronological split.
- Reporting outputs sample counts, class balance, fold-by-fold metrics, and confidence intervals.
- Strict data leakage guards are provably enforced in the pipeline.
- Analysis/Evaluation is provided on whether N=232 is sufficient or if hybrid transfer/pretraining is required.
- Hard gating rule enforced in code: models cannot be promoted based on a single train/test split.

## Edge Cases
- Overlapping trade windows causing target leakage across splits.
- Handling of small sample size for the realized-trade outcome pipeline (transfer learning fallback needed).
- Time-series boundaries misaligned due to non-trading days/hours.
