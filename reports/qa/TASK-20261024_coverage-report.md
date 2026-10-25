# QA Report Card — TASK-20261024 / PR #148

**Date:** 2026-10-24  
**PR:** [#148](https://github.com/Manmade-Anyme/ARES/pull/148)  
**Branch:** `feature/TASK-20261024-improve-prediction-model`  
**Verdict:** **PASSED & APPROVED** ✅  

---

## 1. CI Pipeline Audit
- **GitHub Actions Status:** All checks passed.
  - `deploy/test (pull_request)`: **SUCCESS** (2m 25s) — Python 3.10 test suite + Postgres service.
  - `Auto Labeler/label (pull_request)`: **SUCCESS** (5s).
- **Linter & Runtime Check:** Clean execution, zero syntax or import errors.

---

## 2. Test Execution & Regression Diff
- **Test Suite Execution:** 177 passed out of 177 tests (0 failed, 0 skipped).
  - `tests/unit/test_ml_prediction_improvements.py`: 8 passed (Direction alignment, schema artifact filtering, shadow evaluation metadata, causal probability fallback, Stage 1 validation feature exclusions, standalone SignalConsumer direction propagation, promotion rollback target preservation).
  - `tests/unit/test_task155_ml_predictor.py`: 1 passed.
  - `tests/unit/test_task155_ml_training_refactor.py`: 48 passed.
  - `tests/unit/test_task183_ml_offline.py`: 55 passed.
  - `tests/unit/test_task194_ml_labels_and_oi_distribution.py`: 19 passed.
  - `tests/unit/test_ml_collector.py`: 24 passed.
  - `tests/unit/test_ml_feature_fidelity.py`: 20 passed.
  - `tests/unit/test_ml_live.py`: 2 passed.

---

## 3. Behavioral Invariant Verification
1. **Direction-Aligned Physics:**
   - Bullish setup: Resistance = Target wall, Support = Stop wall.
   - Bearish setup: Support = Target wall, Resistance = Stop wall.
   - Net delta inverted for bearish setup (`-net_delta`), preserving directional delta magnitude.
   - Both in-process `main.py` and standalone `SignalConsumer` pass signal direction through to `predict_from_raw`.
2. **Schema Artifact Isolation Across Pipelines:**
   - Verified schema flags (`greek__has_net_delta`, `structure__*`) are filtered out of both Stage 1 walk-forward validation and final refit.
3. **Causal Transfer Probabilities:**
   - Unscored early trades in inner cross-validation strictly default to neutral 0.5 prior with forward-fill only, eliminating lookahead from `bfill()`.
4. **Operational Model Protection & Rollback Target:**
   - Verified production artifact `ml_signal/models/v10.joblib` remains the active production model.
   - Candidate model saved strictly as `v11_unpromoted.joblib` with rejection audit log (`reports/ml/v11_rejection_audit.json`).
   - Prior production version is captured before candidate persistence so `rollback_target` accurately points to the prior deployed version if a candidate is promoted.
5. **Offline Zero-Network Safety:**
   - `--cache-dir` flag validated for reproducible sandboxed offline training without Supabase network dependencies.

---

## 4. Assertion Quality & Boundary Coverage
- Assertions in `test_ml_prediction_improvements.py` verify concrete arithmetic calculations (e.g. `dist_to_target_wall == 50.0`, `aligned_net_delta == -0.05`), feature set membership, and rejection gate dictionary keys. No tautological or dummy assertions.

