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
  invocation_token uuid,  -- pins persistence retries to the original invocation
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
  consumer_id text NOT NULL REFERENCES llm_consumer_state(consumer_id),
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
  invocation_dispatch_deadline_at timestamptz,

  -- Alert delivery
  alert_status text NOT NULL DEFAULT 'NONE'
    CHECK (alert_status IN ('NONE', 'PENDING', 'SENDING', 'SENT', 'RETRYABLE',
                            'DELIVERY_UNKNOWN', 'DELIVERY_FAILED', 'SUPPRESSED_EXPIRED')),
  alert_attempt_token uuid,
  alert_owner text,
  alert_attempt_started_at timestamptz,
  alert_attempt_deadline_at timestamptz,
  alert_payload jsonb,
  alert_destination text,
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


-- All gates use wall-clock database time, not transaction-start now().
CREATE OR REPLACE FUNCTION jev_in_session(p_now timestamptz)
RETURNS boolean LANGUAGE sql IMMUTABLE SECURITY INVOKER
SET search_path = public
AS $$
  SELECT (p_now AT TIME ZONE 'Asia/Kolkata')::time >= time '09:15'
     AND (p_now AT TIME ZONE 'Asia/Kolkata')::time < time '15:30';
$$;

-- Drop the earlier signature so PostgREST has one unambiguous RPC.
DROP FUNCTION IF EXISTS claim_llm_prediction_job(uuid, uuid, integer, uuid, text, text, text, timestamptz, integer);
CREATE OR REPLACE FUNCTION claim_llm_prediction_job(
  p_signal_uuid uuid,
  p_owner_token uuid,
  p_lease_seconds integer,
  p_snapshot_uuid uuid,
  p_context_version text,
  p_question_version text,
  p_model_name text,
  p_event_at timestamptz,
  p_max_age_seconds integer,
  p_consumer_id text DEFAULT 'jev-primary'
) RETURNS SETOF llm_prediction_jobs
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = public
AS $$
DECLARE
  v_now timestamptz;
  v_state llm_consumer_state;
  v_event_at timestamptz;
  v_expires_at timestamptz;
  v_existing llm_prediction_jobs;
BEGIN
  -- Serialize even the absent-row case; a read-before-insert lock alone cannot.
  PERFORM pg_advisory_xact_lock(hashtextextended(p_signal_uuid::text, 0));
  v_now := clock_timestamp();
  SELECT * INTO v_state FROM llm_consumer_state WHERE consumer_id = p_consumer_id;
  IF NOT FOUND OR v_state.max_signal_age_seconds <= 0 OR p_lease_seconds <= 0 THEN RETURN; END IF;
  SELECT timestamp INTO v_event_at FROM ml_collection
    WHERE snapshot_uuid = p_snapshot_uuid AND signal_uuid = p_signal_uuid;
  IF NOT FOUND OR v_event_at IS NULL OR v_event_at IS DISTINCT FROM p_event_at THEN RETURN; END IF;
  -- The persisted policy wins over the caller's environment setting.
  v_expires_at := v_event_at + make_interval(secs => v_state.max_signal_age_seconds);
  IF v_event_at < v_state.live_from OR v_event_at > v_now OR v_now >= v_expires_at
     OR NOT jev_in_session(v_now) THEN RETURN; END IF;

  SELECT * INTO v_existing FROM llm_prediction_jobs WHERE signal_uuid = p_signal_uuid FOR UPDATE;
  IF FOUND THEN
    IF v_existing.status = 'CLAIMED'
       AND v_existing.invocation_started_at IS NULL
       AND v_existing.lease_expires_at <= v_now
       AND v_existing.expires_at > v_now
       AND v_existing.consumer_id = p_consumer_id
       AND v_existing.snapshot_uuid = p_snapshot_uuid
       AND v_existing.event_at = v_event_at
       AND v_existing.context_version = p_context_version
       AND v_existing.question_version = p_question_version
       AND v_existing.model_name = p_model_name THEN
      UPDATE llm_prediction_jobs
      SET owner_token = p_owner_token,
          lease_expires_at = v_now + make_interval(secs => p_lease_seconds),
          updated_at = v_now
      WHERE signal_uuid = p_signal_uuid
      RETURNING * INTO v_existing;
      RETURN NEXT v_existing;
    END IF;
    RETURN;
  END IF;
  RETURN QUERY
  INSERT INTO llm_prediction_jobs (
    signal_uuid, consumer_id, owner_token, lease_expires_at,
    snapshot_uuid, context_version, question_version, model_name,
    event_at, expires_at, status
  ) VALUES (
    p_signal_uuid, p_consumer_id, p_owner_token, v_now + make_interval(secs => p_lease_seconds),
    p_snapshot_uuid, p_context_version, p_question_version, p_model_name,
    v_event_at, v_expires_at, 'CLAIMED'
  )
  RETURNING *;
END;
$$;

-- Restrict the claim function to service_role
REVOKE ALL ON FUNCTION claim_llm_prediction_job FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION claim_llm_prediction_job TO service_role;

CREATE OR REPLACE FUNCTION begin_llm_invocation(
  p_signal_uuid uuid, p_owner_token uuid, p_invocation_token uuid
) RETURNS SETOF llm_prediction_jobs
LANGUAGE plpgsql SECURITY INVOKER SET search_path = public AS $$
DECLARE v_job llm_prediction_jobs; v_now timestamptz;
BEGIN
  SELECT * INTO v_job FROM llm_prediction_jobs WHERE signal_uuid = p_signal_uuid FOR UPDATE;
  IF NOT FOUND THEN RETURN; END IF;
  v_now := clock_timestamp();
  RETURN QUERY UPDATE llm_prediction_jobs j
    SET status = 'INVOKING', invocation_started_at = v_now,
        invocation_token = p_invocation_token,
        invocation_dispatch_deadline_at = least(j.expires_at, j.lease_expires_at,
          ((v_now AT TIME ZONE 'Asia/Kolkata')::date + time '15:30') AT TIME ZONE 'Asia/Kolkata'),
        updated_at = v_now
    FROM llm_consumer_state c
    WHERE j.id = v_job.id AND c.consumer_id = j.consumer_id
      AND j.owner_token = p_owner_token AND j.status = 'CLAIMED'
      AND j.invocation_started_at IS NULL AND j.lease_expires_at > v_now
      AND j.event_at >= c.live_from AND j.event_at <= v_now
      AND j.expires_at > v_now AND jev_in_session(v_now)
    RETURNING j.*;
END;
$$;

CREATE OR REPLACE FUNCTION check_jev_alert_freshness(
  p_signal_uuid uuid, p_job_id bigint, p_attempt_token uuid DEFAULT NULL
) RETURNS jsonb LANGUAGE plpgsql SECURITY INVOKER SET search_path = public AS $$
DECLARE v_job llm_prediction_jobs; v_state llm_consumer_state; v_now timestamptz; v_reason text;
BEGIN
  SELECT * INTO v_job FROM llm_prediction_jobs WHERE id = p_job_id AND signal_uuid = p_signal_uuid;
  IF NOT FOUND THEN RETURN jsonb_build_object('is_fresh', false, 'reason', 'job_not_found'); END IF;
  SELECT * INTO v_state FROM llm_consumer_state WHERE consumer_id = v_job.consumer_id;
  v_now := clock_timestamp();
  IF NOT FOUND THEN v_reason := 'consumer_state_missing';
  ELSIF v_job.event_at < v_state.live_from OR v_job.event_at > v_now THEN v_reason := 'invalid_event_time';
  ELSIF v_now >= v_job.expires_at THEN v_reason := 'expired';
  ELSIF NOT jev_in_session(v_now) THEN v_reason := 'session_closed';
  ELSIF v_job.status <> 'COMPLETED' OR v_job.prediction_id IS NULL THEN v_reason := 'prediction_not_persisted';
  ELSIF p_attempt_token IS NOT NULL THEN
    IF v_job.alert_status <> 'SENDING' OR v_job.alert_attempt_token IS DISTINCT FROM p_attempt_token
       OR v_job.alert_attempt_deadline_at IS NULL OR v_now >= v_job.alert_attempt_deadline_at THEN
      v_reason := 'attempt_not_owned';
    END IF;
  ELSIF v_job.alert_status NOT IN ('PENDING', 'RETRYABLE') THEN v_reason := 'alert_not_pending';
  ELSIF v_job.alert_backoff_until > v_now THEN v_reason := 'backoff';
  END IF;
  RETURN jsonb_build_object('is_fresh', v_reason IS NULL, 'reason', v_reason,
    'remaining_seconds', greatest(0, extract(epoch FROM
      least(v_job.expires_at, CASE WHEN p_attempt_token IS NOT NULL THEN
        coalesce(v_job.alert_attempt_deadline_at, v_job.expires_at) ELSE v_job.expires_at END) - v_now)),
    'alert_status', v_job.alert_status);
END;
$$;

CREATE OR REPLACE FUNCTION begin_jev_alert_attempt(
  p_signal_uuid uuid, p_job_id bigint, p_attempt_token uuid, p_owner text,
  p_payload jsonb, p_destination text
) RETURNS SETOF llm_prediction_jobs
LANGUAGE plpgsql SECURITY INVOKER SET search_path = public AS $$
DECLARE v_job llm_prediction_jobs; v_gate jsonb; v_now timestamptz;
BEGIN
  IF p_attempt_token IS NULL OR p_owner IS NULL OR p_payload IS NULL
     OR nullif(p_destination, '') IS NULL THEN RETURN; END IF;
  SELECT * INTO v_job FROM llm_prediction_jobs WHERE id = p_job_id AND signal_uuid = p_signal_uuid FOR UPDATE;
  IF NOT FOUND THEN RETURN; END IF;
  v_gate := check_jev_alert_freshness(p_signal_uuid, p_job_id);
  IF NOT (v_gate->>'is_fresh')::boolean THEN
    IF v_gate->>'reason' IN ('expired', 'session_closed') THEN
      UPDATE llm_prediction_jobs SET alert_status = 'SUPPRESSED_EXPIRED', updated_at = clock_timestamp()
        WHERE id = v_job.id AND alert_status IN ('PENDING', 'RETRYABLE');
    END IF;
    RETURN;
  END IF;
  v_now := clock_timestamp();
  RETURN QUERY UPDATE llm_prediction_jobs SET alert_status = 'SENDING',
    alert_attempt_token = p_attempt_token, alert_owner = p_owner,
    alert_attempt_started_at = v_now,
    alert_attempt_deadline_at = least(expires_at, v_now + interval '10 seconds'),
    alert_payload = coalesce(alert_payload, p_payload),
    alert_destination = coalesce(alert_destination, p_destination), updated_at = v_now
    WHERE id = v_job.id AND expires_at > v_now AND jev_in_session(v_now)
    RETURNING *;
END;
$$;

CREATE OR REPLACE FUNCTION recover_llm_prediction_jobs(p_consumer_id text)
RETURNS void LANGUAGE plpgsql SECURITY INVOKER SET search_path = public AS $$
DECLARE v_now timestamptz := clock_timestamp();
BEGIN
  UPDATE llm_prediction_jobs SET status = 'EXPIRED', updated_at = v_now
    WHERE consumer_id = p_consumer_id AND status = 'CLAIMED'
      AND invocation_started_at IS NULL AND expires_at <= v_now;
  UPDATE llm_prediction_jobs SET status = 'UNKNOWN', updated_at = v_now
    WHERE consumer_id = p_consumer_id AND status = 'INVOKING' AND lease_expires_at <= v_now;
  -- Never turn an ambiguous in-flight send back into a pending send.
  UPDATE llm_prediction_jobs SET alert_status = 'DELIVERY_UNKNOWN', updated_at = v_now
    WHERE consumer_id = p_consumer_id AND alert_status = 'SENDING'
      AND coalesce(alert_attempt_deadline_at, alert_attempt_started_at + interval '10 seconds') <= v_now;
  UPDATE llm_prediction_jobs SET alert_status = 'SUPPRESSED_EXPIRED', updated_at = v_now
    WHERE consumer_id = p_consumer_id AND alert_status IN ('PENDING', 'RETRYABLE')
      AND (expires_at <= v_now OR NOT jev_in_session(v_now));
END;
$$;

CREATE OR REPLACE FUNCTION complete_llm_prediction_job(
  p_signal_uuid uuid, p_invocation_token uuid, p_prediction jsonb
) RETURNS SETOF llm_predictions
LANGUAGE plpgsql SECURITY INVOKER SET search_path = public AS $$
DECLARE v_job llm_prediction_jobs; v_prediction llm_predictions;
BEGIN
  SELECT * INTO v_job FROM llm_prediction_jobs WHERE signal_uuid = p_signal_uuid FOR UPDATE;
  IF NOT FOUND OR p_invocation_token IS NULL OR v_job.invocation_token IS DISTINCT FROM p_invocation_token
     OR v_job.status NOT IN ('INVOKING', 'UNKNOWN', 'COMPLETED') THEN RETURN; END IF;
  SELECT * INTO v_prediction FROM llm_predictions WHERE signal_uuid = p_signal_uuid;
  IF FOUND THEN
    IF v_prediction.invocation_token IS DISTINCT FROM p_invocation_token THEN RETURN; END IF;
    -- Lost commit acknowledgment: return the same result without resetting delivery.
    RETURN NEXT v_prediction;
    RETURN;
  END IF;
  IF v_job.status = 'COMPLETED' THEN RETURN; END IF;
  v_prediction := jsonb_populate_record(NULL::llm_predictions, p_prediction);
  IF v_prediction.signal_uuid IS DISTINCT FROM p_signal_uuid
     OR v_prediction.snapshot_uuid IS DISTINCT FROM v_job.snapshot_uuid
     OR v_prediction.context_version IS DISTINCT FROM v_job.context_version
     OR v_prediction.question_version IS DISTINCT FROM v_job.question_version THEN RETURN; END IF;
  INSERT INTO llm_predictions (
    signal_uuid, t1_hit_prob, t2_hit_prob, sl_hit_prob, regime, regime_distribution,
    regime_confidence, setup_quality, is_trap_prob, engine_name, context_version,
    question_version, snapshot_uuid, invocation_token, input_state, raw_response,
    latency_snapshot_read_ms, latency_context_build_ms, latency_typesafe_ms, latency_total_ms,
    signal_timestamp, invocation_started_at, response_received_at
  ) VALUES (
    p_signal_uuid, v_prediction.t1_hit_prob, v_prediction.t2_hit_prob, v_prediction.sl_hit_prob,
    v_prediction.regime, v_prediction.regime_distribution, v_prediction.regime_confidence,
    v_prediction.setup_quality, v_prediction.is_trap_prob, v_prediction.engine_name,
    v_job.context_version, v_job.question_version, v_job.snapshot_uuid, p_invocation_token,
    v_prediction.input_state, v_prediction.raw_response, v_prediction.latency_snapshot_read_ms,
    v_prediction.latency_context_build_ms, v_prediction.latency_typesafe_ms, v_prediction.latency_total_ms,
    v_job.event_at, v_job.invocation_started_at, v_prediction.response_received_at
  ) RETURNING * INTO v_prediction;
  UPDATE llm_prediction_jobs SET status = 'COMPLETED', prediction_id = v_prediction.id,
    alert_status = 'PENDING', updated_at = clock_timestamp() WHERE id = v_job.id;
  RETURN NEXT v_prediction;
END;
$$;

REVOKE ALL ON FUNCTION jev_in_session, begin_llm_invocation,
  check_jev_alert_freshness, begin_jev_alert_attempt, recover_llm_prediction_jobs,
  complete_llm_prediction_job FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION jev_in_session, begin_llm_invocation,
  check_jev_alert_freshness, begin_jev_alert_attempt, recover_llm_prediction_jobs,
  complete_llm_prediction_job TO service_role;
