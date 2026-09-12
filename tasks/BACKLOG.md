# Backlog

## TASK-153: Implement prediction persistence in ml_predictions table for model auditing
**Priority:** High
**Acceptance Criteria:**
- Prediction persistence happens on every inference event.
- Logging is entirely non-blocking/asynchronous and failure to log never crashes the core trading loop.
- All required fields are correctly populated (timestamp, probability, confidence_tier, model_version, signal_id, trade_id, spot price, source, feature_snapshot).
- Tests prove both persistence success and graceful error handling.
- Documentation is present for data retention.
