-- Apply after 20261029_option_trade_accounting.sql.
-- Independent reference-only premium accounting; this does not enroll trades
-- or alter any spot, execution, score, or option-ATR protection state.
ALTER TABLE public.trade_analytics
    ADD COLUMN IF NOT EXISTS option_reference_pnl_points numeric;

COMMENT ON COLUMN public.trade_analytics.option_reference_pnl_points IS
'Unweighted one-lot option premium points (observed final exit minus observed entry); NULL until both exact-contract reference prices are valid.';

CREATE OR REPLACE FUNCTION public.apply_option_reference_capture(
    p_trade_id uuid,
    p_expected_revision integer,
    p_event_id text,
    p_event jsonb
) RETURNS jsonb
LANGUAGE plpgsql SECURITY INVOKER SET search_path = '' AS $$
DECLARE
    v_row public.trade_analytics%ROWTYPE;
    v_context jsonb;
    v_reference jsonb;
    v_events jsonb;
    v_existing jsonb;
    v_kind text;
    v_event_at timestamptz;
    v_observed_at timestamptz;
    v_price numeric;
    v_approximate boolean;
    v_source text;
    v_lot_size numeric;
    v_entry jsonb;
    v_exit jsonb;
    v_points numeric;
    v_revision integer;
    v_name text;
    v_milestone jsonb;
BEGIN
    IF p_expected_revision IS NULL OR p_expected_revision < 1
       OR p_event_id IS NULL OR pg_catalog.btrim(p_event_id) = ''
       OR pg_catalog.jsonb_typeof(p_event) IS DISTINCT FROM 'object' THEN
        RAISE EXCEPTION 'A positive revision, stable event id and event object are required';
    END IF;

    SELECT * INTO STRICT v_row
    FROM public.trade_analytics
    WHERE id = p_trade_id
    FOR UPDATE;

    v_context := COALESCE(v_row.market_context, '{}'::jsonb);
    v_reference := v_context->'option_reference';
    IF pg_catalog.jsonb_typeof(v_reference) IS DISTINCT FROM 'object'
       OR v_reference->>'formula_version' IS DISTINCT FROM 'one_lot_hold_to_exit_v1' THEN
        RAISE EXCEPTION 'Trade is not enrolled in one_lot_hold_to_exit_v1';
    END IF;
    IF pg_catalog.jsonb_typeof(v_reference->'contract') IS DISTINCT FROM 'object'
       OR pg_catalog.jsonb_typeof(v_reference->'reference_lots') IS DISTINCT FROM 'number'
       OR (v_reference->>'reference_lots')::numeric <> 1
       OR pg_catalog.jsonb_typeof(v_reference #> '{contract,lot_size}') IS DISTINCT FROM 'number'
       OR v_reference #>> '{contract,exchange_segment}' IS DISTINCT FROM 'NSE_FNO'
       OR (v_reference #>> '{contract,option_type}' IS DISTINCT FROM 'CE'
           AND v_reference #>> '{contract,option_type}' IS DISTINCT FROM 'PE')
       OR COALESCE(v_reference #>> '{contract,security_id}', '') !~ '^[0-9]+$'
       OR pg_catalog.jsonb_typeof(v_reference #> '{contract,strike}') IS DISTINCT FROM 'number'
       OR COALESCE(v_reference #>> '{contract,expiry_date}', '') !~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}$'
       OR pg_catalog.jsonb_typeof(v_reference->'entry_event_at') IS DISTINCT FROM 'string' THEN
        RAISE EXCEPTION 'A pinned one-lot contract is required';
    END IF;
    v_lot_size := (v_reference #>> '{contract,lot_size}')::numeric;
    IF (v_reference #>> '{contract,strike}')::numeric <= 0
       OR (v_reference #>> '{contract,security_id}')::numeric <= 0
       OR (v_reference #>> '{contract,expiry_date}')::date <
          (v_reference->>'entry_event_at')::timestamptz::date THEN
        RAISE EXCEPTION 'Pinned option contract must have a positive strike and security id and a valid expiry';
    END IF;
    IF v_lot_size <= 0 OR v_lot_size <> pg_catalog.trunc(v_lot_size)
       OR v_lot_size::text IN ('NaN', 'Infinity', '-Infinity') THEN
        RAISE EXCEPTION 'Pinned contract lot size must be a positive integer';
    END IF;

    v_events := COALESCE(v_reference->'applied_events', '{}'::jsonb);
    IF pg_catalog.jsonb_typeof(v_events) IS DISTINCT FROM 'object' THEN
        RAISE EXCEPTION 'Applied event ledger must be an object';
    END IF;
    IF v_events ? p_event_id THEN
        v_existing := v_events->p_event_id;
        IF v_existing IS DISTINCT FROM p_event THEN
            RAISE EXCEPTION 'Capture event id already used for different evidence';
        END IF;
        RETURN pg_catalog.jsonb_build_object(
            'success', true, 'idempotent_skip', true, 'trade_id', p_trade_id,
            'revision', COALESCE((v_reference->>'revision')::integer, 1),
            'option_reference', v_reference,
            'option_reference_pnl_points', v_row.option_reference_pnl_points);
    END IF;

    v_revision := COALESCE((v_reference->>'revision')::integer, 1);
    IF v_revision <> p_expected_revision THEN
        RAISE EXCEPTION 'Revision conflict: expected %, found %', p_expected_revision, v_revision
            USING ERRCODE = '40001';
    END IF;

    v_kind := p_event->>'kind';
    IF v_kind = 'ENTRY_CAPTURE' OR v_kind = 'EXIT_CAPTURE' THEN
        IF pg_catalog.jsonb_typeof(p_event->'evidence') IS DISTINCT FROM 'object'
           OR pg_catalog.jsonb_typeof(p_event->'event_at') IS DISTINCT FROM 'string' THEN
            RAISE EXCEPTION 'Price capture requires an event timestamp and evidence object';
        END IF;
        v_event_at := (p_event->>'event_at')::timestamptz;
        IF NOT pg_catalog.isfinite(v_event_at) THEN
            RAISE EXCEPTION 'Price event timestamp must be finite';
        END IF;
        IF v_event_at::date > (v_reference #>> '{contract,expiry_date}')::date THEN
            RAISE EXCEPTION 'Option price event cannot occur after the pinned contract expiry';
        END IF;
        IF pg_catalog.jsonb_typeof(p_event #> '{evidence,price}') IS DISTINCT FROM 'number'
           OR pg_catalog.jsonb_typeof(p_event #> '{evidence,observed_at}') IS DISTINCT FROM 'string'
           OR pg_catalog.jsonb_typeof(p_event #> '{evidence,source}') IS DISTINCT FROM 'string'
           OR pg_catalog.jsonb_typeof(p_event #> '{evidence,approximate}') IS DISTINCT FROM 'boolean' THEN
            RAISE EXCEPTION 'Price evidence requires price, observed_at, source and approximation status';
        END IF;
        v_price := (p_event #>> '{evidence,price}')::numeric;
        v_observed_at := (p_event #>> '{evidence,observed_at}')::timestamptz;
        v_source := p_event #>> '{evidence,source}';
        v_approximate := (p_event #>> '{evidence,approximate}')::boolean;
        IF NOT pg_catalog.isfinite(v_observed_at) OR v_price <= 0
           OR v_price::text IN ('NaN', 'Infinity', '-Infinity')
           OR v_source IS NULL OR pg_catalog.btrim(v_source) = '' THEN
            RAISE EXCEPTION 'Price evidence must be finite, positive and sourced';
        END IF;
        IF v_approximate THEN
            IF v_source IS DISTINCT FROM 'completed_1m_close'
               OR v_observed_at <> pg_catalog.date_trunc('minute', v_event_at) + interval '1 minute' THEN
                RAISE EXCEPTION 'Approximate evidence must be the matching completed one-minute close';
            END IF;
        ELSIF v_source IS DISTINCT FROM 'dhan_last_trade'
              OR abs(extract(epoch FROM (v_observed_at - v_event_at))) > 5 THEN
            RAISE EXCEPTION 'Exact quote evidence must be within five seconds of its event';
        END IF;
        IF v_kind = 'ENTRY_CAPTURE' THEN
            IF v_reference->>'entry_event_at' IS NULL
               OR (v_reference->>'entry_event_at')::timestamptz <> v_event_at
               OR (v_row.entry_timestamp IS NOT NULL AND v_row.entry_timestamp <> v_event_at) THEN
                RAISE EXCEPTION 'Entry evidence must match the canonical trade entry event';
            END IF;
            v_existing := v_reference->'entry';
            IF v_existing IS NOT NULL AND v_existing <> 'null'::jsonb
               AND v_existing IS DISTINCT FROM p_event->'evidence' THEN
                RAISE EXCEPTION 'Cannot overwrite existing option entry evidence';
            END IF;
            v_reference := pg_catalog.jsonb_set(v_reference, '{entry}', p_event->'evidence', true);
        ELSE
            IF pg_catalog.jsonb_typeof(p_event->'exit_reason') IS DISTINCT FROM 'string'
               OR v_reference->>'exit_event_at' IS NULL
               OR (v_reference->>'exit_event_at')::timestamptz <> v_event_at THEN
                RAISE EXCEPTION 'Final-exit event timestamp is immutable';
            END IF;
            IF v_row.exit_timestamp IS NULL OR v_row.exit_timestamp <> v_event_at
               OR v_row.result_state IS NULL OR v_row.result_state = 'OPEN'
               OR v_row.result_state IS DISTINCT FROM p_event->>'exit_reason' THEN
                RAISE EXCEPTION 'Exit evidence must match the persisted terminal trade event';
            END IF;
            v_existing := v_reference->'exit';
            IF v_existing IS NOT NULL AND v_existing <> 'null'::jsonb
               AND v_existing IS DISTINCT FROM p_event->'evidence' THEN
                RAISE EXCEPTION 'Cannot overwrite existing option exit evidence';
            END IF;
            v_reference := pg_catalog.jsonb_set(v_reference, '{exit_event_at}', pg_catalog.to_jsonb(v_event_at), true);
            v_reference := pg_catalog.jsonb_set(v_reference, '{exit_reason}', p_event->'exit_reason', true);
            v_reference := pg_catalog.jsonb_set(v_reference, '{exit}', p_event->'evidence', true);
            v_reference := pg_catalog.jsonb_set(v_reference, '{status}', '"CLOSED"'::jsonb, true);
        END IF;
    ELSIF v_kind = 'EXIT_MARKER' THEN
        IF pg_catalog.jsonb_typeof(p_event->'event_at') IS DISTINCT FROM 'string'
           OR pg_catalog.jsonb_typeof(p_event->'exit_reason') IS DISTINCT FROM 'string'
           OR v_row.exit_timestamp IS NULL OR v_row.result_state IS NULL OR v_row.result_state = 'OPEN' THEN
            RAISE EXCEPTION 'Final-exit marker requires a persisted closed trade';
        END IF;
        v_event_at := (p_event->>'event_at')::timestamptz;
        IF NOT pg_catalog.isfinite(v_event_at) OR v_row.exit_timestamp <> v_event_at
           OR v_row.result_state IS DISTINCT FROM p_event->>'exit_reason' THEN
            RAISE EXCEPTION 'Final-exit marker does not match the persisted trade exit';
        END IF;
        IF v_reference->>'exit_event_at' IS NOT NULL
           AND (v_reference->>'exit_event_at')::timestamptz <> v_event_at THEN
            RAISE EXCEPTION 'Final-exit event timestamp is immutable';
        END IF;
        IF v_reference->>'exit_reason' IS NOT NULL
           AND v_reference->>'exit_reason' IS DISTINCT FROM p_event->>'exit_reason' THEN
            RAISE EXCEPTION 'Final-exit reason is immutable';
        END IF;
        v_reference := pg_catalog.jsonb_set(v_reference, '{exit_event_at}', pg_catalog.to_jsonb(v_event_at), true);
        v_reference := pg_catalog.jsonb_set(v_reference, '{exit_reason}', p_event->'exit_reason', true);
        v_reference := pg_catalog.jsonb_set(v_reference, '{status}', '"EXIT_PENDING"'::jsonb, true);
    ELSIF v_kind = 'MILESTONE' THEN
        v_name := p_event->>'name';
        v_milestone := p_event->'milestone';
        IF (v_name IS DISTINCT FROM 'T1' AND v_name IS DISTINCT FROM 'T2')
           OR pg_catalog.jsonb_typeof(v_milestone) IS DISTINCT FROM 'object'
           OR pg_catalog.jsonb_typeof(v_milestone->'event_at') IS DISTINCT FROM 'string'
           OR pg_catalog.jsonb_typeof(v_milestone->'spot_target') IS DISTINCT FROM 'number'
           OR pg_catalog.jsonb_typeof(v_milestone->'source') IS DISTINCT FROM 'string'
           OR pg_catalog.jsonb_typeof(v_milestone->'time_basis') IS DISTINCT FROM 'string'
           OR (pg_catalog.jsonb_typeof(v_milestone->'spot_price') IS DISTINCT FROM 'number'
               AND pg_catalog.jsonb_typeof(v_milestone->'spot_price') IS DISTINCT FROM 'null') THEN
            RAISE EXCEPTION 'Spot milestone requires T1/T2, timestamp, target, source, time basis, and numeric-or-null spot price';
        END IF;
        v_event_at := (v_milestone->>'event_at')::timestamptz;
        IF NOT pg_catalog.isfinite(v_event_at)
           OR v_event_at < (v_reference->>'entry_event_at')::timestamptz
           OR (v_reference->>'exit_event_at' IS NOT NULL
               AND v_event_at > (v_reference->>'exit_event_at')::timestamptz)
           OR (v_milestone->>'spot_target')::numeric <= 0
           OR pg_catalog.btrim(v_milestone->>'source') = ''
           OR pg_catalog.btrim(v_milestone->>'time_basis') = '' THEN
            RAISE EXCEPTION 'Spot milestone values must be valid';
        END IF;
        IF pg_catalog.jsonb_typeof(v_milestone->'spot_price') = 'number'
           AND ((v_milestone->>'spot_price')::numeric <= 0
                OR (v_milestone->>'spot_price')::numeric::text IN ('NaN', 'Infinity', '-Infinity')) THEN
            RAISE EXCEPTION 'Observed milestone spot price must be finite and positive';
        END IF;
        v_existing := v_reference #> ARRAY['milestones', v_name];
        IF v_existing IS NOT NULL AND v_existing <> 'null'::jsonb
           AND v_existing IS DISTINCT FROM v_milestone THEN
            RAISE EXCEPTION 'Cannot overwrite existing spot milestone evidence %', v_name;
        END IF;
        v_reference := pg_catalog.jsonb_set(
            v_reference, ARRAY['milestones', v_name], v_milestone, true);
    ELSE
        RAISE EXCEPTION 'Unsupported option reference event kind %', v_kind;
    END IF;

    v_entry := v_reference->'entry';
    v_exit := v_reference->'exit';
    v_points := NULL;
    IF pg_catalog.jsonb_typeof(v_entry) = 'object' AND pg_catalog.jsonb_typeof(v_exit) = 'object' THEN
        IF (v_reference->>'entry_event_at')::timestamptz > (v_reference->>'exit_event_at')::timestamptz
           OR (v_entry->>'observed_at')::timestamptz > (v_exit->>'observed_at')::timestamptz THEN
            RAISE EXCEPTION 'Option entry evidence must precede final-exit evidence';
        END IF;
        v_points := pg_catalog.round((v_exit->>'price')::numeric - (v_entry->>'price')::numeric, 8);
        v_reference := pg_catalog.jsonb_set(v_reference, '{premium_points}', pg_catalog.to_jsonb(v_points), true);
    ELSE
        v_reference := pg_catalog.jsonb_set(v_reference, '{premium_points}', 'null'::jsonb, true);
    END IF;

    v_events := pg_catalog.jsonb_set(v_events, ARRAY[p_event_id], p_event, true);
    v_reference := pg_catalog.jsonb_set(v_reference, '{applied_events}', v_events, true);
    v_reference := pg_catalog.jsonb_set(v_reference, '{revision}', pg_catalog.to_jsonb(v_revision + 1), true);
    v_context := pg_catalog.jsonb_set(v_context, '{option_reference}', v_reference, true);

    UPDATE public.trade_analytics
    SET market_context = v_context,
        option_reference_pnl_points = v_points
    WHERE id = p_trade_id;

    RETURN pg_catalog.jsonb_build_object(
        'success', true, 'trade_id', p_trade_id, 'revision', v_revision + 1,
        'option_reference', v_reference, 'option_reference_pnl_points', v_points);
END;
$$;

GRANT SELECT, UPDATE ON TABLE public.trade_analytics TO service_role;

REVOKE ALL ON FUNCTION public.apply_option_reference_capture(uuid, integer, text, jsonb)
    FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.apply_option_reference_capture(uuid, integer, text, jsonb)
    TO service_role;

CREATE OR REPLACE FUNCTION public.list_incomplete_option_reference(
    p_limit integer DEFAULT 100,
    p_offset integer DEFAULT 0
) RETURNS jsonb
LANGUAGE plpgsql SECURITY INVOKER SET search_path = '' AS $$
DECLARE
    v_rows jsonb;
BEGIN
    IF p_limit IS NULL OR p_limit < 1 OR p_limit > 500
       OR p_offset IS NULL OR p_offset < 0 THEN
        RAISE EXCEPTION 'Recovery page requires limit 1..500 and non-negative offset';
    END IF;
    SELECT COALESCE(pg_catalog.jsonb_agg(page.record ORDER BY page.exit_timestamp NULLS FIRST, page.id), '[]'::jsonb)
    INTO v_rows
    FROM (
        SELECT t.id, t.exit_timestamp,
            pg_catalog.jsonb_build_object(
                'id', t.id,
                'entry_timestamp', t.entry_timestamp,
                'exit_timestamp', t.exit_timestamp,
                'score', t.score,
                'result_state', t.result_state,
                'market_context', pg_catalog.jsonb_build_object(
                    'option_reference', t.market_context->'option_reference',
                    'target_milestones', t.market_context->'target_milestones',
                    'option_reference_milestones', t.market_context->'option_reference_milestones')) AS record
        FROM public.trade_analytics AS t
        WHERE t.market_context->'option_reference'->>'formula_version' = 'one_lot_hold_to_exit_v1'
          AND (
              pg_catalog.jsonb_typeof(t.market_context #> '{option_reference,entry}') IS DISTINCT FROM 'object'
              OR (t.exit_timestamp IS NOT NULL AND (
                  pg_catalog.jsonb_typeof(t.market_context #> '{option_reference,exit}') IS DISTINCT FROM 'object'
                  OR t.option_reference_pnl_points IS NULL))
              OR EXISTS (
                  SELECT 1
                  FROM (VALUES ('T1'), ('T2')) AS milestone(name)
                  WHERE pg_catalog.jsonb_typeof(
                            t.market_context #> ARRAY['option_reference_milestones', milestone.name]
                        ) = 'object'
                    AND pg_catalog.jsonb_typeof(
                            t.market_context #> ARRAY['option_reference', 'milestones', milestone.name]
                        ) IS DISTINCT FROM 'object'
              )
          )
        ORDER BY t.exit_timestamp NULLS FIRST, t.id
        LIMIT p_limit OFFSET p_offset
    ) AS page;
    RETURN v_rows;
END;
$$;

REVOKE ALL ON FUNCTION public.list_incomplete_option_reference(integer, integer)
    FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.list_incomplete_option_reference(integer, integer)
    TO service_role;

CREATE OR REPLACE FUNCTION public.option_reference_capture_capabilities()
RETURNS jsonb
LANGUAGE sql SECURITY INVOKER SET search_path = '' AS $$
    SELECT pg_catalog.jsonb_build_object(
        'schema_version', 'one_lot_hold_to_exit_v1',
        'ready',
        EXISTS (SELECT 1 FROM pg_catalog.pg_attribute
                WHERE attrelid = pg_catalog.to_regclass('public.trade_analytics')
                  AND attname = 'option_reference_pnl_points' AND NOT attisdropped)
        AND pg_catalog.to_regprocedure('public.apply_option_reference_capture(uuid,integer,text,jsonb)') IS NOT NULL
        AND pg_catalog.to_regprocedure('public.list_incomplete_option_reference(integer,integer)') IS NOT NULL
        AND pg_catalog.has_table_privilege(CURRENT_USER, 'public.trade_analytics', 'SELECT')
        AND pg_catalog.has_table_privilege(CURRENT_USER, 'public.trade_analytics', 'UPDATE')
        AND pg_catalog.has_function_privilege(
            CURRENT_USER, pg_catalog.to_regprocedure('public.apply_option_reference_capture(uuid,integer,text,jsonb)'), 'EXECUTE')
        AND pg_catalog.has_function_privilege(
            CURRENT_USER, pg_catalog.to_regprocedure('public.list_incomplete_option_reference(integer,integer)'), 'EXECUTE')
    );
$$;

REVOKE ALL ON FUNCTION public.option_reference_capture_capabilities()
    FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.option_reference_capture_capabilities()
    TO service_role;
