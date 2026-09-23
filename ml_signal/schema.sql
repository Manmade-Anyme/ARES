-- Run this in Supabase SQL Editor to create the ML tables.

-- ============================================================
-- ml_predictions: Machine learning model prediction persistence for
-- auditing, calibration (Brier scores), and concept drift monitoring.
-- Persisted on inference by in-process PredictionLogger (TASK-153).
-- ============================================================

CREATE TABLE IF NOT EXISTS ml_predictions (
  id bigserial primary key,
  timestamp timestamptz not null,

  -- Prediction Metrics
  probability numeric not null,
  confidence_tier text not null,       -- 'HIGH' | 'MEDIUM' | 'LOW'
  model_version text not null,         -- e.g. 'v1', 'v2', 'v2.joblib'

  -- Entity Linkage
  signal_id text,                      -- Display signal ID (e.g. '0042') or UUID
  trade_id uuid,                       -- Links to active_trades.id / trade_analytics.id when executed
  
  -- Market Context
  spot numeric not null,
  source text not null default 'event_triggered', -- 'event_triggered' | 'continuous'

  -- Feature Payload
  feature_snapshot jsonb not null,     -- Canonical key-value dictionary of model input features

  created_at timestamptz not null default now()
);

CREATE INDEX IF NOT EXISTS idx_ml_pred_timestamp ON ml_predictions (timestamp desc);
CREATE INDEX IF NOT EXISTS idx_ml_pred_signal ON ml_predictions (signal_id);
CREATE INDEX IF NOT EXISTS idx_ml_pred_trade ON ml_predictions (trade_id);
CREATE INDEX IF NOT EXISTS idx_ml_pred_model_version ON ml_predictions (model_version);
CREATE INDEX IF NOT EXISTS idx_ml_pred_confidence ON ml_predictions (confidence_tier);
CREATE INDEX IF NOT EXISTS idx_ml_pred_source ON ml_predictions (source);


-- ============================================================
-- ml_collection: Training data gathered during live ARES runs.
-- Every engine evaluation cycle logs a row with all 50+ features.
-- Trade outcomes are back-filled when trades close.
-- This is the table used for XGBoost training.
-- ============================================================

CREATE TABLE IF NOT EXISTS ml_collection (
  id bigserial primary key,
  snapshot_uuid uuid UNIQUE,
  timestamp timestamptz not null,
  spot numeric,

  -- Feature snapshots by category
  candle_features jsonb,
  volume_features jsonb,
  iv_features jsonb,
  oi_features jsonb,
  greek_features jsonb,
  structure_features jsonb,
  meta_features jsonb,

  -- Signal reference (filled if a signal fired this cycle)
  signal_generated boolean default false,
  signal_id text,
  signal_uuid uuid,
  signal_display_id text,
  trade_binding_status text,
  signal_setup_type text,
  signal_direction text,
  signal_confidence text,

  -- Detector scores for post-hoc analysis
  detector_scores jsonb,

  -- Trade outcome (filled retroactively when trade closes)
  trade_id uuid,
  trade_outcome text,
  trade_pnl numeric,
  trade_score integer,

  -- Raw market snapshot for repro
  raw_candle jsonb,
  raw_atm_oi jsonb,
  oi_wall_context jsonb, -- TASK-073: per-cycle wall context (NULL if no wall)

  created_at timestamptz default now()
);

CREATE INDEX IF NOT EXISTS idx_ml_collection_timestamp ON ml_collection (timestamp desc);
CREATE INDEX IF NOT EXISTS idx_ml_collection_signal ON ml_collection (signal_id);
CREATE INDEX IF NOT EXISTS idx_ml_collection_trade ON ml_collection (trade_id);
CREATE INDEX IF NOT EXISTS idx_ml_collection_outcome ON ml_collection (trade_outcome);
CREATE INDEX IF NOT EXISTS idx_ml_collection_oi_wall_strike ON ml_collection ((oi_wall_context ->> 'wall_strike'));

-- If you encounter RLS errors (Code 42501), run:
-- ALTER TABLE ml_predictions DISABLE ROW LEVEL SECURITY;
-- ALTER TABLE ml_collection DISABLE ROW LEVEL SECURITY;
