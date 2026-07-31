-- Run this in Supabase SQL Editor to create the ML tables.

-- ============================================================
-- ml_predictions: Stores XGBoost inference results (live mode)
-- Used AFTER model is trained and deployed.
-- ============================================================

CREATE TABLE IF NOT EXISTS ml_predictions (
  id bigserial primary key,
  timestamp timestamptz not null,

  -- Prediction
  probability numeric not null,
  confidence_tier text,
  model_version text,

  -- Linking (optional — populated in event-triggered mode)
  signal_id text,
  signal_setup_type text,
  trade_id uuid,

  -- Market context
  spot numeric,
  source text default 'continuous',  -- 'continuous' | 'event_triggered'

  -- Feature snapshot for backtesting and retraining
  features jsonb,

  created_at timestamptz default now()
);

CREATE INDEX IF NOT EXISTS idx_ml_pred_timestamp ON ml_predictions (timestamp desc);
CREATE INDEX IF NOT EXISTS idx_ml_pred_signal ON ml_predictions (signal_id);
CREATE INDEX IF NOT EXISTS idx_ml_pred_confidence ON ml_predictions (confidence_tier);


-- ============================================================
-- ml_collection: Training data gathered during live ARES runs.
-- Every engine evaluation cycle logs a row with all 50+ features.
-- Trade outcomes are back-filled when trades close.
-- This is the table used for XGBoost training.
-- ============================================================

CREATE TABLE IF NOT EXISTS ml_collection (
  id bigserial primary key,
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

  created_at timestamptz default now()
);

CREATE INDEX IF NOT EXISTS idx_ml_collection_timestamp ON ml_collection (timestamp desc);
CREATE INDEX IF NOT EXISTS idx_ml_collection_signal ON ml_collection (signal_id);
CREATE INDEX IF NOT EXISTS idx_ml_collection_trade ON ml_collection (trade_id);
CREATE INDEX IF NOT EXISTS idx_ml_collection_outcome ON ml_collection (trade_outcome);

-- If you encounter RLS errors (Code 42501), run:
-- ALTER TABLE ml_predictions DISABLE ROW LEVEL SECURITY;
-- ALTER TABLE ml_collection DISABLE ROW LEVEL SECURITY;
