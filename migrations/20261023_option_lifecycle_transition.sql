-- ====================================================================
-- ARES Migration: Option-Premium ATR Trailing & Milestone Lifecycle
-- Task: TASK-20261023
-- Date: 2026-10-23
-- Description:
--   1. Adds support for active state 'T2_REACHED' in active_trades.
--   2. Provides the transactional RPC apply_option_lifecycle_transition
--      for atomic lifecycle, milestone score, and market_context updates.
--   3. Clarifies schema comments that trade_analytics.score may be 1 (T1)
--      or 2 (T2) while result_state remains 'OPEN'.
-- ====================================================================

-- 1. Ensure active_trades allows T2_REACHED state
DO $$
BEGIN
    -- Update check constraint on active_trades.state if present
    IF EXISTS (
        SELECT 1 FROM pg_constraint 
        WHERE conname = 'chk_active_trades_state'
    ) THEN
        ALTER TABLE active_trades DROP CONSTRAINT chk_active_trades_state;
        ALTER TABLE active_trades ADD CONSTRAINT chk_active_trades_state 
            CHECK (state IN ('OPEN', 'T1_HIT', 'T2_REACHED', 'CLOSED', 'STOPPED_OUT'));
    END IF;
END $$;

-- 2. Schema comments update: Score is recorded at T1 and T2 while trade is OPEN
COMMENT ON COLUMN trade_analytics.score IS 
'Milestone score: NULL before T1, 1 at T1, 2 at T2. For option_atr_v1, recorded score updates while result_state is OPEN and is retained on final closure.';

COMMENT ON COLUMN trade_analytics.result_state IS 
'Lifecycle result: OPEN while any modeled allocation remains. Set to terminal exit reason (SL_HIT, STOPPED_OUT_AT_BE, ATR_SL_HIT, OPTION_SL_FLOOR_HIT) on final closure.';

-- 3. Transactional lifecycle transition procedure
CREATE OR REPLACE FUNCTION apply_option_lifecycle_transition(
    p_trade_id uuid,
    p_expected_revision integer,
    p_event_id text,
    p_transition jsonb
)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
AS $$
DECLARE
    v_active_trade record;
    v_analytics record;
    v_current_context jsonb;
    v_opt_context jsonb;
    v_current_rev integer;
    v_new_rev integer;
    v_new_state text;
    v_new_score integer;
    v_result_state text;
    v_exit_reason text;
    v_remaining_fraction numeric;
    v_alert_events jsonb;
    v_now timestamptz := now();
BEGIN
    -- Lock matching trade rows
    SELECT * INTO v_active_trade 
    FROM active_trades 
    WHERE id = p_trade_id 
    FOR UPDATE;

    SELECT * INTO v_analytics 
    FROM trade_analytics 
    WHERE id = p_trade_id 
    FOR UPDATE;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'Trade % not found in trade_analytics', p_trade_id;
    END IF;

    v_current_context := COALESCE(v_analytics.market_context, '{}'::jsonb);
    v_opt_context := COALESCE(v_current_context->'option_trailing', '{}'::jsonb);
    v_current_rev := COALESCE((v_opt_context->>'revision')::integer, 1);

    -- Check for duplicate event ID (idempotency)
    v_alert_events := COALESCE(v_opt_context->'alert_events', '{}'::jsonb);
    IF p_event_id IS NOT NULL AND v_alert_events ? p_event_id THEN
        RETURN jsonb_build_object(
            'success', true,
            'idempotent_skip', true,
            'trade_id', p_trade_id,
            'revision', v_current_rev,
            'phase', v_opt_context->>'phase'
        );
    END IF;

    -- Concurrency check: Reject stale revisions
    IF p_expected_revision IS NOT NULL AND v_current_rev != p_expected_revision THEN
        RAISE EXCEPTION 'Revision conflict for trade %: expected %, found %', 
            p_trade_id, p_expected_revision, v_current_rev;
    END IF;

    v_new_rev := v_current_rev + 1;
    v_new_state := p_transition->>'active_state';
    v_new_score := (p_transition->>'score')::integer;
    v_remaining_fraction := COALESCE((p_transition->'option_trailing'->>'remaining_fraction')::numeric, 0.0);
    v_exit_reason := p_transition->>'exit_reason';

    -- Merge option_trailing into existing market_context
    v_opt_context := v_opt_context || (p_transition->'option_trailing');
    v_opt_context := jsonb_set(v_opt_context, '{revision}', to_jsonb(v_new_rev));
    
    -- Record event in alert_events
    IF p_event_id IS NOT NULL THEN
        v_alert_events := jsonb_set(
            v_alert_events, 
            ARRAY[p_event_id], 
            jsonb_build_object(
                'recorded_at', v_now,
                'transition', p_transition->>'update_type'
            )
        );
        v_opt_context := jsonb_set(v_opt_context, '{alert_events}', v_alert_events);
    END IF;

    v_current_context := jsonb_set(v_current_context, '{option_trailing}', v_opt_context);

    -- Determine result_state: remains OPEN if any allocation remains; otherwise set terminal reason
    IF v_remaining_fraction > 0.0 THEN
        v_result_state := 'OPEN';
    ELSE
        v_result_state := COALESCE(v_exit_reason, 'CLOSED');
    END IF;

    -- Update active_trades if row exists
    IF v_active_trade.id IS NOT NULL THEN
        UPDATE active_trades
        SET 
            state = v_new_state,
            stop_loss = COALESCE((p_transition->>'stop_loss')::numeric, stop_loss),
            exit_price = CASE WHEN v_remaining_fraction = 0.0 THEN (p_transition->>'exit_price')::numeric ELSE exit_price END,
            exit_type = CASE WHEN v_remaining_fraction = 0.0 THEN v_result_state ELSE exit_type END,
            exit_timestamp = CASE WHEN v_remaining_fraction = 0.0 THEN COALESCE(p_transition->>'exit_timestamp', v_now::text)::timestamptz ELSE exit_timestamp END
        WHERE id = p_trade_id;
    END IF;

    -- Update trade_analytics
    UPDATE trade_analytics
    SET 
        score = COALESCE(v_new_score, score),
        result_state = v_result_state,
        exit_price = CASE WHEN v_remaining_fraction = 0.0 THEN (p_transition->>'exit_price')::numeric ELSE exit_price END,
        exit_timestamp = CASE WHEN v_remaining_fraction = 0.0 THEN COALESCE(p_transition->>'exit_timestamp', v_now::text)::timestamptz ELSE exit_timestamp END,
        pnl_points = CASE WHEN v_remaining_fraction = 0.0 THEN (p_transition->>'pnl_points')::numeric ELSE pnl_points END,
        market_context = v_current_context
    WHERE id = p_trade_id;

    RETURN jsonb_build_object(
        'success', true,
        'trade_id', p_trade_id,
        'revision', v_new_rev,
        'active_state', v_new_state,
        'result_state', v_result_state,
        'score', v_new_score,
        'remaining_fraction', v_remaining_fraction
    );
END;
$$;
