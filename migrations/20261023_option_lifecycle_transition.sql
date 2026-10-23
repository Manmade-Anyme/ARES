-- TASK-20261023: backend-only, revisioned option lifecycle and durable alerts.
-- Deployment/activation remain separate approvals; this migration does not
-- convert carried trades or establish chart parity.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_catalog.pg_constraint
               WHERE conname = 'chk_active_trades_state'
                 AND conrelid = 'public.active_trades'::regclass) THEN
        ALTER TABLE public.active_trades DROP CONSTRAINT chk_active_trades_state;
        ALTER TABLE public.active_trades ADD CONSTRAINT chk_active_trades_state
            CHECK (state IN ('OPEN', 'T1_HIT', 'T2_REACHED', 'CLOSED', 'STOPPED_OUT'));
    END IF;
END $$;

COMMENT ON COLUMN public.trade_analytics.score IS
'Milestone score: NULL before T1, 1 at T1, 2 at T2. option_atr_v1 runners remain OPEN until remaining allocation is zero; terminal score retains highest milestone.';

CREATE OR REPLACE FUNCTION public.apply_option_lifecycle_transition(
    p_trade_id uuid, p_expected_revision integer, p_event_id text, p_transition jsonb
) RETURNS jsonb
LANGUAGE plpgsql SECURITY INVOKER SET search_path = ''
AS $$
DECLARE
    v_active public.active_trades%ROWTYPE;
    v_analytics public.trade_analytics%ROWTYPE;
    v_context jsonb;
    v_old jsonb;
    v_option jsonb;
    v_events jsonb;
    v_rev integer;
    v_highest integer;
    v_remaining numeric;
    v_phase text;
    v_state text;
    v_score integer;
    v_result text;
    v_now timestamptz := pg_catalog.clock_timestamp();
BEGIN
    IF p_expected_revision IS NULL OR p_expected_revision < 1
       OR p_event_id IS NULL OR pg_catalog.btrim(p_event_id) = ''
       OR pg_catalog.jsonb_typeof(p_transition) IS DISTINCT FROM 'object' THEN
        RAISE EXCEPTION 'A positive expected revision, stable event id and object transition are required';
    END IF;

    -- Same lock order for every lifecycle writer. An absent row is an error,
    -- never permission to advance only the other half of the ledger.
    SELECT * INTO STRICT v_active FROM public.active_trades WHERE id = p_trade_id FOR UPDATE;
    SELECT * INTO STRICT v_analytics FROM public.trade_analytics WHERE id = p_trade_id FOR UPDATE;
    v_context := COALESCE(v_analytics.market_context, '{}'::jsonb);
    v_old := COALESCE(v_context->'option_trailing', '{}'::jsonb);
    IF v_old->>'policy_version' IS DISTINCT FROM 'option_atr_v1' THEN
        RAISE EXCEPTION 'Trade % is not enrolled in option_atr_v1', p_trade_id;
    END IF;
    v_rev := COALESCE((v_old->>'revision')::integer, 1);
    v_events := COALESCE(v_old->'alert_events', '{}'::jsonb);
    -- A replay can carry the old revision after a successful commit whose
    -- response was lost. Its immutable event identity makes that safe.
    IF v_events ? p_event_id OR v_old->>'last_non_alert_event_id' = p_event_id THEN
        RETURN pg_catalog.jsonb_build_object('success', true, 'idempotent_skip', true,
            'trade_id', p_trade_id, 'revision', v_rev, 'option_trailing', v_old,
            'active_state', v_active.state, 'result_state', v_analytics.result_state,
            'score', v_analytics.score);
    END IF;
    IF v_rev <> p_expected_revision THEN
        RAISE EXCEPTION 'Revision conflict: expected %, found %', p_expected_revision, v_rev
            USING ERRCODE = '40001';
    END IF;
    IF v_active.state IN ('CLOSED', 'STOPPED_OUT')
       OR v_analytics.result_state IS DISTINCT FROM 'OPEN'
       OR v_old->>'phase' = 'CLOSED' THEN
        RAISE EXCEPTION 'Terminal trade cannot receive a new lifecycle transition';
    END IF;
    IF pg_catalog.jsonb_typeof(p_transition->'option_trailing') IS DISTINCT FROM 'object' THEN
        RAISE EXCEPTION 'Canonical option state is required';
    END IF;
    v_option := v_old || (p_transition->'option_trailing');
    -- Client snapshots must not erase acknowledgements committed meanwhile.
    v_option := pg_catalog.jsonb_set(v_option, '{alert_events}', v_events);
    IF v_option->>'policy_version' IS DISTINCT FROM 'option_atr_v1'
       OR v_option->'contract' IS DISTINCT FROM v_old->'contract'
       OR v_option->'original_option_sl' IS DISTINCT FROM v_old->'original_option_sl'
       OR v_option->'original_option_entry' IS DISTINCT FROM v_old->'original_option_entry' THEN
        RAISE EXCEPTION 'Pinned contract and original premium bracket are immutable';
    END IF;
    v_highest := (v_option->>'highest_milestone')::integer;
    v_remaining := (v_option->>'remaining_fraction')::numeric;
    v_phase := v_option->>'phase';
    v_state := p_transition->>'active_state';
    IF v_phase IS NULL OR v_state IS NULL OR v_highest IS NULL OR v_highest NOT BETWEEN 0 AND 2
       OR v_highest < COALESCE((v_old->>'highest_milestone')::integer, 0)
       OR v_remaining IS NULL OR v_remaining NOT BETWEEN 0 AND 1
       OR v_remaining > COALESCE((v_old->>'remaining_fraction')::numeric, 1)
       OR NOT ((v_phase = 'PRE_T1' AND v_highest = 0 AND v_remaining = 1 AND v_state = 'OPEN')
           OR (v_phase = 'POST_T1' AND v_highest = 1 AND v_remaining = .5 AND v_state = 'T1_HIT')
           OR (v_phase = 'POST_T2' AND v_highest = 2 AND v_remaining = .2 AND v_state = 'T2_REACHED')
           OR (v_phase = 'CLOSED' AND v_remaining = 0 AND v_state = 'CLOSED')) THEN
        RAISE EXCEPTION 'Invalid lifecycle phase, milestone or allocation';
    END IF;
    IF (v_old->>'locked_option_sl') IS NOT NULL AND
       ((v_option->>'locked_option_sl') IS NULL OR
        (v_option->>'locked_option_sl')::numeric < (v_old->>'locked_option_sl')::numeric) THEN
        RAISE EXCEPTION 'Locked premium stop cannot decrease';
    END IF;
    IF (v_option->>'locked_option_sl') IS NOT NULL AND
       (v_option->>'locked_option_sl')::numeric < (v_old->>'original_option_sl')::numeric THEN
        RAISE EXCEPTION 'Locked premium stop cannot be below the original floor';
    END IF;
    v_score := CASE WHEN v_highest = 0 AND v_remaining > 0 THEN NULL ELSE v_highest END;
    IF (p_transition ? 'score') AND (p_transition->>'score')::integer IS DISTINCT FROM v_score THEN
        RAISE EXCEPTION 'Score must equal canonical highest milestone';
    END IF;
    IF v_analytics.score IS NOT NULL AND (v_score IS NULL OR v_score < v_analytics.score) THEN
        RAISE EXCEPTION 'Recorded milestone score cannot decrease';
    END IF;
    v_result := CASE WHEN v_remaining > 0 THEN 'OPEN' ELSE p_transition->>'exit_reason' END;
    IF v_remaining = 0 AND (v_result IS NULL OR v_result NOT IN
        ('SL_HIT', 'STOPPED_OUT', 'STOPPED_OUT_AT_BE', 'ATR_SL_HIT', 'OPTION_SL_FLOOR_HIT')) THEN
        RAISE EXCEPTION 'A supported terminal exit reason is required';
    END IF;
    IF v_remaining = 0 AND (
        (v_result IN ('SL_HIT', 'STOPPED_OUT') AND v_highest <> 0)
        OR (v_result = 'STOPPED_OUT_AT_BE' AND v_highest <> 1)
        OR (v_result = 'ATR_SL_HIT' AND v_highest < 1)
        OR (v_result = 'OPTION_SL_FLOOR_HIT' AND v_highest <> 2)) THEN
        RAISE EXCEPTION 'Exit reason is inconsistent with highest milestone';
    END IF;
    IF v_remaining = 0 AND NULLIF(p_transition->>'exit_timestamp', '') IS NULL THEN
        RAISE EXCEPTION 'A source event exit timestamp is required';
    END IF;

    -- Only deliverable snapshots belong in the outbox. Silent source-watermark
    -- writes still need bounded retry identity after a lost commit response.
    -- Keep that server-owned marker separate from pending/delivered alerts.
    v_option := pg_catalog.jsonb_set(v_option, '{last_non_alert_event_id}',
        COALESCE(v_old->'last_non_alert_event_id', 'null'::jsonb));
    IF pg_catalog.jsonb_typeof(p_transition->'alert_snapshot') = 'object'
       AND p_transition->'alert_snapshot' <> '{}'::jsonb THEN
        v_events := pg_catalog.jsonb_set(v_events, ARRAY[p_event_id], pg_catalog.jsonb_build_object(
            'status', 'PENDING', 'recorded_at', v_now, 'transition', p_transition->>'update_type',
            'snapshot', p_transition->'alert_snapshot'));
    ELSE
        v_option := pg_catalog.jsonb_set(v_option, '{last_non_alert_event_id}', pg_catalog.to_jsonb(p_event_id));
    END IF;
    v_option := pg_catalog.jsonb_set(v_option, '{revision}', pg_catalog.to_jsonb(v_rev + 1));
    v_option := pg_catalog.jsonb_set(v_option, '{alert_events}', v_events);
    v_context := pg_catalog.jsonb_set(v_context, '{option_trailing}', v_option);

    UPDATE public.active_trades SET
        state = v_state,
        stop_loss = COALESCE((p_transition->>'stop_loss')::numeric, stop_loss),
        exit_price = CASE WHEN v_remaining = 0 THEN (p_transition->>'exit_price')::numeric ELSE exit_price END,
        exit_type = CASE WHEN v_remaining = 0 THEN v_result ELSE exit_type END,
        exit_timestamp = CASE WHEN v_remaining = 0 THEN (p_transition->>'exit_timestamp')::timestamptz ELSE exit_timestamp END,
        pnl_points_override = CASE WHEN v_remaining = 0 THEN (p_transition->>'pnl_points')::numeric ELSE pnl_points_override END
    WHERE id = p_trade_id;
    UPDATE public.trade_analytics SET
        score = v_score, result_state = v_result, market_context = v_context,
        exit_price = CASE WHEN v_remaining = 0 THEN (p_transition->>'exit_price')::numeric ELSE exit_price END,
        exit_timestamp = CASE WHEN v_remaining = 0 THEN (p_transition->>'exit_timestamp')::timestamptz ELSE exit_timestamp END,
        pnl_points = CASE WHEN v_remaining = 0 THEN (p_transition->>'pnl_points')::numeric ELSE pnl_points END
    WHERE id = p_trade_id;
    RETURN pg_catalog.jsonb_build_object('success', true, 'trade_id', p_trade_id,
        'revision', v_rev + 1, 'option_trailing', v_option, 'active_state', v_state,
        'result_state', v_result, 'score', v_score, 'remaining_fraction', v_remaining);
END;
$$;

CREATE OR REPLACE FUNCTION public.acknowledge_option_lifecycle_alert(
    p_trade_id uuid, p_event_id text, p_status text, p_detail text DEFAULT NULL
) RETURNS jsonb
LANGUAGE plpgsql SECURITY INVOKER SET search_path = ''
AS $$
DECLARE
    v_context jsonb;
    v_event jsonb;
    v_option jsonb;
BEGIN
    IF p_status IS NULL OR p_status NOT IN ('SENDING', 'DELIVERED', 'FAILED', 'AMBIGUOUS', 'SUPPRESSED') THEN
        RAISE EXCEPTION 'Unsupported alert acknowledgement';
    END IF;
    SELECT market_context INTO STRICT v_context FROM public.trade_analytics
        WHERE id = p_trade_id FOR UPDATE;
    v_option := v_context->'option_trailing';
    v_event := v_option->'alert_events'->p_event_id;
    IF v_event IS NULL THEN RAISE EXCEPTION 'Unknown committed alert event'; END IF;
    -- A completed delivery cannot be downgraded by a delayed failure callback.
    -- Ambiguous sends require manual reconciliation, never a blind retry.
    IF v_event->>'status' IN ('DELIVERED', 'SUPPRESSED', 'AMBIGUOUS')
       OR (p_status = 'SENDING' AND v_event->>'status' = 'SENDING') THEN
        RETURN pg_catalog.jsonb_build_object('success', true, 'idempotent_skip', true,
            'claimed', false, 'option_trailing', v_option);
    END IF;
    v_event := v_event || pg_catalog.jsonb_build_object('status', p_status,
        'acknowledged_at', pg_catalog.clock_timestamp(), 'detail', p_detail);
    v_option := pg_catalog.jsonb_set(v_option, ARRAY['alert_events', p_event_id], v_event);
    UPDATE public.trade_analytics SET market_context = pg_catalog.jsonb_set(
        v_context, '{option_trailing}', v_option) WHERE id = p_trade_id;
    RETURN pg_catalog.jsonb_build_object('success', true, 'claimed', p_status = 'SENDING',
        'option_trailing', v_option);
END;
$$;

-- Service credentials are backend-only. No anonymous/authenticated API caller
-- may invoke either function; invoker permissions retain the table boundary.
REVOKE ALL ON FUNCTION public.apply_option_lifecycle_transition(uuid, integer, text, jsonb) FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.acknowledge_option_lifecycle_alert(uuid, text, text, text) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.apply_option_lifecycle_transition(uuid, integer, text, jsonb) TO service_role;
GRANT EXECUTE ON FUNCTION public.acknowledge_option_lifecycle_alert(uuid, text, text, text) TO service_role;
