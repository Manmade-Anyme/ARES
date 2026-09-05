-- =====================================================================
-- TASK-073 — OI wall entry decoupling and re-test telemetry
-- Run in the Supabase SQL Editor. Safe to run more than once (idempotent).
-- =====================================================================

ALTER TABLE ares_signals
  ADD COLUMN IF NOT EXISTS oi_wall_context jsonb;

COMMENT ON COLUMN ares_signals.oi_wall_context IS
  'Normalized OI wall context and re-test telemetry (TASK-073). NULL for non-OI-wall signals.';

ALTER TABLE ml_collection
  ADD COLUMN IF NOT EXISTS oi_wall_context jsonb;

COMMENT ON COLUMN ml_collection.oi_wall_context IS
  'Normalized OI wall context for every cycle with a tracked wall (TASK-073). NULL when no wall is tracked.';

CREATE INDEX IF NOT EXISTS idx_ml_collection_oi_wall_strike
  ON ml_collection ((oi_wall_context ->> 'wall_strike'));
