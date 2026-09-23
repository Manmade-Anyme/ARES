# Pre-Debug Report — TASK-153

**Task:** MANM-153 / TASK-153 (ML Prediction Persistence in `ml_predictions`)  
**Phase:** Code Generator Implementation Complete

## Files Created/Modified
- `config.py` (modified: added `supabase_service_role_key` to `Secrets`)
- `tests/conftest.py` (modified: added dummy `supabase_service_role_key` in session start)
- `models.py` (modified: added `trade_id: Optional[str] = None` to `AresSignal`)
- `position_manager.py` (modified: assigned `signal.trade_id = trade_id` in `add_trade`)
- `storage.py` (modified: added `_sanitize_value`, `sanitize_feature_snapshot`, and `PredictionLogger`)
- `main.py` (modified: initialized `PredictionLogger` and hooked `log_prediction` at trade entry)
- `ml_signal/live.py` (modified: integrated `PredictionLogger` for `source="continuous"`)
- `ml_signal/signal_consumer.py` (modified: mapped `feature_snapshot` and noted in-process writer authority)
- `migrations/2026-09-12-task153-ml-predictions-schema.sql` (new: idempotent schema migration with RLS & indexes)
- `schema.sql` (modified: added standardized `ml_predictions` table and indexes)
- `ml_signal/schema.sql` (modified: standardized `ml_predictions` table and indexes)
- `docs/data_retention_and_privacy.md` (new: comprehensive retention & privacy policy)
- `tests/unit/test_task153_prediction_persistence.py` (new: unit test suite)

## Functions Implemented
- `sanitize_feature_snapshot(features: Dict[str, Any]) -> Dict[str, Any]`
- `PredictionLogger.__init__(supabase_client: Optional[Client] = None, max_workers: int = 2)`
- `PredictionLogger.log_prediction(probability, confidence_tier, model_version, spot, feature_snapshot, signal_id, trade_id, source, timestamp)`
- `PredictionLogger._insert_prediction(record)`
- `PredictionLogger.shutdown(wait: bool = True)`

## Deviations from ADR
None. Implemented strictly per ADR-153 specifications.

## Known Edge Cases Handled
- Bounded thread pool (`max_workers=2`) avoids starving async event loop.
- Non-blocking async fire-and-forget with sync fallback when no loop is active.
- Full exception suppression ensuring database failures never impact the live trading loop.
- Pure sanitization of NumPy types, NaNs, infinities, and datetimes for PostgreSQL JSONB compliance.
- Atomic entity linkage associating `trade_id` and `signal_id` in a single INSERT without race conditions.
