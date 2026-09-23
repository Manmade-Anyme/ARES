-- MANM-152: Exit Timestamp Validation and Flagging

BEGIN;

-- 1. Add time_metrics_excluded to trade_analytics
ALTER TABLE trade_analytics
ADD COLUMN IF NOT EXISTS time_metrics_excluded boolean NOT NULL DEFAULT false;

-- 2. Add entry_timestamp and time_metrics_excluded to active_trades
ALTER TABLE active_trades
ADD COLUMN IF NOT EXISTS entry_timestamp timestamptz,
ADD COLUMN IF NOT EXISTS exit_timestamp timestamptz,
ADD COLUMN IF NOT EXISTS exit_price numeric,
ADD COLUMN IF NOT EXISTS exit_type text,
ADD COLUMN IF NOT EXISTS time_metrics_excluded boolean NOT NULL DEFAULT false;

-- 3. Backfill active_trades from trade_analytics
UPDATE active_trades AS active
SET entry_timestamp = coalesce(active.entry_timestamp, analytics.entry_timestamp),
    exit_timestamp = coalesce(active.exit_timestamp, analytics.exit_timestamp),
    exit_price = coalesce(active.exit_price, analytics.exit_price),
    exit_type = coalesce(active.exit_type, analytics.result_state)
FROM trade_analytics AS analytics
WHERE active.id = analytics.id;

UPDATE active_trades
SET entry_timestamp = created_at
WHERE entry_timestamp IS NULL;

-- 4. Flag and isolate unrecoverable trades
UPDATE trade_analytics
SET time_metrics_excluded = true,
    market_context = jsonb_set(
        coalesce(market_context, '{}'::jsonb),
        '{anomaly}',
        jsonb_build_object(
            'flag', CASE WHEN exit_timestamp IS NULL THEN 'MISSING_EXIT_TIMESTAMP' ELSE 'INVALID_NEGATIVE_DURATION' END,
            'reason', CASE WHEN exit_timestamp IS NULL THEN 'Terminal trade has no exit timestamp' ELSE 'Exit timestamp precedes entry timestamp' END,
            'investigation', 'MANM-152'
        )
    )
WHERE (exit_timestamp IS NULL AND result_state != 'OPEN')
   OR (exit_timestamp IS NOT NULL AND exit_timestamp < entry_timestamp);

UPDATE active_trades
SET time_metrics_excluded = true
WHERE (exit_timestamp IS NULL AND state IN ('CLOSED', 'STOPPED_OUT'))
   OR (exit_timestamp IS NOT NULL AND exit_timestamp < entry_timestamp);

-- 5. Keep both schema-mode entry RPCs compatible with the required column.
-- Only the function installed for the deployment's current schema mode is
-- replaced; dynamic SQL prevents PostgreSQL from parsing columns belonging to
-- the other mode.
DO $migration$
BEGIN
  IF to_regprocedure('create_trade_entry_bridge(uuid,uuid,text,text,text,numeric,numeric,numeric,numeric,text,jsonb,jsonb,timestamptz,bigint)') IS NOT NULL THEN
    EXECUTE $function$
      CREATE OR REPLACE FUNCTION create_trade_entry_bridge(
        p_trade_id uuid, p_signal_uuid uuid, p_display_id text,
        p_setup_type text, p_direction text, p_entry_price numeric,
        p_stop_loss numeric, p_target_1 numeric, p_target_2 numeric,
        p_added_time_ist text, p_market_context jsonb, p_oi_data jsonb,
        p_entry_timestamp timestamptz, p_legacy_signal_id bigint
      ) RETURNS uuid LANGUAGE plpgsql AS $body$
      BEGIN
        INSERT INTO active_trades (
          id, signal_id, signal_uuid, display_id, setup_type, direction,
          entry_timestamp, entry_price, stop_loss, target_1, target_2, state,
          added_time_ist
        ) VALUES (
          p_trade_id, p_legacy_signal_id::text, p_signal_uuid, p_display_id,
          p_setup_type, p_direction, p_entry_timestamp, p_entry_price,
          p_stop_loss, p_target_1, p_target_2, 'OPEN', p_added_time_ist
        ) ON CONFLICT (id) DO NOTHING;

        INSERT INTO trade_analytics (
          id, signal_id, signal_uuid, setup_type, direction, entry_price,
          market_context, oi_data, entry_timestamp, result_state
        ) VALUES (
          p_trade_id, p_legacy_signal_id, p_signal_uuid, p_setup_type,
          p_direction, p_entry_price, p_market_context, p_oi_data,
          p_entry_timestamp, 'OPEN'
        ) ON CONFLICT (id) DO NOTHING;

        IF NOT EXISTS (
          SELECT 1 FROM active_trades
          WHERE id = p_trade_id
            AND signal_id IS NOT DISTINCT FROM p_legacy_signal_id::text
            AND signal_uuid IS NOT DISTINCT FROM p_signal_uuid
            AND display_id IS NOT DISTINCT FROM p_display_id
            AND setup_type IS NOT DISTINCT FROM p_setup_type
            AND direction IS NOT DISTINCT FROM p_direction
            AND entry_timestamp IS NOT DISTINCT FROM p_entry_timestamp
            AND entry_price IS NOT DISTINCT FROM p_entry_price
            AND stop_loss IS NOT DISTINCT FROM p_stop_loss
            AND target_1 IS NOT DISTINCT FROM p_target_1
            AND target_2 IS NOT DISTINCT FROM p_target_2
        ) OR NOT EXISTS (
          SELECT 1 FROM trade_analytics
          WHERE id = p_trade_id
            AND signal_id IS NOT DISTINCT FROM p_legacy_signal_id::text
            AND signal_uuid IS NOT DISTINCT FROM p_signal_uuid
            AND setup_type IS NOT DISTINCT FROM p_setup_type
            AND direction IS NOT DISTINCT FROM p_direction
            AND entry_price IS NOT DISTINCT FROM p_entry_price
            AND market_context IS NOT DISTINCT FROM p_market_context
            AND oi_data IS NOT DISTINCT FROM p_oi_data
            AND entry_timestamp IS NOT DISTINCT FROM p_entry_timestamp
        ) THEN
          RAISE EXCEPTION 'Conflicting trade entry for id %', p_trade_id;
        END IF;
        RETURN p_trade_id;
      END;
      $body$
    $function$;
  END IF;

  IF to_regprocedure('create_trade_entry_greenfield(uuid,uuid,text,text,text,numeric,numeric,numeric,numeric,text,jsonb,jsonb,timestamptz)') IS NOT NULL THEN
    EXECUTE $function$
      CREATE OR REPLACE FUNCTION create_trade_entry_greenfield(
        p_trade_id uuid, p_signal_uuid uuid, p_display_id text,
        p_setup_type text, p_direction text, p_entry_price numeric,
        p_stop_loss numeric, p_target_1 numeric, p_target_2 numeric,
        p_added_time_ist text, p_market_context jsonb, p_oi_data jsonb,
        p_entry_timestamp timestamptz
      ) RETURNS uuid LANGUAGE plpgsql AS $body$
      BEGIN
        INSERT INTO active_trades (
          id, signal_id, display_id, setup_type, direction, entry_timestamp,
          entry_price, stop_loss, target_1, target_2, state, added_time_ist
        ) VALUES (
          p_trade_id, p_signal_uuid, p_display_id, p_setup_type, p_direction,
          p_entry_timestamp, p_entry_price, p_stop_loss, p_target_1,
          p_target_2, 'OPEN', p_added_time_ist
        ) ON CONFLICT (id) DO NOTHING;

        INSERT INTO trade_analytics (
          id, signal_id, setup_type, direction, entry_price, market_context,
          oi_data, entry_timestamp, result_state
        ) VALUES (
          p_trade_id, p_signal_uuid, p_setup_type, p_direction, p_entry_price,
          p_market_context, p_oi_data, p_entry_timestamp, 'OPEN'
        ) ON CONFLICT (id) DO NOTHING;

        IF NOT EXISTS (
          SELECT 1 FROM active_trades
          WHERE id = p_trade_id
            AND signal_id IS NOT DISTINCT FROM p_signal_uuid
            AND display_id IS NOT DISTINCT FROM p_display_id
            AND setup_type IS NOT DISTINCT FROM p_setup_type
            AND direction IS NOT DISTINCT FROM p_direction
            AND entry_timestamp IS NOT DISTINCT FROM p_entry_timestamp
            AND entry_price IS NOT DISTINCT FROM p_entry_price
            AND stop_loss IS NOT DISTINCT FROM p_stop_loss
            AND target_1 IS NOT DISTINCT FROM p_target_1
            AND target_2 IS NOT DISTINCT FROM p_target_2
        ) OR NOT EXISTS (
          SELECT 1 FROM trade_analytics
          WHERE id = p_trade_id
            AND signal_id IS NOT DISTINCT FROM p_signal_uuid
            AND setup_type IS NOT DISTINCT FROM p_setup_type
            AND direction IS NOT DISTINCT FROM p_direction
            AND entry_price IS NOT DISTINCT FROM p_entry_price
            AND market_context IS NOT DISTINCT FROM p_market_context
            AND oi_data IS NOT DISTINCT FROM p_oi_data
            AND entry_timestamp IS NOT DISTINCT FROM p_entry_timestamp
        ) THEN
          RAISE EXCEPTION 'Conflicting trade entry for id %', p_trade_id;
        END IF;
        RETURN p_trade_id;
      END;
      $body$
    $function$;
  END IF;
END;
$migration$;

-- 6. Replace and validate constraints so a repaired migration is rerunnable.
ALTER TABLE trade_analytics DROP CONSTRAINT IF EXISTS chk_trade_analytics_exit_chronology;
ALTER TABLE trade_analytics
ADD CONSTRAINT chk_trade_analytics_exit_chronology
CHECK (time_metrics_excluded OR exit_timestamp IS NULL OR exit_timestamp >= entry_timestamp);

ALTER TABLE trade_analytics DROP CONSTRAINT IF EXISTS chk_trade_analytics_closed_requires_exit;
ALTER TABLE trade_analytics
ADD CONSTRAINT chk_trade_analytics_closed_requires_exit
CHECK (result_state = 'OPEN' OR exit_timestamp IS NOT NULL OR time_metrics_excluded = true);

ALTER TABLE active_trades
ALTER COLUMN entry_timestamp SET NOT NULL;

ALTER TABLE active_trades DROP CONSTRAINT IF EXISTS chk_active_trades_exit_chronology;

ALTER TABLE active_trades
ADD CONSTRAINT chk_active_trades_exit_chronology
CHECK (time_metrics_excluded OR exit_timestamp IS NULL OR exit_timestamp >= entry_timestamp);

ALTER TABLE active_trades DROP CONSTRAINT IF EXISTS chk_active_trades_closed_requires_exit;
ALTER TABLE active_trades
ADD CONSTRAINT chk_active_trades_closed_requires_exit
CHECK (state NOT IN ('CLOSED', 'STOPPED_OUT') OR exit_timestamp IS NOT NULL OR time_metrics_excluded = true);


COMMIT;
