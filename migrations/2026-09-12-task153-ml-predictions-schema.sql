-- =====================================================================
-- TASK-153: Standardize ml_predictions schema for prediction persistence
-- Safe and idempotent across clean installs and existing tables.
-- =====================================================================

CREATE TABLE IF NOT EXISTS ml_predictions (
  id bigserial primary key,
  timestamp timestamptz not null,
  probability numeric not null,
  confidence_tier text not null,
  model_version text not null,
  signal_id text,
  trade_id uuid,
  spot numeric not null,
  source text not null default 'event_triggered',
  feature_snapshot jsonb not null,
  created_at timestamptz not null default now()
);

DO $$
BEGIN
  -- Handle migration from legacy column name 'features' to 'feature_snapshot'
  IF EXISTS (
    SELECT 1 FROM information_schema.columns 
    WHERE table_schema = current_schema()
      AND table_name = 'ml_predictions'
      AND column_name = 'features'
  ) AND NOT EXISTS (
    SELECT 1 FROM information_schema.columns 
    WHERE table_schema = current_schema()
      AND table_name = 'ml_predictions'
      AND column_name = 'feature_snapshot'
  ) THEN
    ALTER TABLE ml_predictions RENAME COLUMN features TO feature_snapshot;
  END IF;

  -- Ensure trade_id column exists
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.columns 
    WHERE table_schema = current_schema()
      AND table_name = 'ml_predictions'
      AND column_name = 'trade_id'
  ) THEN
    ALTER TABLE ml_predictions ADD COLUMN trade_id uuid;
  END IF;

  -- Ensure source column has NOT NULL and default
  IF EXISTS (
    SELECT 1 FROM information_schema.columns 
    WHERE table_schema = current_schema()
      AND table_name = 'ml_predictions'
      AND column_name = 'source'
  ) THEN
    ALTER TABLE ml_predictions ALTER COLUMN source SET DEFAULT 'event_triggered';
  END IF;
END $$;

-- CREATE TABLE IF NOT EXISTS is a no-op for an existing deployment. Add every
-- standardized column explicitly so upgraded and fresh installations converge.
ALTER TABLE ml_predictions
   ADD COLUMN IF NOT EXISTS timestamp timestamptz,
   ADD COLUMN IF NOT EXISTS probability numeric,
   ADD COLUMN IF NOT EXISTS confidence_tier text,
   ADD COLUMN IF NOT EXISTS model_version text,
   ADD COLUMN IF NOT EXISTS signal_id text,
   ADD COLUMN IF NOT EXISTS trade_id uuid,
   ADD COLUMN IF NOT EXISTS spot numeric,
   ADD COLUMN IF NOT EXISTS source text,
   ADD COLUMN IF NOT EXISTS feature_snapshot jsonb,
   ADD COLUMN IF NOT EXISTS created_at timestamptz;

-- Bridge-mode installations stored the canonical UUID separately while
-- signal_id held a legacy display/row identifier. Normalize those rows before
-- signal_id is indexed and treated as the canonical linkage. Keep the legacy
-- column for the separately managed bridge cutover.
DO $$
BEGIN
   IF EXISTS (
      SELECT 1
      FROM information_schema.columns
      WHERE table_schema = current_schema()
        AND table_name = 'ml_predictions'
        AND column_name = 'signal_uuid'
   ) THEN
      EXECUTE 'UPDATE ml_predictions SET signal_id = signal_uuid::text WHERE signal_uuid IS NOT NULL AND signal_id IS DISTINCT FROM signal_uuid::text';
   END IF;
END $$;

UPDATE ml_predictions
SET feature_snapshot = '{}'::jsonb
WHERE feature_snapshot IS NULL;

UPDATE ml_predictions
SET source = 'event_triggered'
WHERE source IS NULL;

UPDATE ml_predictions
SET created_at = now()
WHERE created_at IS NULL;

UPDATE ml_predictions
SET timestamp = created_at
WHERE timestamp IS NULL;

DO $$
BEGIN
   IF EXISTS (
      SELECT 1 FROM ml_predictions
      WHERE timestamp IS NULL
          OR probability IS NULL
          OR confidence_tier IS NULL
          OR model_version IS NULL
          OR spot IS NULL
   ) THEN
      RAISE EXCEPTION
         'TASK-153 cannot apply NOT NULL constraints: legacy ml_predictions rows are missing required audit values';
   END IF;
END $$;

ALTER TABLE ml_predictions
   ALTER COLUMN timestamp SET NOT NULL,
   ALTER COLUMN probability SET NOT NULL,
   ALTER COLUMN confidence_tier SET NOT NULL,
   ALTER COLUMN model_version SET NOT NULL,
   ALTER COLUMN spot SET NOT NULL,
   ALTER COLUMN source SET NOT NULL,
   ALTER COLUMN feature_snapshot SET NOT NULL,
   ALTER COLUMN created_at SET NOT NULL;

ALTER TABLE ml_predictions
   ALTER COLUMN source SET DEFAULT 'event_triggered',
   ALTER COLUMN created_at SET DEFAULT now();

-- Prediction rows are backend audit data. Never use the anon key for this
-- table; only the service role may read or append rows.
ALTER TABLE ml_predictions ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON TABLE ml_predictions FROM PUBLIC, anon, authenticated;
GRANT SELECT, INSERT ON TABLE ml_predictions TO service_role;
GRANT USAGE, SELECT ON SEQUENCE ml_predictions_id_seq TO service_role;

DROP POLICY IF EXISTS ml_predictions_service_read ON ml_predictions;
CREATE POLICY ml_predictions_service_read
   ON ml_predictions FOR SELECT TO service_role USING (true);

DROP POLICY IF EXISTS ml_predictions_service_insert ON ml_predictions;
CREATE POLICY ml_predictions_service_insert
   ON ml_predictions FOR INSERT TO service_role WITH CHECK (true);

-- Idempotent index creation
CREATE INDEX IF NOT EXISTS idx_ml_pred_timestamp ON ml_predictions (timestamp desc);
CREATE INDEX IF NOT EXISTS idx_ml_pred_signal ON ml_predictions (signal_id);
CREATE INDEX IF NOT EXISTS idx_ml_pred_trade ON ml_predictions (trade_id);
CREATE INDEX IF NOT EXISTS idx_ml_pred_model_version ON ml_predictions (model_version);
CREATE INDEX IF NOT EXISTS idx_ml_pred_confidence ON ml_predictions (confidence_tier);
CREATE INDEX IF NOT EXISTS idx_ml_pred_source ON ml_predictions (source);
