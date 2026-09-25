# Backlog

## High Priority
- **MANM-155**: Refactor ML training: separate self-labeled vs realized outcomes and implement walk-forward validation.
  - [x] Create intent document for MANM-155 (PM) - directives/TASK-155_ml-training-refactor.md
  - [x] Draft ADR-155 for decoupled ML pipelines & walk-forward CV (Architect) - directives/adr/TASK-155_ml-training-refactor.md
  - [ ] Implement decoupled pipelines in ml_signal/train_offline.py (Code Generator)
  - [ ] Implement walk-forward validation and data leakage guards (Code Generator)
  - [ ] Implement production promotion gate (Code Generator)
  - [ ] QA test verification and coverage validation (QA)

- **MANM-154**: Audit and resolve feature-versioning and missing-data inconsistency in `ml_collection`.
  - [x] Create intent document for MANM-154 (PM)
  - [x] Investigate `ml_collection` missing data (Architect)
  - [x] Write ADR on handling schema versioning and missing data (Architect) - Proposed in directives/adr/MANM-154_feature-versioning-missing-data.md
  - [x] Implement `feature_version` metadata and enforce `NULL`/`NaN` in storage (Code Generator)
  - [x] Produce missingness audit report (Code Generator / Architect)
  - [x] Review implementation and ensure tests pass (QA & PR Reviewer)

- **MANM-150**: Fix broken signal-to-trade joins between `ares_signals`, `active_trades`, and analytics.
  - [x] Create ADR (Architect) - Proposed in directives/adr/MANM-150_fix-signal-joins.md
  - [x] Implement data model and insertion path changes (Code Generator)
  - [x] Safe read-only validation query and backfill plan

## Medium / Low Priority
- Monitor live signal accuracy during the next NSE session.
- Observe how the new dynamic confidence levels impact trade suggestion filters.

## Completed
### TASK-153: Implement prediction persistence in ml_predictions table for model auditing
**Priority:** High
**Status:** Completed
**Acceptance Criteria:**
- [x] Prediction persistence happens on every inference event.
- [x] Logging is entirely non-blocking/asynchronous and failure to log never crashes the core trading loop.
- [x] All required fields are correctly populated (timestamp, probability, confidence_tier, model_version, signal_id, trade_id, spot price, source, feature_snapshot).
- [x] Tests prove both persistence success and graceful error handling.
- [x] Documentation is present for data retention.
