# MANM-156 Enable SHAP package support, interpretability reporting, and native fallback
**Date:** 2026-09-12
**Status:** ready

## Goal
Ensure SHAP interpretability works robustly in all environments by integrating the `shap` package, with a graceful fallback to XGBoost's native `pred_contribs=True` if the external package fails.

## Inputs
- Requirements files: `requirements.txt`
- ML pipeline modules in `ml_signal/`
- Issue MANM-156 context and acceptance criteria

## Tools / Scripts to Use
- Standard python/pip environment
- XGBoost, SHAP libraries

## Expected Output
- `requirements.txt` includes `shap`.
- Robust calculation of SHAP values with the `shap` package.
- Graceful fallback to XGBoost native SHAP (`pred_contribs=True`) if `shap` fails to import or compute.
- Additivity validation for tree explainers (raw-margin / log-odds).
- Generation of holdout SHAP summary plots, beeswarm plots, and feature importance rankings.
- Audit logic for SHAP stability across rolling training windows (feature drift detection).
- SHAP outputs enriched with metadata: model version, training/testing date windows, sample size, feature schema version.

## Acceptance Criteria
- Ensure `shap` package is specified in dependencies and installed in training/evaluation environments.
- Implement and preserve a graceful fallback to native XGBoost tree SHAP (`pred_contribs=True`) if the external `shap` package fails.
- Validate raw-margin / log-odds additivity for tree explainers.
- Generate holdout SHAP summary plots, beeswarm plots, and feature importance rankings.
- Audit SHAP stability across rolling training windows to detect feature drift.
- Ensure SHAP outputs capture metadata: model version, training/testing date windows, sample size, and feature schema version.

## Edge Cases
- The `shap` package is unavailable in the audit environment.
- Native XGBoost fallback output structure (`pred_contribs`) differs from `shap` output; normalise outputs so downstream consumers see a consistent interface.
- Rolling window data may be sparse, causing drift calculation to fail; handle gracefully.
