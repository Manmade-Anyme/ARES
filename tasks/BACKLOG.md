# Backlog

## High Priority
- **MANM-150**: Fix broken signal-to-trade joins between `ares_signals`, `active_trades`, and analytics.
  - [x] Create ADR (Architect) - Proposed in directives/adr/MANM-150_fix-signal-joins.md
  - [x] Implement data model and insertion path changes (Code Generator)
  - [x] Safe read-only validation query and backfill plan

## TASK-153: Implement prediction persistence in ml_predictions table for model auditing
**Priority:** High
**Status:** Completed
**Acceptance Criteria:**
- [x] Prediction persistence happens on every inference event.
- [x] Logging is entirely non-blocking/asynchronous and failure to log never crashes the core trading loop.
- [x] All required fields are correctly populated (timestamp, probability, confidence_tier, model_version, signal_id, trade_id, spot price, source, feature_snapshot).
- [x] Tests prove both persistence success and graceful error handling.
- [x] Documentation is present for data retention.
