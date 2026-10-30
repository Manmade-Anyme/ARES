-- Apply after 20261023_option_lifecycle_transition.sql; does not enroll existing trades.
ALTER TABLE public.trade_analytics ADD COLUMN IF NOT EXISTS option_pnl_points numeric;
COMMENT ON COLUMN public.trade_analytics.option_pnl_points IS
'Weighted one-lot equivalent option premium points; NULL when required evidence is missing.';

-- The label is linked only by the already-bound trade and its canonical signal.
-- Missing snapshots are a safe no-op; the recovery worker retries later.
CREATE OR REPLACE FUNCTION public.sync_option_reference_ml_label(p_trade_id uuid)
RETURNS jsonb LANGUAGE plpgsql SECURITY INVOKER SET search_path = '' AS $$
DECLARE
    v_active public.active_trades%ROWTYPE;
    v_analytics public.trade_analytics%ROWTYPE;
    v_option jsonb;
    v_signal_uuid text;
    v_signal_id text;
    v_count integer;
BEGIN
    SELECT * INTO STRICT v_active FROM public.active_trades WHERE id = p_trade_id FOR UPDATE;
    SELECT * INTO STRICT v_analytics FROM public.trade_analytics WHERE id = p_trade_id FOR UPDATE;
    v_option := v_analytics.market_context->'option_trailing';
    IF v_active.state NOT IN ('CLOSED', 'STOPPED_OUT')
       OR v_analytics.result_state IS NULL OR v_analytics.result_state = 'OPEN'
       OR v_option->>'phase' IS DISTINCT FROM 'CLOSED'
       OR v_option->>'policy_version' IS DISTINCT FROM 'option_atr_v1'
       OR v_option->>'accounting_formula_version' IS DISTINCT FROM 'one_lot_reference_v1' THEN
        RAISE EXCEPTION 'ML label sync requires a closed one-lot reference trade';
    END IF;
    v_signal_uuid := COALESCE(pg_catalog.to_jsonb(v_analytics)->>'signal_uuid',
                              pg_catalog.to_jsonb(v_active)->>'signal_uuid');
    v_signal_id := COALESCE(pg_catalog.to_jsonb(v_analytics)->>'signal_id',
                            pg_catalog.to_jsonb(v_active)->>'signal_id');
    UPDATE public.ml_collection AS m SET
        trade_outcome = COALESCE(m.trade_outcome, v_analytics.result_state),
        trade_score = COALESCE(m.trade_score, v_analytics.score),
        trade_pnl = COALESCE(m.trade_pnl, v_analytics.pnl_points)
    WHERE m.trade_id::text = p_trade_id::text
      AND CASE WHEN v_signal_uuid IS NOT NULL THEN pg_catalog.to_jsonb(m)->>'signal_uuid' = v_signal_uuid
               WHEN v_signal_id IS NOT NULL THEN pg_catalog.to_jsonb(m)->>'signal_id' = v_signal_id
               ELSE true END
      AND (m.trade_outcome IS NULL OR m.trade_outcome = v_analytics.result_state)
      AND (m.trade_score IS NULL OR m.trade_score = v_analytics.score)
      AND (m.trade_pnl IS NULL OR (v_analytics.pnl_points IS NOT NULL AND m.trade_pnl = v_analytics.pnl_points))
      AND (m.trade_outcome IS NULL OR m.trade_score IS NULL
           OR (m.trade_pnl IS NULL AND v_analytics.pnl_points IS NOT NULL));
    GET DIAGNOSTICS v_count = ROW_COUNT;
    RETURN pg_catalog.jsonb_build_object('success', true, 'matched', v_count);
END;
$$;
REVOKE ALL ON FUNCTION public.sync_option_reference_ml_label(uuid) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.sync_option_reference_ml_label(uuid) TO service_role;

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
    v_old_accounting jsonb;
    v_new_accounting jsonb;
    v_merged_legs jsonb;
    v_merged_leg jsonb;
    v_incoming_leg jsonb;
    v_leg_name text;
    v_field text;
    v_value jsonb;
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
    -- Legacy contexts may serialize a previously absent optional key as null.
    IF v_option->>'accounting_formula_version' IS DISTINCT FROM v_old->>'accounting_formula_version' THEN
        RAISE EXCEPTION 'Accounting formula version is immutable';
    END IF;
    IF v_old->>'accounting_formula_version' = 'one_lot_reference_v1' THEN
        v_old_accounting := COALESCE(v_old->'accounting', '{}'::jsonb);
        v_new_accounting := COALESCE(v_option->'accounting', '{}'::jsonb);
        IF pg_catalog.jsonb_typeof(v_old_accounting) IS DISTINCT FROM 'object'
           OR pg_catalog.jsonb_typeof(v_new_accounting) IS DISTINCT FROM 'object'
           OR (v_new_accounting ? 'legs' AND pg_catalog.jsonb_typeof(v_new_accounting->'legs') IS DISTINCT FROM 'object')
           OR (v_old_accounting ? 'legs' AND pg_catalog.jsonb_typeof(v_old_accounting->'legs') IS DISTINCT FROM 'object') THEN
            RAISE EXCEPTION 'Accounting and leg snapshots must be objects';
        END IF;
        v_merged_legs := COALESCE(v_old_accounting->'legs', '{}'::jsonb);
        FOR v_leg_name, v_incoming_leg IN SELECT * FROM pg_catalog.jsonb_each(
            COALESCE(v_new_accounting->'legs', '{}'::jsonb)) LOOP
            IF v_leg_name NOT IN ('T1', 'T2', 'EXIT')
               OR pg_catalog.jsonb_typeof(v_incoming_leg) IS DISTINCT FROM 'object' THEN
                RAISE EXCEPTION 'Invalid accounting leg %', v_leg_name;
            END IF;
            v_merged_leg := COALESCE(v_merged_legs->v_leg_name, '{}'::jsonb);
            FOR v_field, v_value IN SELECT * FROM pg_catalog.jsonb_each(v_incoming_leg) LOOP
                IF v_value = 'null'::jsonb THEN CONTINUE; END IF;
                IF v_merged_leg->v_field IS NOT NULL AND v_merged_leg->v_field <> 'null'::jsonb
                   AND v_merged_leg->v_field IS DISTINCT FROM v_value THEN
                    RAISE EXCEPTION 'Cannot overwrite existing accounting evidence %.%', v_leg_name, v_field;
                END IF;
                v_merged_leg := pg_catalog.jsonb_set(v_merged_leg, ARRAY[v_field], v_value, true);
            END LOOP;
            v_merged_legs := pg_catalog.jsonb_set(v_merged_legs, ARRAY[v_leg_name], v_merged_leg, true);
        END LOOP;
        v_new_accounting := v_old_accounting || v_new_accounting;
        v_new_accounting := pg_catalog.jsonb_set(v_new_accounting, '{legs}', v_merged_legs, true);
        -- An old same-milestone snapshot may not know a booked total already
        -- derived from canonical evidence; do not erase it with null.
        IF (v_option->>'highest_milestone')::integer = (v_old->>'highest_milestone')::integer THEN
            FOR v_field IN SELECT pg_catalog.unnest(ARRAY['spot_booked_points', 'option_booked_points']) LOOP
                IF v_new_accounting->>v_field IS NULL AND v_old_accounting->>v_field IS NOT NULL THEN
                    v_new_accounting := pg_catalog.jsonb_set(v_new_accounting, ARRAY[v_field], v_old_accounting->v_field, true);
                END IF;
            END LOOP;
        END IF;
        v_option := pg_catalog.jsonb_set(v_option, '{accounting}', v_new_accounting, true);
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
    IF v_remaining = 0 AND v_option->>'accounting_formula_version' = 'one_lot_reference_v1' THEN
        IF p_transition->>'pnl_points' IS NOT NULL AND (
            v_option #>> '{accounting,legs,EXIT,spot_price}' IS NULL
            OR (v_highest >= 1 AND v_option #>> '{accounting,legs,T1,spot_price}' IS NULL)
            OR (v_highest >= 2 AND v_option #>> '{accounting,legs,T2,spot_price}' IS NULL)) THEN
            RAISE EXCEPTION 'Spot points require all applicable spot leg prices';
        END IF;
        IF p_transition->>'option_pnl_points' IS NOT NULL AND (
            v_option->>'original_option_entry' IS NULL
            OR v_option #>> '{accounting,legs,EXIT,option_price}' IS NULL
            OR (v_highest >= 1 AND v_option #>> '{accounting,legs,T1,option_price}' IS NULL)
            OR (v_highest >= 2 AND v_option #>> '{accounting,legs,T2,option_price}' IS NULL)) THEN
            RAISE EXCEPTION 'Option points require the entry and all applicable option leg prices';
        END IF;
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
        pnl_points = CASE WHEN v_remaining = 0 THEN (p_transition->>'pnl_points')::numeric ELSE pnl_points END,
        option_pnl_points = CASE WHEN v_remaining = 0 THEN (p_transition->>'option_pnl_points')::numeric ELSE option_pnl_points END
    WHERE id = p_trade_id;
    IF v_remaining = 0 AND v_option->>'accounting_formula_version' = 'one_lot_reference_v1' THEN
        PERFORM public.sync_option_reference_ml_label(p_trade_id);
    END IF;
    RETURN pg_catalog.jsonb_build_object('success', true, 'trade_id', p_trade_id,
        'revision', v_rev + 1, 'option_trailing', v_option, 'active_state', v_state,
        'result_state', v_result, 'score', v_score, 'remaining_fraction', v_remaining);
END;
$$;

-- This repair path only fills absent evidence on an already closed reference
-- trade. It cannot alter its exit, score, order, outbox, or existing evidence.
CREATE OR REPLACE FUNCTION public.apply_option_accounting_correction(
    p_trade_id uuid, p_expected_revision integer, p_event_id text,
    p_accounting jsonb, p_spot_pnl_points numeric, p_option_pnl_points numeric
) RETURNS jsonb
LANGUAGE plpgsql SECURITY INVOKER SET search_path = ''
AS $$
DECLARE
    v_active public.active_trades%ROWTYPE;
    v_analytics public.trade_analytics%ROWTYPE;
    v_context jsonb;
    v_option jsonb;
    v_accounting jsonb;
    v_legs jsonb;
    v_old_leg jsonb;
    v_new_leg jsonb;
    v_corrections jsonb;
    v_request jsonb;
    v_name text;
    v_key text;
    v_value jsonb;
    v_rev integer;
    v_highest integer;
    v_spot numeric;
    v_premium numeric;
    v_expected numeric;
    v_weight numeric;
    v_entry numeric;
    v_direction numeric;
BEGIN
    IF p_expected_revision IS NULL OR p_expected_revision < 1
       OR p_event_id IS NULL OR pg_catalog.btrim(p_event_id) = ''
       OR pg_catalog.jsonb_typeof(p_accounting) IS DISTINCT FROM 'object'
       OR pg_catalog.jsonb_typeof(p_accounting->'legs') IS DISTINCT FROM 'object' THEN
        RAISE EXCEPTION 'A positive expected revision, stable event id and accounting legs are required';
    END IF;
    SELECT * INTO STRICT v_active FROM public.active_trades WHERE id = p_trade_id FOR UPDATE;
    SELECT * INTO STRICT v_analytics FROM public.trade_analytics WHERE id = p_trade_id FOR UPDATE;
    v_context := COALESCE(v_analytics.market_context, '{}'::jsonb);
    v_option := v_context->'option_trailing';
    IF v_option->>'policy_version' IS DISTINCT FROM 'option_atr_v1' THEN
        RAISE EXCEPTION 'Trade is not enrolled in option_atr_v1';
    END IF;
    IF v_option->>'accounting_formula_version' IS DISTINCT FROM 'one_lot_reference_v1' THEN
        RAISE EXCEPTION 'Trade is not enrolled in one-lot reference accounting';
    END IF;
    v_rev := COALESCE((v_option->>'revision')::integer, 1);
    v_corrections := COALESCE(v_option->'accounting_corrections', '{}'::jsonb);
    v_request := pg_catalog.jsonb_build_object('accounting', p_accounting,
        'spot_pnl_points', p_spot_pnl_points, 'option_pnl_points', p_option_pnl_points);
    IF v_corrections ? p_event_id THEN
        IF v_corrections->p_event_id IS DISTINCT FROM v_request THEN
            RAISE EXCEPTION 'Correction event id already used for different evidence';
        END IF;
        RETURN pg_catalog.jsonb_build_object('success', true, 'idempotent_skip', true,
            'trade_id', p_trade_id, 'revision', v_rev, 'option_trailing', v_option);
    END IF;
    IF v_rev <> p_expected_revision THEN
        RAISE EXCEPTION 'Revision conflict: expected %, found %', p_expected_revision, v_rev
            USING ERRCODE = '40001';
    END IF;
    IF v_active.state NOT IN ('CLOSED', 'STOPPED_OUT')
       OR v_analytics.result_state IS NULL OR v_analytics.result_state = 'OPEN'
       OR v_option->>'phase' IS DISTINCT FROM 'CLOSED' THEN
        RAISE EXCEPTION 'Accounting correction requires a closed trade';
    END IF;
    v_highest := (v_option->>'highest_milestone')::integer;
    v_accounting := COALESCE(v_option->'accounting', '{"legs":{}}'::jsonb);
    v_legs := COALESCE(v_accounting->'legs', '{}'::jsonb);
    FOR v_key IN SELECT pg_catalog.jsonb_object_keys(p_accounting) LOOP
        IF v_key NOT IN ('legs', 'spot_booked_points', 'option_booked_points',
                         'spot_total_points', 'option_total_points') THEN
            RAISE EXCEPTION 'Unsupported accounting field %', v_key;
        END IF;
    END LOOP;
    FOR v_name, v_new_leg IN SELECT * FROM pg_catalog.jsonb_each(p_accounting->'legs') LOOP
        IF v_name NOT IN ('T1', 'T2', 'EXIT')
           OR (v_name = 'T1' AND v_highest < 1)
           OR (v_name = 'T2' AND v_highest < 2)
           OR pg_catalog.jsonb_typeof(v_new_leg) IS DISTINCT FROM 'object' THEN
            RAISE EXCEPTION 'Invalid accounting leg %', v_name;
        END IF;
        v_old_leg := COALESCE(v_legs->v_name, '{}'::jsonb);
        FOR v_key, v_value IN SELECT * FROM pg_catalog.jsonb_each(v_new_leg) LOOP
            IF v_key NOT IN ('event_at', 'spot_price', 'spot_source', 'option_price',
                             'option_source', 'option_observed_at', 'approximate',
                             'spot_observed_at', 'spot_approximate', 'source_minute') THEN
                RAISE EXCEPTION 'Unsupported accounting leg field %', v_key;
            END IF;
            IF v_value = 'null'::jsonb THEN CONTINUE; END IF;
            IF v_old_leg->v_key IS NOT NULL AND v_old_leg->v_key <> 'null'::jsonb
               AND v_old_leg->v_key IS DISTINCT FROM v_value THEN
                RAISE EXCEPTION 'Cannot overwrite existing accounting evidence %.%', v_name, v_key;
            END IF;
            v_old_leg := pg_catalog.jsonb_set(v_old_leg, ARRAY[v_key], v_value, true);
        END LOOP;
        v_legs := pg_catalog.jsonb_set(v_legs, ARRAY[v_name], v_old_leg, true);
    END LOOP;
    v_accounting := pg_catalog.jsonb_set(v_accounting, '{legs}', v_legs, true);
    FOR v_key IN SELECT pg_catalog.unnest(ARRAY['spot_booked_points', 'option_booked_points',
                                               'spot_total_points', 'option_total_points']) LOOP
        v_value := p_accounting->v_key;
        IF v_value IS NULL OR v_value = 'null'::jsonb THEN CONTINUE; END IF;
        IF v_accounting->v_key IS NOT NULL AND v_accounting->v_key <> 'null'::jsonb
           AND v_accounting->v_key IS DISTINCT FROM v_value THEN
            RAISE EXCEPTION 'Cannot overwrite existing accounting total %', v_key;
        END IF;
        v_accounting := pg_catalog.jsonb_set(v_accounting, ARRAY[v_key], v_value, true);
    END LOOP;
    v_spot := COALESCE(v_analytics.pnl_points, p_spot_pnl_points);
    v_premium := COALESCE(v_analytics.option_pnl_points, p_option_pnl_points);
    IF (v_accounting->>'spot_total_points' IS NOT NULL AND v_spot IS NULL)
       OR (v_accounting->>'option_total_points' IS NOT NULL AND v_premium IS NULL) THEN
        RAISE EXCEPTION 'Accounting totals require matching persisted points';
    END IF;
    IF (v_analytics.pnl_points IS NOT NULL AND p_spot_pnl_points IS NOT NULL
        AND v_analytics.pnl_points <> p_spot_pnl_points)
       OR (v_analytics.option_pnl_points IS NOT NULL AND p_option_pnl_points IS NOT NULL
        AND v_analytics.option_pnl_points <> p_option_pnl_points) THEN
        RAISE EXCEPTION 'Cannot overwrite existing terminal totals';
    END IF;
    IF v_spot IS NOT NULL AND (
        v_legs->'EXIT'->>'spot_price' IS NULL
        OR (v_highest >= 1 AND v_legs->'T1'->>'spot_price' IS NULL)
        OR (v_highest >= 2 AND v_legs->'T2'->>'spot_price' IS NULL)) THEN
        RAISE EXCEPTION 'Spot points require all applicable spot leg prices';
    END IF;
    IF v_premium IS NOT NULL AND (
        v_option->>'original_option_entry' IS NULL OR v_legs->'EXIT'->>'option_price' IS NULL
        OR (v_highest >= 1 AND v_legs->'T1'->>'option_price' IS NULL)
        OR (v_highest >= 2 AND v_legs->'T2'->>'option_price' IS NULL)) THEN
        RAISE EXCEPTION 'Option points require the entry and all applicable option leg prices';
    END IF;
    IF v_spot IS NOT NULL THEN
        IF v_active.entry_price IS NULL OR v_active.direction IS NULL OR v_active.direction NOT IN ('BULLISH', 'BEARISH') THEN
            RAISE EXCEPTION 'Spot entry and direction are required for spot points';
        END IF;
        v_entry := v_active.entry_price;
        v_direction := CASE WHEN v_active.direction = 'BULLISH' THEN 1 ELSE -1 END;
        v_weight := CASE v_highest WHEN 0 THEN 1 WHEN 1 THEN .5 ELSE .2 END;
        v_expected := v_weight * v_direction * ((v_legs->'EXIT'->>'spot_price')::numeric - v_entry);
        IF v_highest >= 1 THEN
            v_expected := v_expected + .5 * v_direction * ((v_legs->'T1'->>'spot_price')::numeric - v_entry);
        END IF;
        IF v_highest >= 2 THEN
            v_expected := v_expected + .3 * v_direction * ((v_legs->'T2'->>'spot_price')::numeric - v_entry);
        END IF;
        IF v_spot <> pg_catalog.round(v_expected, 8) OR
           ((v_accounting->>'spot_total_points') IS NOT NULL AND
            (v_accounting->>'spot_total_points')::numeric <> v_spot) THEN
            RAISE EXCEPTION 'Spot total does not match weighted leg prices';
        END IF;
    END IF;
    IF v_premium IS NOT NULL THEN
        v_entry := (v_option->>'original_option_entry')::numeric;
        v_weight := CASE v_highest WHEN 0 THEN 1 WHEN 1 THEN .5 ELSE .2 END;
        v_expected := v_weight * ((v_legs->'EXIT'->>'option_price')::numeric - v_entry);
        IF v_highest >= 1 THEN
            v_expected := v_expected + .5 * ((v_legs->'T1'->>'option_price')::numeric - v_entry);
        END IF;
        IF v_highest >= 2 THEN
            v_expected := v_expected + .3 * ((v_legs->'T2'->>'option_price')::numeric - v_entry);
        END IF;
        IF v_premium <> pg_catalog.round(v_expected, 8) OR
           ((v_accounting->>'option_total_points') IS NOT NULL AND
            (v_accounting->>'option_total_points')::numeric <> v_premium) THEN
            RAISE EXCEPTION 'Option total does not match weighted leg prices';
        END IF;
    END IF;
    v_corrections := pg_catalog.jsonb_set(v_corrections, ARRAY[p_event_id], v_request, true);
    v_option := pg_catalog.jsonb_set(v_option, '{accounting}', v_accounting, true);
    v_option := pg_catalog.jsonb_set(v_option, '{accounting_corrections}', v_corrections, true);
    v_option := pg_catalog.jsonb_set(v_option, '{revision}', pg_catalog.to_jsonb(v_rev + 1), true);
    v_context := pg_catalog.jsonb_set(v_context, '{option_trailing}', v_option, true);
    UPDATE public.active_trades SET pnl_points_override = COALESCE(pnl_points_override, v_spot),
        exit_price = COALESCE(exit_price, (v_legs->'EXIT'->>'spot_price')::numeric)
        WHERE id = p_trade_id;
    UPDATE public.trade_analytics SET market_context = v_context,
        pnl_points = v_spot, option_pnl_points = v_premium,
        exit_price = COALESCE(exit_price, (v_legs->'EXIT'->>'spot_price')::numeric)
        WHERE id = p_trade_id;
    PERFORM public.sync_option_reference_ml_label(p_trade_id);
    RETURN pg_catalog.jsonb_build_object('success', true, 'trade_id', p_trade_id,
        'revision', v_rev + 1, 'option_trailing', v_option,
        'pnl_points', v_spot, 'option_pnl_points', v_premium);
END;
$$;
REVOKE ALL ON FUNCTION public.apply_option_lifecycle_transition(uuid, integer, text, jsonb) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.apply_option_lifecycle_transition(uuid, integer, text, jsonb) TO service_role;
REVOKE ALL ON FUNCTION public.apply_option_accounting_correction(uuid, integer, text, jsonb, numeric, numeric) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.apply_option_accounting_correction(uuid, integer, text, jsonb, numeric, numeric) TO service_role;

-- Backend can check readiness without opening a trade or modifying data.
CREATE OR REPLACE FUNCTION public.option_reference_accounting_capabilities()
RETURNS jsonb LANGUAGE sql SECURITY INVOKER SET search_path = '' AS $$
    SELECT pg_catalog.jsonb_build_object(
        'schema_version', 'option_reference_accounting_v1',
        'ready',
        EXISTS (SELECT 1 FROM pg_catalog.pg_attribute
                WHERE attrelid = pg_catalog.to_regclass('public.trade_analytics')
                  AND attname = 'option_pnl_points' AND NOT attisdropped)
        AND pg_catalog.to_regprocedure('public.apply_option_lifecycle_transition(uuid,integer,text,jsonb)') IS NOT NULL
        AND pg_catalog.to_regprocedure('public.acknowledge_option_lifecycle_alert(uuid,text,text,text)') IS NOT NULL
        AND pg_catalog.to_regprocedure('public.apply_option_accounting_correction(uuid,integer,text,jsonb,numeric,numeric)') IS NOT NULL
        AND pg_catalog.to_regprocedure('public.list_incomplete_option_accounting(integer,integer)') IS NOT NULL
        AND pg_catalog.to_regprocedure('public.sync_option_reference_ml_label(uuid)') IS NOT NULL
        AND pg_catalog.has_table_privilege(CURRENT_USER, 'public.trade_analytics', 'SELECT')
        AND pg_catalog.has_table_privilege(CURRENT_USER, 'public.trade_analytics', 'UPDATE')
        AND pg_catalog.has_table_privilege(CURRENT_USER, 'public.active_trades', 'SELECT')
        AND pg_catalog.has_table_privilege(CURRENT_USER, 'public.active_trades', 'UPDATE')
        AND pg_catalog.has_table_privilege(CURRENT_USER, 'public.ml_collection', 'SELECT')
        AND pg_catalog.has_table_privilege(CURRENT_USER, 'public.ml_collection', 'UPDATE')
        AND pg_catalog.has_function_privilege(CURRENT_USER,
            pg_catalog.to_regprocedure('public.apply_option_lifecycle_transition(uuid,integer,text,jsonb)'), 'EXECUTE')
        AND pg_catalog.has_function_privilege(CURRENT_USER,
            pg_catalog.to_regprocedure('public.acknowledge_option_lifecycle_alert(uuid,text,text,text)'), 'EXECUTE')
        AND pg_catalog.has_function_privilege(CURRENT_USER,
            pg_catalog.to_regprocedure('public.apply_option_accounting_correction(uuid,integer,text,jsonb,numeric,numeric)'), 'EXECUTE')
        AND pg_catalog.has_function_privilege(CURRENT_USER,
            pg_catalog.to_regprocedure('public.list_incomplete_option_accounting(integer,integer)'), 'EXECUTE')
        AND pg_catalog.has_function_privilege(CURRENT_USER,
            pg_catalog.to_regprocedure('public.sync_option_reference_ml_label(uuid)'), 'EXECUTE')
    );
$$;
REVOKE ALL ON FUNCTION public.option_reference_accounting_capabilities() FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.option_reference_accounting_capabilities() TO service_role;

-- Bounded, backend-only recovery scan.  The marker excludes all pre-existing
-- policy trades, even if their typed context later grows an accounting key.
CREATE OR REPLACE FUNCTION public.list_incomplete_option_accounting(
    p_limit integer DEFAULT 100, p_offset integer DEFAULT 0
) RETURNS jsonb LANGUAGE plpgsql SECURITY INVOKER SET search_path = '' AS $$
DECLARE
    v_rows jsonb;
BEGIN
    IF p_limit IS NULL OR p_limit < 1 OR p_limit > 500 OR p_offset IS NULL OR p_offset < 0 THEN
        RAISE EXCEPTION 'Recovery page requires limit 1..500 and non-negative offset';
    END IF;
    SELECT COALESCE(pg_catalog.jsonb_agg(page.record ORDER BY page.exit_timestamp NULLS LAST, page.id), '[]'::jsonb)
    INTO v_rows FROM (
        SELECT a.id, t.exit_timestamp, pg_catalog.jsonb_build_object(
            'active_trade', pg_catalog.to_jsonb(a), 'option_trailing', t.market_context->'option_trailing',
            'score', t.score, 'result_state', t.result_state, 'exit_timestamp', t.exit_timestamp,
            'pnl_points', t.pnl_points, 'option_pnl_points', t.option_pnl_points) AS record
        FROM public.trade_analytics t JOIN public.active_trades a ON a.id = t.id
        WHERE a.state IN ('CLOSED', 'STOPPED_OUT') AND t.result_state IS NOT NULL AND t.result_state <> 'OPEN'
          AND t.market_context->'option_trailing'->>'phase' = 'CLOSED'
          AND t.market_context->'option_trailing'->>'policy_version' = 'option_atr_v1'
          AND t.market_context->'option_trailing'->>'accounting_formula_version' = 'one_lot_reference_v1'
          AND pg_catalog.jsonb_typeof(t.market_context->'option_trailing'->'accounting') = 'object'
          AND (
              t.pnl_points IS NULL OR t.option_pnl_points IS NULL
              OR t.market_context #>> '{option_trailing,accounting,legs,EXIT,spot_price}' IS NULL
              OR t.market_context #>> '{option_trailing,accounting,legs,EXIT,option_price}' IS NULL
              OR t.market_context #>> '{option_trailing,original_option_entry}' IS NULL
              OR (COALESCE((t.market_context #>> '{option_trailing,highest_milestone}')::integer, 0) >= 1 AND (
                  t.market_context #>> '{option_trailing,accounting,legs,T1,spot_price}' IS NULL OR
                  t.market_context #>> '{option_trailing,accounting,legs,T1,option_price}' IS NULL))
              OR (COALESCE((t.market_context #>> '{option_trailing,highest_milestone}')::integer, 0) >= 2 AND (
                  t.market_context #>> '{option_trailing,accounting,legs,T2,spot_price}' IS NULL OR
                  t.market_context #>> '{option_trailing,accounting,legs,T2,option_price}' IS NULL))
              OR NOT EXISTS (
                  SELECT 1 FROM public.ml_collection m
                  WHERE m.trade_id::text = t.id::text
                    AND CASE
                        WHEN COALESCE(pg_catalog.to_jsonb(t)->>'signal_uuid', pg_catalog.to_jsonb(a)->>'signal_uuid') IS NOT NULL
                        THEN pg_catalog.to_jsonb(m)->>'signal_uuid' = COALESCE(pg_catalog.to_jsonb(t)->>'signal_uuid', pg_catalog.to_jsonb(a)->>'signal_uuid')
                        WHEN COALESCE(pg_catalog.to_jsonb(t)->>'signal_id', pg_catalog.to_jsonb(a)->>'signal_id') IS NOT NULL
                        THEN pg_catalog.to_jsonb(m)->>'signal_id' = COALESCE(pg_catalog.to_jsonb(t)->>'signal_id', pg_catalog.to_jsonb(a)->>'signal_id')
                        ELSE true END
                    AND m.trade_outcome = t.result_state
                    AND m.trade_score IS NOT DISTINCT FROM t.score
                    AND m.trade_pnl IS NOT DISTINCT FROM t.pnl_points
              )
          )
        ORDER BY t.exit_timestamp NULLS LAST, a.id LIMIT p_limit OFFSET p_offset
    ) page;
    RETURN v_rows;
END;
$$;
REVOKE ALL ON FUNCTION public.list_incomplete_option_accounting(integer, integer) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.list_incomplete_option_accounting(integer, integer) TO service_role;
