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

-- One-time, exact-cohort recovery for 26–30 Oct 2026. This is deliberately
-- separate from normal enrollment/capture and cannot enroll arbitrary history.
CREATE OR REPLACE FUNCTION public.apply_option_reference_weekly_backfill(p_rows jsonb)
RETURNS jsonb
LANGUAGE plpgsql SECURITY INVOKER SET search_path = '' AS $$
DECLARE
    v_input jsonb;
    v_id uuid;
    v_row public.trade_analytics%ROWTYPE;
    v_context jsonb;
    v_reference jsonb;
    v_options jsonb;
    v_contract jsonb;
    v_entry jsonb;
    v_exit jsonb;
    v_evidence jsonb;
    v_provenance jsonb;
    v_event_at timestamptz;
    v_entry_at timestamptz;
    v_exit_at timestamptz;
    v_bar_at timestamptz;
    v_sampled_at timestamptz;
    v_price numeric;
    v_open numeric;
    v_high numeric;
    v_low numeric;
    v_close numeric;
    v_volume numeric;
    v_side text;
    v_provider text;
    v_event jsonb;
    v_result jsonb;
    v_all_idempotent boolean := true;
    v_input_ids uuid[];
    v_eligible_ids uuid[];
BEGIN
    IF pg_catalog.jsonb_typeof(p_rows) IS DISTINCT FROM 'array'
       OR pg_catalog.jsonb_array_length(p_rows) <> 16 THEN
        RAISE EXCEPTION 'Weekly option-reference backfill requires exactly 16 rows';
    END IF;

    IF EXISTS (
        SELECT 1
        FROM pg_catalog.jsonb_array_elements(p_rows) AS item(value)
        WHERE pg_catalog.jsonb_typeof(item.value) IS DISTINCT FROM 'object'
           OR pg_catalog.jsonb_typeof(item.value->'id') IS DISTINCT FROM 'string'
    ) THEN
        RAISE EXCEPTION 'Every weekly backfill row requires an object and trade id';
    END IF;

    SELECT pg_catalog.array_agg(parsed.id ORDER BY parsed.id)
    INTO v_input_ids
    FROM (
        SELECT (item.value->>'id')::uuid AS id
        FROM pg_catalog.jsonb_array_elements(p_rows) AS item(value)
    ) AS parsed;
    IF pg_catalog.cardinality(v_input_ids) <> 16
       OR (SELECT pg_catalog.count(DISTINCT item.value->>'id')
           FROM pg_catalog.jsonb_array_elements(p_rows) AS item(value)) <> 16 THEN
        RAISE EXCEPTION 'Weekly backfill trade ids must be unique';
    END IF;

    -- Lock exactly the rows the report counts, in deterministic order.
    PERFORM t.id
    FROM public.trade_analytics AS t
    WHERE t.exit_timestamp >= TIMESTAMPTZ '2026-10-26 00:00:00+05:30'
      AND t.exit_timestamp < TIMESTAMPTZ '2026-10-31 00:00:00+05:30'
      AND t.time_metrics_excluded = false
      AND t.pnl_points IS NOT NULL
      AND (t.entry_timestamp IS NULL OR t.exit_timestamp >= t.entry_timestamp)
    ORDER BY t.id
    FOR UPDATE;

    SELECT pg_catalog.array_agg(t.id ORDER BY t.id)
    INTO v_eligible_ids
    FROM public.trade_analytics AS t
    WHERE t.exit_timestamp >= TIMESTAMPTZ '2026-10-26 00:00:00+05:30'
      AND t.exit_timestamp < TIMESTAMPTZ '2026-10-31 00:00:00+05:30'
      AND t.time_metrics_excluded = false
      AND t.pnl_points IS NOT NULL
      AND (t.entry_timestamp IS NULL OR t.exit_timestamp >= t.entry_timestamp);
    IF pg_catalog.cardinality(v_eligible_ids) IS DISTINCT FROM 16 THEN
        RAISE EXCEPTION 'Expected exactly 16 report-eligible closed trades for 26–30 Oct 2026; found %',
            COALESCE(pg_catalog.cardinality(v_eligible_ids), 0);
    END IF;
    IF v_input_ids IS DISTINCT FROM v_eligible_ids THEN
        RAISE EXCEPTION 'Submitted trade ids must exactly match the report-eligible 26–30 Oct 2026 cohort';
    END IF;

    -- Validate every candidate against canonical rows before writing any row.
    FOR v_input IN SELECT item.value FROM pg_catalog.jsonb_array_elements(p_rows) AS item(value)
    LOOP
        v_id := (v_input->>'id')::uuid;
        SELECT * INTO STRICT v_row FROM public.trade_analytics WHERE id = v_id;
        IF v_row.entry_timestamp IS NULL OR v_row.exit_timestamp IS NULL
           OR v_row.result_state IS NULL OR v_row.result_state = 'OPEN'
           OR v_row.exit_timestamp < v_row.entry_timestamp THEN
            RAISE EXCEPTION 'Trade % is not a chronologically valid closed trade', v_id;
        END IF;
        IF pg_catalog.jsonb_typeof(v_input->'entry_event_at') IS DISTINCT FROM 'string'
           OR pg_catalog.jsonb_typeof(v_input->'exit_event_at') IS DISTINCT FROM 'string'
           OR (v_input->>'entry_event_at')::timestamptz IS DISTINCT FROM v_row.entry_timestamp
           OR (v_input->>'exit_event_at')::timestamptz IS DISTINCT FROM v_row.exit_timestamp
           OR v_input->>'exit_reason' IS DISTINCT FROM v_row.result_state THEN
            RAISE EXCEPTION 'Trade % event times or terminal state do not match Supabase', v_id;
        END IF;

        v_options := v_row.market_context->'options_sizing';
        IF pg_catalog.jsonb_typeof(v_options) IS DISTINCT FROM 'object'
           OR COALESCE(v_options->>'security_id', '') !~ '^[0-9]+$'
           OR COALESCE(v_options->>'expiry_date', '') !~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}$'
           OR COALESCE(v_options->>'expiry_date', '') NOT IN ('2026-10-27', '2026-11-03')
           OR COALESCE(v_options->>'option_type', '') NOT IN ('CE', 'PE')
           OR pg_catalog.jsonb_typeof(v_options->'strike') IS DISTINCT FROM 'number'
           OR pg_catalog.jsonb_typeof(v_options->'lot_size') IS DISTINCT FROM 'number'
           OR (v_options->>'strike')::numeric <= 0
           OR (v_options->>'lot_size')::numeric <= 0
           OR (v_options->>'strike')::numeric::text IN ('NaN', 'Infinity', '-Infinity')
           OR (v_options->>'lot_size')::numeric::text IN ('NaN', 'Infinity', '-Infinity')
           OR (v_options->>'lot_size')::numeric <> pg_catalog.trunc((v_options->>'lot_size')::numeric)
           OR (v_options->>'expiry_date')::date < (v_row.exit_timestamp AT TIME ZONE 'Asia/Kolkata')::date THEN
            RAISE EXCEPTION 'Trade % has no valid pinned options_sizing contract', v_id;
        END IF;
        v_contract := pg_catalog.jsonb_build_object(
            'security_id', v_options->>'security_id',
            'exchange_segment', 'NSE_FNO',
            'expiry_date', v_options->>'expiry_date',
            'strike', (v_options->>'strike')::numeric,
            'option_type', v_options->>'option_type',
            'lot_size', (v_options->>'lot_size')::numeric);
        IF pg_catalog.jsonb_typeof(v_input->'contract') IS DISTINCT FROM 'object'
           OR (v_input->'contract'->>'security_id') IS DISTINCT FROM v_contract->>'security_id'
           OR (v_input->'contract'->>'exchange_segment') IS DISTINCT FROM 'NSE_FNO'
           OR (v_input->'contract'->>'expiry_date') IS DISTINCT FROM v_contract->>'expiry_date'
           OR (v_input->'contract'->>'option_type') IS DISTINCT FROM v_contract->>'option_type'
           OR (v_input->'contract'->>'strike')::numeric IS DISTINCT FROM (v_contract->>'strike')::numeric
           OR (v_input->'contract'->>'lot_size')::numeric IS DISTINCT FROM (v_contract->>'lot_size')::numeric THEN
            RAISE EXCEPTION 'Trade % submitted contract does not match its pinned options_sizing contract', v_id;
        END IF;

        FOR v_side, v_event_at, v_evidence IN
            SELECT 'entry', v_row.entry_timestamp, v_input->'entry'
            UNION ALL
            SELECT 'exit', v_row.exit_timestamp, v_input->'exit'
        LOOP
            IF pg_catalog.jsonb_typeof(v_evidence) IS DISTINCT FROM 'object'
               OR pg_catalog.jsonb_typeof(v_evidence->'price') IS DISTINCT FROM 'number'
               OR pg_catalog.jsonb_typeof(v_evidence->'observed_at') IS DISTINCT FROM 'string'
               OR v_evidence->>'source' IS DISTINCT FROM 'completed_1m_close'
               OR pg_catalog.jsonb_typeof(v_evidence->'approximate') IS DISTINCT FROM 'boolean'
               OR (v_evidence->>'approximate')::boolean IS DISTINCT FROM true
               OR pg_catalog.jsonb_typeof(v_evidence->'provenance') IS DISTINCT FROM 'object' THEN
                RAISE EXCEPTION 'Trade % % requires approximate completed-minute evidence', v_id, v_side;
            END IF;
            v_provenance := v_evidence->'provenance';
            IF pg_catalog.jsonb_typeof(v_provenance) IS DISTINCT FROM 'object'
               OR pg_catalog.jsonb_typeof(v_provenance->'provider') IS DISTINCT FROM 'string'
               OR pg_catalog.jsonb_typeof(v_provenance->'bar_start') IS DISTINCT FROM 'string'
               OR COALESCE(v_provenance->>'bar_start', '') = ''
               OR pg_catalog.jsonb_typeof(v_provenance->'sampled_at') IS DISTINCT FROM 'string'
               OR COALESCE(v_provenance->>'sampled_at', '') = ''
               OR pg_catalog.jsonb_typeof(v_provenance->'complete') IS DISTINCT FROM 'boolean'
               OR v_provenance->'complete' IS DISTINCT FROM 'true'::jsonb
               OR pg_catalog.jsonb_typeof(v_provenance->'requested_exchange_segment') IS DISTINCT FROM 'string'
               OR pg_catalog.jsonb_typeof(v_provenance->'requested_expiry_date') IS DISTINCT FROM 'string'
               OR pg_catalog.jsonb_typeof(v_provenance->'requested_strike') IS DISTINCT FROM 'number'
               OR pg_catalog.jsonb_typeof(v_provenance->'requested_option_type') IS DISTINCT FROM 'string'
               OR pg_catalog.jsonb_typeof(v_provenance->'requested_lot_size') IS DISTINCT FROM 'number'
               OR pg_catalog.jsonb_typeof(v_provenance->'open') IS DISTINCT FROM 'number'
               OR pg_catalog.jsonb_typeof(v_provenance->'high') IS DISTINCT FROM 'number'
               OR pg_catalog.jsonb_typeof(v_provenance->'low') IS DISTINCT FROM 'number'
               OR pg_catalog.jsonb_typeof(v_provenance->'close') IS DISTINCT FROM 'number'
               OR pg_catalog.jsonb_typeof(v_provenance->'volume') IS DISTINCT FROM 'number' THEN
                RAISE EXCEPTION 'Trade % % has missing or malformed timestamp, request, or OHLCV provenance', v_id, v_side;
            END IF;
            IF v_provenance->>'provider' = 'dhan_intraday_minute'
               AND pg_catalog.jsonb_typeof(v_provenance->'requested_security_id') IS DISTINCT FROM 'string' THEN
                RAISE EXCEPTION 'Trade % % intraday provenance requires a requested security id', v_id, v_side;
            ELSIF v_provenance->>'provider' = 'dhan_expired_rolling_options'
               AND (pg_catalog.jsonb_typeof(v_provenance->'underlying_security_id') IS DISTINCT FROM 'string'
                    OR pg_catalog.jsonb_typeof(v_provenance->'expiry_flag') IS DISTINCT FROM 'string'
                    OR pg_catalog.jsonb_typeof(v_provenance->'expiry_code') IS DISTINCT FROM 'number'
                    OR pg_catalog.jsonb_typeof(v_provenance->'returned_strike') IS DISTINCT FROM 'number'
                    OR pg_catalog.jsonb_typeof(v_provenance->'moneyness') IS DISTINCT FROM 'string') THEN
                RAISE EXCEPTION 'Trade % % expired rolling provenance is missing expiry or returned-strike fields', v_id, v_side;
            END IF;
            v_provider := v_provenance->>'provider';
            v_bar_at := (v_provenance->>'bar_start')::timestamptz;
            v_sampled_at := (v_provenance->>'sampled_at')::timestamptz;
            v_price := (v_evidence->>'price')::numeric;
            v_open := (v_provenance->>'open')::numeric;
            v_high := (v_provenance->>'high')::numeric;
            v_low := (v_provenance->>'low')::numeric;
            v_close := (v_provenance->>'close')::numeric;
            v_volume := (v_provenance->>'volume')::numeric;
            IF NOT pg_catalog.isfinite(v_event_at)
               OR v_event_at < TIMESTAMPTZ '2026-10-26 00:00:00+05:30'
               OR v_event_at >= TIMESTAMPTZ '2026-10-31 00:00:00+05:30'
               OR (v_event_at AT TIME ZONE 'Asia/Kolkata')::time < TIME '09:15'
               OR (v_event_at AT TIME ZONE 'Asia/Kolkata')::time >= TIME '15:30'
               OR v_bar_at <> pg_catalog.date_trunc('minute', v_event_at)
               OR (v_evidence->>'observed_at')::timestamptz <> v_bar_at + INTERVAL '1 minute'
               OR v_sampled_at < v_bar_at + INTERVAL '1 minute'
               OR v_sampled_at > pg_catalog.clock_timestamp()
               OR v_provenance->'complete' IS DISTINCT FROM 'true'::jsonb
               OR v_provenance->>'requested_exchange_segment' IS DISTINCT FROM 'NSE_FNO'
               OR v_provenance->>'requested_expiry_date' IS DISTINCT FROM v_contract->>'expiry_date'
               OR v_provenance->>'requested_option_type' IS DISTINCT FROM v_contract->>'option_type'
               OR (v_provenance->>'requested_strike')::numeric IS DISTINCT FROM (v_contract->>'strike')::numeric
               OR (v_provenance->>'requested_lot_size')::numeric IS DISTINCT FROM (v_contract->>'lot_size')::numeric
               OR v_price <= 0 OR v_price::text IN ('NaN', 'Infinity', '-Infinity')
               OR v_open <= 0 OR v_high <= 0 OR v_low <= 0 OR v_close <= 0
               OR v_open::text IN ('NaN', 'Infinity', '-Infinity')
               OR v_high::text IN ('NaN', 'Infinity', '-Infinity')
               OR v_low::text IN ('NaN', 'Infinity', '-Infinity')
               OR v_close::text IN ('NaN', 'Infinity', '-Infinity')
               OR v_volume < 0 OR v_volume::text IN ('NaN', 'Infinity', '-Infinity')
               OR v_close IS DISTINCT FROM v_price
               OR v_high < GREATEST(v_open, v_low, v_close)
               OR v_low > LEAST(v_open, v_high, v_close) THEN
                RAISE EXCEPTION 'Trade % % has invalid completed-minute provenance or OHLCV', v_id, v_side;
            END IF;
            IF v_provider = 'dhan_intraday_minute' THEN
                IF v_provenance->>'requested_security_id' IS DISTINCT FROM v_contract->>'security_id' THEN
                    RAISE EXCEPTION 'Trade % % intraday security id does not match its contract pin', v_id, v_side;
                END IF;
            ELSIF v_provider = 'dhan_expired_rolling_options' THEN
                IF v_provenance->>'underlying_security_id' IS DISTINCT FROM '13'
                   OR v_provenance->>'expiry_flag' IS DISTINCT FROM 'WEEK'
                   OR (v_contract->>'expiry_date' = '2026-10-27'
                       AND v_provenance->>'expiry_code' IS DISTINCT FROM '0')
                   OR (v_contract->>'expiry_date' <> '2026-10-27'
                       AND v_provenance->>'expiry_code' IS DISTINCT FROM '1')
                   OR (v_provenance->>'returned_strike')::numeric IS DISTINCT FROM (v_contract->>'strike')::numeric
                   OR COALESCE(v_provenance->>'moneyness', '') !~ '^ATM([+-][0-9]+)?$' THEN
                    RAISE EXCEPTION 'Trade % % expired rolling bar returned strike/provenance does not match the pinned weekly strike', v_id, v_side;
                END IF;
            ELSE
                RAISE EXCEPTION 'Trade % % has unsupported Dhan provider provenance', v_id, v_side;
            END IF;
        END LOOP;
        IF (v_input->>'entry_event_at')::timestamptz > (v_input->>'exit_event_at')::timestamptz
           OR (v_input->'entry'->>'observed_at')::timestamptz > (v_input->'exit'->>'observed_at')::timestamptz THEN
            RAISE EXCEPTION 'Trade % option entry evidence must precede final exit evidence', v_id;
        END IF;

        v_context := COALESCE(v_row.market_context, '{}'::jsonb);
        v_reference := v_context->'option_reference';
        IF v_reference IS NOT NULL AND v_reference <> 'null'::jsonb THEN
            IF pg_catalog.jsonb_typeof(v_reference) IS DISTINCT FROM 'object'
               OR v_reference->>'formula_version' IS DISTINCT FROM 'one_lot_hold_to_exit_v1'
               OR v_reference->'contract' IS DISTINCT FROM v_contract
               OR (v_reference->>'entry_event_at')::timestamptz IS DISTINCT FROM v_row.entry_timestamp THEN
                RAISE EXCEPTION 'Trade % already has a conflicting option reference', v_id;
            END IF;
        END IF;
    END LOOP;

    -- All checks passed. The function call is one transaction; any downstream
    -- conflict rolls every row back. Existing capture RPCs retain their normal
    -- enrolled-row-only boundary and validate each immutable event again.
    FOR v_input IN SELECT item.value FROM pg_catalog.jsonb_array_elements(p_rows) AS item(value)
                   ORDER BY item.value->>'id'
    LOOP
        v_id := (v_input->>'id')::uuid;
        SELECT * INTO STRICT v_row FROM public.trade_analytics WHERE id = v_id;
        v_context := COALESCE(v_row.market_context, '{}'::jsonb);
        v_reference := v_context->'option_reference';
        v_contract := pg_catalog.jsonb_build_object(
            'security_id', v_row.market_context #>> '{options_sizing,security_id}',
            'exchange_segment', 'NSE_FNO',
            'expiry_date', v_row.market_context #>> '{options_sizing,expiry_date}',
            'strike', (v_row.market_context #>> '{options_sizing,strike}')::numeric,
            'option_type', v_row.market_context #>> '{options_sizing,option_type}',
            'lot_size', (v_row.market_context #>> '{options_sizing,lot_size}')::numeric);
        IF v_reference IS NULL OR v_reference = 'null'::jsonb THEN
            v_reference := pg_catalog.jsonb_build_object(
                'formula_version', 'one_lot_hold_to_exit_v1', 'revision', 1,
                'status', 'TRACKING', 'reference_lots', 1, 'contract', v_contract,
                'entry_event_at', v_row.entry_timestamp, 'milestones', '{}'::jsonb,
                'applied_events', '{}'::jsonb);
            v_context := pg_catalog.jsonb_set(v_context, '{option_reference}', v_reference, true);
            UPDATE public.trade_analytics SET market_context = v_context WHERE id = v_id;
        END IF;

        v_result := public.apply_option_reference_capture(
            v_id, COALESCE((v_reference->>'revision')::integer, 1),
            'weekly-backfill-20261030-entry',
            pg_catalog.jsonb_build_object('kind', 'ENTRY_CAPTURE',
                'event_at', v_row.entry_timestamp, 'evidence', v_input->'entry'));
        IF COALESCE((v_result->>'idempotent_skip')::boolean, false) IS false THEN
            v_all_idempotent := false;
        END IF;
        v_reference := v_result->'option_reference';
        v_result := public.apply_option_reference_capture(
            v_id, (v_reference->>'revision')::integer,
            'weekly-backfill-20261030-exit-marker',
            pg_catalog.jsonb_build_object('kind', 'EXIT_MARKER',
                'event_at', v_row.exit_timestamp, 'exit_reason', v_row.result_state));
        IF COALESCE((v_result->>'idempotent_skip')::boolean, false) IS false THEN
            v_all_idempotent := false;
        END IF;
        v_reference := v_result->'option_reference';
        v_result := public.apply_option_reference_capture(
            v_id, (v_reference->>'revision')::integer,
            'weekly-backfill-20261030-exit',
            pg_catalog.jsonb_build_object('kind', 'EXIT_CAPTURE',
                'event_at', v_row.exit_timestamp, 'exit_reason', v_row.result_state,
                'evidence', v_input->'exit'));
        IF COALESCE((v_result->>'idempotent_skip')::boolean, false) IS false THEN
            v_all_idempotent := false;
        END IF;
    END LOOP;

    RETURN pg_catalog.jsonb_build_object(
        'success', true, 'backfilled', 16, 'idempotent', v_all_idempotent);
END;
$$;

REVOKE ALL ON FUNCTION public.apply_option_reference_weekly_backfill(jsonb)
    FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.apply_option_reference_weekly_backfill(jsonb)
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
        AND pg_catalog.to_regprocedure('public.apply_option_reference_weekly_backfill(jsonb)') IS NOT NULL
        AND pg_catalog.to_regprocedure('public.list_incomplete_option_reference(integer,integer)') IS NOT NULL
        AND pg_catalog.has_table_privilege(CURRENT_USER, 'public.trade_analytics', 'SELECT')
        AND pg_catalog.has_table_privilege(CURRENT_USER, 'public.trade_analytics', 'UPDATE')
        AND pg_catalog.has_function_privilege(
            CURRENT_USER, pg_catalog.to_regprocedure('public.apply_option_reference_capture(uuid,integer,text,jsonb)'), 'EXECUTE')
        AND pg_catalog.has_function_privilege(
            CURRENT_USER, pg_catalog.to_regprocedure('public.apply_option_reference_weekly_backfill(jsonb)'), 'EXECUTE')
        AND pg_catalog.has_function_privilege(
            CURRENT_USER, pg_catalog.to_regprocedure('public.list_incomplete_option_reference(integer,integer)'), 'EXECUTE')
    );
$$;

REVOKE ALL ON FUNCTION public.option_reference_capture_capabilities()
    FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.option_reference_capture_capabilities()
    TO service_role;
