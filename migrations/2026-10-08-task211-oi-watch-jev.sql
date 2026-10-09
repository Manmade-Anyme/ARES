-- TASK-211: immutable ready-watch outbox; independent inference/delivery owners.
-- No signal rows or TASK-210 schema changes. Apply before enabling the producer.
CREATE TABLE IF NOT EXISTS public.oi_watch_predictions (
  event_id uuid PRIMARY KEY,
  producer_run_id uuid NOT NULL,
  observed_at timestamptz NOT NULL,
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  prediction_deadline timestamptz NOT NULL,
  expires_at timestamptz NOT NULL,
  input_state jsonb NOT NULL CHECK (jsonb_typeof(input_state)='object'),
  base_payload jsonb NOT NULL CHECK (jsonb_typeof(base_payload)='object'),
  restart_cancellation_payload jsonb NOT NULL CHECK (jsonb_typeof(restart_cancellation_payload)='object'),
  lifecycle text NOT NULL DEFAULT 'ACTIVE' CHECK (lifecycle IN ('ACTIVE','CONSUMED','RESOLVED','CANCELED')),
  prediction_status text NOT NULL DEFAULT 'PENDING' CHECK (prediction_status IN ('PENDING','INVOKING','COMPLETED','FAILED')),
  invocation_token uuid,
  prediction jsonb,
  prediction_completed_at timestamptz,
  error text,
  delivery_status text NOT NULL DEFAULT 'PENDING' CHECK (delivery_status IN ('PENDING','SENDING','SENT','UNKNOWN','SKIPPED')),
  delivery_token uuid,
  delivery_payload jsonb,
  delivery_started_at timestamptz,
  delivered_at timestamptz,
  cancellation_payload jsonb,
  cancellation_status text NOT NULL DEFAULT 'PENDING' CHECK (cancellation_status IN ('PENDING','SENDING','SENT','UNKNOWN')),
  cancellation_token uuid,
  cancellation_started_at timestamptz,
  next_attempt_at timestamptz,
  cancellation_next_attempt_at timestamptz,
  CHECK (observed_at <= prediction_deadline AND prediction_deadline <= expires_at)
);
CREATE INDEX IF NOT EXISTS oi_watch_pending_idx ON public.oi_watch_predictions(observed_at)
  WHERE delivery_status IN ('PENDING','SENDING') OR cancellation_status IN ('PENDING','SENDING');
ALTER TABLE public.oi_watch_predictions ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.oi_watch_predictions FROM PUBLIC, anon, authenticated;
GRANT SELECT, INSERT, UPDATE ON public.oi_watch_predictions TO service_role;

CREATE OR REPLACE FUNCTION public.oi_watch_in_session(p_now timestamptz)
RETURNS boolean LANGUAGE sql STABLE SECURITY INVOKER SET search_path=public,pg_temp AS $$
  SELECT (p_now AT TIME ZONE 'Asia/Kolkata')::time >= time '09:15'
     AND (p_now AT TIME ZONE 'Asia/Kolkata')::time < time '15:30'
$$;
CREATE OR REPLACE FUNCTION public.oi_watch_session_end(p_now timestamptz)
RETURNS timestamptz LANGUAGE sql STABLE SECURITY INVOKER SET search_path=public,pg_temp AS $$
  SELECT ((p_now AT TIME ZONE 'Asia/Kolkata')::date + time '15:30') AT TIME ZONE 'Asia/Kolkata'
$$;

CREATE OR REPLACE FUNCTION public.protect_oi_watch_snapshot()
RETURNS trigger LANGUAGE plpgsql SECURITY INVOKER SET search_path=public,pg_temp AS $$
BEGIN
  IF (NEW.event_id,NEW.producer_run_id,NEW.observed_at,NEW.created_at,NEW.prediction_deadline,
      NEW.expires_at,NEW.input_state,NEW.base_payload,NEW.restart_cancellation_payload)
     IS DISTINCT FROM
     (OLD.event_id,OLD.producer_run_id,OLD.observed_at,OLD.created_at,OLD.prediction_deadline,
      OLD.expires_at,OLD.input_state,OLD.base_payload,OLD.restart_cancellation_payload) THEN
    RAISE EXCEPTION 'OI watch snapshot is immutable';
  END IF;
  RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS oi_watch_snapshot_immutable ON public.oi_watch_predictions;
CREATE TRIGGER oi_watch_snapshot_immutable BEFORE UPDATE ON public.oi_watch_predictions
  FOR EACH ROW EXECUTE FUNCTION public.protect_oi_watch_snapshot();

CREATE OR REPLACE FUNCTION public.enqueue_oi_watch(
  p_event_id uuid,p_producer_run_id uuid,p_observed_at timestamptz,p_input_state jsonb,
  p_base_payload jsonb,p_restart_cancellation_payload jsonb,p_wait_seconds double precision DEFAULT 8,
  p_max_age_seconds double precision DEFAULT 60)
RETURNS SETOF public.oi_watch_predictions LANGUAGE plpgsql SECURITY INVOKER SET search_path=public,pg_temp AS $$
DECLARE v_now timestamptz:=clock_timestamp(); v_age double precision; v_wait double precision;
BEGIN
  IF p_wait_seconds IS NULL OR p_max_age_seconds IS NULL OR p_wait_seconds < 0
     OR p_max_age_seconds <= 0 OR p_wait_seconds >= 'Infinity'::float8
     OR p_max_age_seconds >= 'Infinity'::float8 THEN
    RAISE EXCEPTION 'Watch deadlines must be finite and positive';
  END IF;
  v_age:=least(p_max_age_seconds,60); v_wait:=least(p_wait_seconds,v_age);
  -- Retry returns the original row, including a now-terminal record, unchanged.
  IF EXISTS(SELECT 1 FROM oi_watch_predictions WHERE event_id=p_event_id) THEN
    RETURN QUERY SELECT * FROM oi_watch_predictions WHERE event_id=p_event_id; RETURN;
  END IF;
  IF p_observed_at IS NULL OR p_observed_at > v_now OR NOT oi_watch_in_session(v_now)
     OR (p_observed_at AT TIME ZONE 'Asia/Kolkata')::date <> (v_now AT TIME ZONE 'Asia/Kolkata')::date
     OR p_observed_at + make_interval(secs=>v_age) <= v_now THEN RETURN; END IF;
  INSERT INTO oi_watch_predictions(event_id,producer_run_id,observed_at,prediction_deadline,expires_at,
    input_state,base_payload,restart_cancellation_payload)
  VALUES(p_event_id,p_producer_run_id,p_observed_at,
    least(p_observed_at+make_interval(secs=>v_wait),oi_watch_session_end(v_now)),
    least(p_observed_at+make_interval(secs=>v_age),oi_watch_session_end(v_now)),
    p_input_state,p_base_payload,p_restart_cancellation_payload)
  ON CONFLICT(event_id) DO NOTHING;
  RETURN QUERY SELECT * FROM oi_watch_predictions WHERE event_id=p_event_id;
END $$;

CREATE OR REPLACE FUNCTION public.transition_oi_watch(p_event_id uuid,p_action text,p_cancellation_payload jsonb DEFAULT NULL)
RETURNS SETOF public.oi_watch_predictions LANGUAGE plpgsql SECURITY INVOKER SET search_path=public,pg_temp AS $$
DECLARE v public.oi_watch_predictions;
BEGIN
  IF p_action IS NULL OR p_action NOT IN ('CANCEL','CONSUME','RESOLVE') THEN RAISE EXCEPTION 'Invalid watch action'; END IF;
  SELECT * INTO v FROM oi_watch_predictions WHERE event_id=p_event_id FOR UPDATE;
  IF NOT FOUND THEN RETURN; END IF;
  IF v.lifecycle <> 'RESOLVED' THEN
    UPDATE oi_watch_predictions SET
      lifecycle=CASE WHEN p_action='RESOLVE' THEN 'RESOLVED' WHEN p_action='CANCEL' THEN 'CANCELED'
        WHEN lifecycle='ACTIVE' THEN 'CONSUMED' ELSE lifecycle END,
      cancellation_payload=CASE WHEN p_action='RESOLVE' THEN
          CASE WHEN delivery_status IN ('SENDING','UNKNOWN') THEN p_cancellation_payload ELSE NULL END
        WHEN p_action='CANCEL'
        THEN coalesce(cancellation_payload,p_cancellation_payload,restart_cancellation_payload)
        ELSE cancellation_payload END,
      delivery_status=CASE WHEN delivery_status='PENDING' THEN 'SKIPPED' ELSE delivery_status END
    WHERE event_id=p_event_id;
  END IF;
  RETURN QUERY SELECT * FROM oi_watch_predictions WHERE event_id=p_event_id;
END $$;

CREATE OR REPLACE FUNCTION public.restart_oi_watches(p_producer_run_id uuid)
RETURNS integer LANGUAGE plpgsql SECURITY INVOKER SET search_path=public,pg_temp AS $$
DECLARE v_count integer;
BEGIN
  UPDATE oi_watch_predictions SET lifecycle='CANCELED',
    cancellation_payload=coalesce(cancellation_payload,restart_cancellation_payload),
    delivery_status=CASE WHEN delivery_status='PENDING' THEN 'SKIPPED' ELSE delivery_status END
  WHERE producer_run_id<>p_producer_run_id AND lifecycle IN ('ACTIVE','CONSUMED');
  GET DIAGNOSTICS v_count=ROW_COUNT; RETURN v_count;
END $$;

CREATE OR REPLACE FUNCTION public.poll_oi_watch_inference()
RETURNS SETOF public.oi_watch_predictions LANGUAGE sql VOLATILE SECURITY INVOKER SET search_path=public,pg_temp AS $$
  SELECT * FROM oi_watch_predictions WHERE lifecycle='ACTIVE' AND prediction_status='PENDING'
    AND observed_at<=clock_timestamp() AND prediction_deadline>clock_timestamp()
    AND expires_at>clock_timestamp() AND oi_watch_in_session(clock_timestamp())
  ORDER BY observed_at LIMIT 10
$$;
CREATE OR REPLACE FUNCTION public.claim_oi_watch_inference(p_event_id uuid,p_invocation_token uuid)
RETURNS SETOF jsonb LANGUAGE plpgsql SECURITY INVOKER SET search_path=public,pg_temp AS $$
DECLARE v_now timestamptz:=clock_timestamp(); v public.oi_watch_predictions;
BEGIN
  IF p_invocation_token IS NULL THEN RETURN; END IF;
  UPDATE oi_watch_predictions SET prediction_status='INVOKING',invocation_token=p_invocation_token
  WHERE event_id=p_event_id AND lifecycle='ACTIVE' AND prediction_status='PENDING'
    AND prediction_deadline>v_now AND expires_at>v_now AND observed_at<=v_now AND oi_watch_in_session(v_now)
  RETURNING * INTO v;
  IF FOUND THEN RETURN NEXT to_jsonb(v)||jsonb_build_object('remaining_seconds',
    extract(epoch FROM least(v.prediction_deadline,v.expires_at,oi_watch_session_end(v_now))-v_now)); END IF;
END $$;
CREATE OR REPLACE FUNCTION public.complete_oi_watch_inference(
  p_event_id uuid,p_invocation_token uuid,p_prediction jsonb DEFAULT NULL,p_error text DEFAULT NULL)
RETURNS SETOF public.oi_watch_predictions LANGUAGE plpgsql SECURITY INVOKER SET search_path=public,pg_temp AS $$
BEGIN
  UPDATE oi_watch_predictions SET prediction_status=CASE WHEN p_prediction IS NULL THEN 'FAILED' ELSE 'COMPLETED' END,
    prediction=p_prediction,error=left(p_error,500),prediction_completed_at=clock_timestamp()
  WHERE event_id=p_event_id AND invocation_token=p_invocation_token AND prediction_status='INVOKING';
  RETURN QUERY SELECT * FROM oi_watch_predictions WHERE event_id=p_event_id AND invocation_token=p_invocation_token;
END $$;

CREATE OR REPLACE FUNCTION public.poll_oi_watch_delivery()
RETURNS SETOF jsonb LANGUAGE plpgsql SECURITY INVOKER SET search_path=public,pg_temp AS $$
DECLARE v_now timestamptz:=clock_timestamp();
BEGIN
  -- An abandoned transport is ambiguous, never authority for another POST.
  UPDATE oi_watch_predictions SET delivery_status='UNKNOWN'
    WHERE delivery_status='SENDING' AND delivery_started_at<=v_now-interval '10 seconds';
  UPDATE oi_watch_predictions SET cancellation_status='UNKNOWN'
    WHERE cancellation_status='SENDING' AND cancellation_started_at<=v_now-interval '10 seconds';
  UPDATE oi_watch_predictions SET delivery_status='SKIPPED'
    WHERE delivery_status='PENDING' AND (lifecycle<>'ACTIVE' OR expires_at<=v_now OR NOT oi_watch_in_session(v_now));
  RETURN QUERY SELECT to_jsonb(w)||jsonb_build_object('is_cancellation',true)
    FROM oi_watch_predictions w WHERE lifecycle='CANCELED' AND delivery_status='SENT'
      AND cancellation_payload IS NOT NULL AND cancellation_status='PENDING'
      AND coalesce(cancellation_next_attempt_at,v_now)<=v_now ORDER BY observed_at LIMIT 10;
  RETURN QUERY SELECT to_jsonb(w)||jsonb_build_object('is_cancellation',false,
    'prediction_available_within_deadline',prediction_completed_at<=prediction_deadline)
    FROM oi_watch_predictions w WHERE lifecycle='ACTIVE' AND delivery_status='PENDING'
      AND expires_at>v_now AND oi_watch_in_session(v_now) AND coalesce(next_attempt_at,v_now)<=v_now
      AND (prediction_status IN ('COMPLETED','FAILED') OR prediction_deadline<=v_now)
    ORDER BY observed_at LIMIT 10;
END $$;
CREATE OR REPLACE FUNCTION public.begin_oi_watch_delivery(
  p_event_id uuid,p_delivery_token uuid,p_payload jsonb,p_is_cancellation boolean DEFAULT false)
RETURNS SETOF jsonb LANGUAGE plpgsql SECURITY INVOKER SET search_path=public,pg_temp AS $$
DECLARE v_now timestamptz:=clock_timestamp(); v public.oi_watch_predictions; v_budget double precision;
BEGIN
  IF p_delivery_token IS NULL OR p_payload IS NULL OR jsonb_typeof(p_payload)<>'object' THEN RETURN; END IF;
  SELECT * INTO v FROM oi_watch_predictions WHERE event_id=p_event_id FOR UPDATE;
  IF NOT FOUND THEN RETURN; END IF;
  IF p_is_cancellation THEN
    IF v.lifecycle<>'CANCELED' OR v.delivery_status<>'SENT' OR v.cancellation_payload IS NULL
      OR v.cancellation_status<>'PENDING' OR coalesce(v.cancellation_next_attempt_at,v_now)>v_now THEN RETURN; END IF;
    UPDATE oi_watch_predictions SET cancellation_status='SENDING',cancellation_token=p_delivery_token,
      cancellation_started_at=v_now,cancellation_payload=p_payload
      WHERE event_id=p_event_id RETURNING * INTO v;
    v_budget:=5;
  ELSE
    IF v.lifecycle<>'ACTIVE' OR v.delivery_status<>'PENDING' OR v.expires_at<=v_now
      OR v.observed_at>v_now OR NOT oi_watch_in_session(v_now) OR coalesce(v.next_attempt_at,v_now)>v_now
      OR (v.prediction_status NOT IN ('COMPLETED','FAILED') AND v.prediction_deadline>v_now) THEN RETURN; END IF;
    UPDATE oi_watch_predictions SET delivery_status='SENDING',delivery_token=p_delivery_token,
      delivery_started_at=v_now,delivery_payload=coalesce(delivery_payload,p_payload)
      WHERE event_id=p_event_id RETURNING * INTO v;
    v_budget:=least(5.0,extract(epoch FROM least(v.expires_at,oi_watch_session_end(v_now))-v_now));
  END IF;
  RETURN NEXT to_jsonb(v)||jsonb_build_object('remaining_seconds',v_budget,
    'prediction_available_within_deadline',v.prediction_completed_at<=v.prediction_deadline);
END $$;
CREATE OR REPLACE FUNCTION public.finish_oi_watch_delivery(
  p_event_id uuid,p_delivery_token uuid,p_status text,p_is_cancellation boolean DEFAULT false,
  p_retry_after_seconds double precision DEFAULT 0)
RETURNS SETOF public.oi_watch_predictions LANGUAGE plpgsql SECURITY INVOKER SET search_path=public,pg_temp AS $$
DECLARE v_now timestamptz:=clock_timestamp(); v public.oi_watch_predictions;
BEGIN
  IF p_status IS NULL OR p_status NOT IN ('SENT','UNKNOWN','RETRY') OR p_retry_after_seconds IS NULL
     OR p_retry_after_seconds < 0 OR p_retry_after_seconds >= 'Infinity'::float8 THEN
    RAISE EXCEPTION 'Invalid watch delivery outcome'; END IF;
  SELECT * INTO v FROM oi_watch_predictions WHERE event_id=p_event_id FOR UPDATE;
  IF NOT FOUND THEN RETURN; END IF;
  IF p_is_cancellation THEN
    IF v.cancellation_token<>p_delivery_token OR v.cancellation_token IS NULL THEN RETURN; END IF;
    IF v.cancellation_status='SENDING' OR (v.cancellation_status='UNKNOWN' AND p_status='SENT') THEN
      UPDATE oi_watch_predictions SET cancellation_status=CASE WHEN p_status='RETRY' THEN 'PENDING' ELSE p_status END,
        cancellation_next_attempt_at=CASE WHEN p_status='RETRY' THEN v_now+make_interval(secs=>p_retry_after_seconds) ELSE NULL END
      WHERE event_id=p_event_id;
    END IF;
  ELSE
    IF v.delivery_token<>p_delivery_token OR v.delivery_token IS NULL THEN RETURN; END IF;
    IF v.delivery_status='SENDING' OR (v.delivery_status='UNKNOWN' AND p_status='SENT') THEN
      UPDATE oi_watch_predictions SET delivery_status=CASE WHEN p_status='RETRY' THEN
          CASE WHEN lifecycle='ACTIVE' AND expires_at>v_now AND oi_watch_in_session(v_now) THEN 'PENDING' ELSE 'SKIPPED' END
          ELSE p_status END,
        next_attempt_at=CASE WHEN p_status='RETRY' THEN v_now+make_interval(secs=>p_retry_after_seconds) ELSE NULL END,
        delivered_at=CASE WHEN p_status='SENT' THEN v_now ELSE delivered_at END,
        lifecycle=CASE WHEN p_status='SENT' AND lifecycle='RESOLVED' AND cancellation_payload IS NOT NULL
          THEN 'CANCELED' ELSE lifecycle END
      WHERE event_id=p_event_id;
    END IF;
  END IF;
  RETURN QUERY SELECT * FROM oi_watch_predictions WHERE event_id=p_event_id;
END $$;

-- Functions are invoker-only. Public/default EXECUTE must not expose the outbox.
REVOKE ALL ON FUNCTION public.oi_watch_in_session(timestamptz),public.oi_watch_session_end(timestamptz),
  public.protect_oi_watch_snapshot(),public.enqueue_oi_watch(uuid,uuid,timestamptz,jsonb,jsonb,jsonb,double precision,double precision),
  public.transition_oi_watch(uuid,text,jsonb),public.restart_oi_watches(uuid),public.poll_oi_watch_inference(),
  public.claim_oi_watch_inference(uuid,uuid),public.complete_oi_watch_inference(uuid,uuid,jsonb,text),
  public.poll_oi_watch_delivery(),public.begin_oi_watch_delivery(uuid,uuid,jsonb,boolean),
  public.finish_oi_watch_delivery(uuid,uuid,text,boolean,double precision) FROM PUBLIC,anon,authenticated;
GRANT EXECUTE ON FUNCTION public.oi_watch_in_session(timestamptz),public.oi_watch_session_end(timestamptz),
  public.protect_oi_watch_snapshot(),public.enqueue_oi_watch(uuid,uuid,timestamptz,jsonb,jsonb,jsonb,double precision,double precision),
  public.transition_oi_watch(uuid,text,jsonb),public.restart_oi_watches(uuid),public.poll_oi_watch_inference(),
  public.claim_oi_watch_inference(uuid,uuid),public.complete_oi_watch_inference(uuid,uuid,jsonb,text),
  public.poll_oi_watch_delivery(),public.begin_oi_watch_delivery(uuid,uuid,jsonb,boolean),
  public.finish_oi_watch_delivery(uuid,uuid,text,boolean,double precision) TO service_role;
