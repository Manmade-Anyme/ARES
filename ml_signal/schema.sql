-- Run this in Supabase SQL Editor to create the ML predictions table.

CREATE TABLE ml_predictions (
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

CREATE INDEX idx_ml_pred_timestamp ON ml_predictions (timestamp desc);
CREATE INDEX idx_ml_pred_signal ON ml_predictions (signal_id);
CREATE INDEX idx_ml_pred_confidence ON ml_predictions (confidence_tier);

-- If you encounter RLS errors (Code 42501), run:
-- ALTER TABLE ml_predictions DISABLE ROW LEVEL SECURITY;
