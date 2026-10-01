-- Migration: 2026-10-01-task210-llm-predictions.sql
-- Description: Create tables for LLM predictions, consumer state, and prediction jobs.

-- Table 1: llm_predictions
CREATE TABLE IF NOT EXISTS llm_predictions (
  id bigserial PRIMARY KEY,
  signal_uuid uuid NOT NULL UNIQUE REFERENCES ares_signals(signal_uuid),

  -- Probability outputs (0-1 range)
  t1_hit_prob numeric NOT NULL CHECK (t1_hit_prob >= 0 AND t1_hit_prob <= 1),
  t2_hit_prob numeric NOT NULL CHECK (t2_hit_prob >= 0 AND t2_hit_prob <= 1),
  sl_hit_prob numeric NOT NULL CHECK (sl_hit_prob >= 0 AND sl_hit_prob <= 1),

  -- Regime classification
  regime text NOT NULL,
  regime_distribution jsonb NOT NULL DEFAULT '{}'::jsonb,
  regime_confidence numeric,

  -- Quality and trap assessment
  setup_quality numeric,  -- 0-10 scale
  is_trap_prob numeric CHECK (is_trap_prob IS NULL OR (is_trap_prob >= 0 AND is_trap_prob <= 1)),

  -- Model metadata
  engine_name text NOT NULL,  -- resolved API model name
  context_version text NOT NULL,
  question_version text NOT NULL,

  -- Input provenance
  snapshot_uuid uuid,  -- ml_collection.snapshot_uuid used as source
  input_state jsonb NOT NULL,  -- exact prepared state sent to Jev
  raw_response jsonb NOT NULL,  -- full API response

  -- Latency tracking (milliseconds)
  latency_snapshot_read_ms numeric,
  latency_context_build_ms numeric,
  latency_typesafe_ms numeric,
  latency_total_ms numeric,

  -- Timestamps
  signal_timestamp timestamptz NOT NULL,  -- event_at from ml_collection.timestamp
  invocation_started_at timestamptz,
  response_received_at timestamptz,
  available_at timestamptz NOT NULL DEFAULT now(),  -- persistence time, NOT backdated
  created_at timestamptz NOT NULL DEFAULT now()
);

-- Indexes for llm_predictions
CREATE INDEX IF NOT EXISTS idx_llm_predictions_signal_uuid ON llm_predictions(signal_uuid);
CREATE INDEX IF NOT EXISTS idx_llm_predictions_engine_name ON llm_predictions(engine_name);
CREATE INDEX IF NOT EXISTS idx_llm_predictions_regime ON llm_predictions(regime);
CREATE INDEX IF NOT EXISTS idx_llm_predictions_signal_timestamp ON llm_predictions(signal_timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_llm_predictions_available_at ON llm_predictions(available_at DESC);

-- Table 2: llm_consumer_state
CREATE TABLE IF NOT EXISTS llm_consumer_state (
  consumer_id text PRIMARY KEY,
  live_from timestamptz NOT NULL DEFAULT now(),
  max_signal_age_seconds integer NOT NULL DEFAULT 60,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

-- Table 3: llm_prediction_jobs
CREATE TABLE IF NOT EXISTS llm_prediction_jobs (
  id bigserial PRIMARY KEY,
  signal_uuid uuid NOT NULL UNIQUE REFERENCES ares_signals(signal_uuid),

  -- Ownership
  owner_token uuid NOT NULL,
  lease_expires_at timestamptz NOT NULL,

  -- Input pinning
  snapshot_uuid uuid,
  context_version text NOT NULL,
  question_version text NOT NULL,
  model_name text NOT NULL,

  -- Event timing
  event_at timestamptz NOT NULL,  -- pinned ml_collection.timestamp
  expires_at timestamptz NOT NULL,  -- event_at + max_signal_age_seconds

  -- Status tracking
  status text NOT NULL DEFAULT 'CLAIMED'
    CHECK (status IN ('CLAIMED', 'INVOKING', 'COMPLETED', 'FAILED', 'UNKNOWN', 'EXPIRED')),
  invocation_started_at timestamptz,
  invocation_token uuid,

  -- Alert delivery
  alert_status text DEFAULT 'NONE'
    CHECK (alert_status IN ('NONE', 'PENDING', 'SENDING', 'SENT', 'RETRYABLE',
                            'DELIVERY_UNKNOWN', 'DELIVERY_FAILED', 'SUPPRESSED_EXPIRED')),
  alert_attempt_token uuid,
  alert_owner text,
  alert_attempt_started_at timestamptz,
  alert_discord_message_id text,
  alert_rejection_reason text,
  alert_backoff_until timestamptz,

  -- Error/audit
  error_message text,
  prediction_id bigint REFERENCES llm_predictions(id),

  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

-- Indexes for llm_prediction_jobs
CREATE INDEX IF NOT EXISTS idx_llm_prediction_jobs_status ON llm_prediction_jobs(status);
CREATE INDEX IF NOT EXISTS idx_llm_prediction_jobs_signal_uuid ON llm_prediction_jobs(signal_uuid);
CREATE INDEX IF NOT EXISTS idx_llm_prediction_jobs_event_at ON llm_prediction_jobs(event_at DESC);

-- RLS, Policies, Grants for llm_predictions
ALTER TABLE llm_predictions ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON llm_predictions FROM PUBLIC, anon, authenticated;
GRANT SELECT, INSERT, UPDATE ON llm_predictions TO service_role;
GRANT USAGE, SELECT ON SEQUENCE llm_predictions_id_seq TO service_role;

CREATE POLICY "service_role_all_llm_predictions" ON llm_predictions
  FOR ALL
  TO service_role
  USING (true)
  WITH CHECK (true);

-- RLS, Policies, Grants for llm_consumer_state
ALTER TABLE llm_consumer_state ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON llm_consumer_state FROM PUBLIC, anon, authenticated;
GRANT SELECT, INSERT, UPDATE ON llm_consumer_state TO service_role;

CREATE POLICY "service_role_all_llm_consumer_state" ON llm_consumer_state
  FOR ALL
  TO service_role
  USING (true)
  WITH CHECK (true);

-- RLS, Policies, Grants for llm_prediction_jobs
ALTER TABLE llm_prediction_jobs ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON llm_prediction_jobs FROM PUBLIC, anon, authenticated;
GRANT SELECT, INSERT, UPDATE ON llm_prediction_jobs TO service_role;
GRANT USAGE, SELECT ON SEQUENCE llm_prediction_jobs_id_seq TO service_role;

CREATE POLICY "service_role_all_llm_prediction_jobs" ON llm_prediction_jobs
  FOR ALL
  TO service_role
  USING (true)
  WITH CHECK (true);


-- Atomic claim function
CREATE OR REPLACE FUNCTION claim_llm_prediction_job(
  p_signal_uuid uuid,
  p_owner_token uuid,
  p_lease_seconds integer,
  p_snapshot_uuid uuid,
  p_context_version text,
  p_question_version text,
  p_model_name text,
  p_event_at timestamptz,
  p_max_age_seconds integer
) RETURNS SETOF llm_prediction_jobs
LANGUAGE plpgsql
SECURITY INVOKER
AS $$
DECLARE
  v_expires_at timestamptz;
  v_lease_expires timestamptz;
  v_existing llm_prediction_jobs;
BEGIN
  v_expires_at := p_event_at + (p_max_age_seconds || ' seconds')::interval;
  v_lease_expires := now() + (p_lease_seconds || ' seconds')::interval;

  -- Check for existing job
  SELECT * INTO v_existing FROM llm_prediction_jobs WHERE signal_uuid = p_signal_uuid FOR UPDATE;

  IF FOUND THEN
    -- Only recover CLAIMED jobs with no invocation and expired lease
    IF v_existing.status = 'CLAIMED'
       AND v_existing.invocation_started_at IS NULL
       AND v_existing.lease_expires_at < now()
       AND v_existing.event_at + (p_max_age_seconds || ' seconds')::interval > now()
    THEN
      UPDATE llm_prediction_jobs
      SET owner_token = p_owner_token,
          lease_expires_at = v_lease_expires,
          updated_at = now()
      WHERE signal_uuid = p_signal_uuid
      RETURNING * INTO v_existing;
      RETURN NEXT v_existing;
      RETURN;
    ELSE
      RAISE EXCEPTION 'Signal % already has an active or terminal job (status=%)', p_signal_uuid, v_existing.status;
    END IF;
  END IF;

  -- Fresh claim
  RETURN QUERY
  INSERT INTO llm_prediction_jobs (
    signal_uuid, owner_token, lease_expires_at,
    snapshot_uuid, context_version, question_version, model_name,
    event_at, expires_at, status
  ) VALUES (
    p_signal_uuid, p_owner_token, v_lease_expires,
    p_snapshot_uuid, p_context_version, p_question_version, p_model_name,
    p_event_at, v_expires_at, 'CLAIMED'
  )
  RETURNING *;
END;
$$;

-- Restrict the claim function to service_role
REVOKE ALL ON FUNCTION claim_llm_prediction_job FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION claim_llm_prediction_job TO service_role;
