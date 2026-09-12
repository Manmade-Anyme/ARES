# TASK-153 Prediction Persistence
**Date:** 2026-09-12
**Status:** ready

## Goal
Implement persistence of machine learning model predictions into the `ml_predictions` Supabase table on every inference event for model auditing and drift monitoring.

## Inputs
- Existing database schema (`schema.sql` or Supabase structure).
- Current ML inference pipeline where predictions are made.
- Environment variables and Supabase configuration.

## Tools / Scripts to Use
- Supabase Python client (`storage.py` or database layer)
- Asynchronous execution tools/libraries.
- Test runner (pytest).

## Expected Output
- Code to write prediction data to `ml_predictions` including fields: `timestamp`, `probability`, `confidence_tier`, `model_version`, `signal_id`, `trade_id`, `spot` price, `source`, `feature_snapshot`.
- Update to `schema.sql` if the `ml_predictions` table needs modification to match required fields.
- Non-blocking asynchronous logging mechanism.
- Unit and integration tests.
- Documentation for data retention policy and privacy considerations (could be in a README or docs/ folder).

## Acceptance Criteria
- Prediction persistence happens on every inference event.
- Logging is entirely non-blocking/asynchronous and failure to log never crashes the core trading loop.
- All required fields are correctly populated.
- Tests prove both persistence success and graceful error handling.
- Documentation is present for data retention.

## Edge Cases
- Supabase connection failure or latency during logging.
- Missing optional data (e.g., `trade_id` if no execution happens).
- JSON serialization errors for `feature_snapshot`.
