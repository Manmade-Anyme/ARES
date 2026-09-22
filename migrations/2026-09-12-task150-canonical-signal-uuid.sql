-- Bridge mode schema changes for MANM-150

CREATE EXTENSION IF NOT EXISTS "pgcrypto";

ALTER TABLE ares_signals ADD COLUMN IF NOT EXISTS signal_uuid uuid;
UPDATE ares_signals
SET signal_uuid = gen_random_uuid()
WHERE signal_uuid IS NULL;
ALTER TABLE ares_signals
  ALTER COLUMN signal_uuid SET DEFAULT gen_random_uuid(),
  ALTER COLUMN signal_uuid SET NOT NULL;

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_class WHERE relname = 'uq_ares_signals_signal_uuid') THEN
    CREATE UNIQUE INDEX uq_ares_signals_signal_uuid ON ares_signals (signal_uuid);
  END IF;
END
$$;

ALTER TABLE ares_signals ADD COLUMN IF NOT EXISTS display_id text;

ALTER TABLE active_trades ADD COLUMN IF NOT EXISTS signal_uuid uuid;
ALTER TABLE active_trades ADD COLUMN IF NOT EXISTS display_id text;
ALTER TABLE trade_analytics ADD COLUMN IF NOT EXISTS signal_uuid uuid;

ALTER TABLE ml_collection ADD COLUMN IF NOT EXISTS signal_uuid uuid;
ALTER TABLE ml_collection ADD COLUMN IF NOT EXISTS signal_display_id text;
ALTER TABLE ml_collection ADD COLUMN IF NOT EXISTS trade_binding_status text;
ALTER TABLE ml_collection ADD COLUMN IF NOT EXISTS snapshot_uuid uuid;
CREATE UNIQUE INDEX IF NOT EXISTS idx_ml_collection_snapshot_uuid
  ON ml_collection (snapshot_uuid);

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_active_trades_signal_uuid') THEN
    ALTER TABLE active_trades ADD CONSTRAINT fk_active_trades_signal_uuid
      FOREIGN KEY (signal_uuid) REFERENCES ares_signals(signal_uuid) NOT VALID;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_trade_analytics_signal_uuid') THEN
    ALTER TABLE trade_analytics ADD CONSTRAINT fk_trade_analytics_signal_uuid
      FOREIGN KEY (signal_uuid) REFERENCES ares_signals(signal_uuid) NOT VALID;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_ml_collection_signal_uuid') THEN
    ALTER TABLE ml_collection ADD CONSTRAINT fk_ml_collection_signal_uuid
      FOREIGN KEY (signal_uuid) REFERENCES ares_signals(signal_uuid) NOT VALID;
  END IF;
END;
$$;

-- Install the atomic RPC in the bridge migration
DROP FUNCTION IF EXISTS create_trade_entry_bridge(
  uuid, bigint, uuid, text, text, numeric, numeric, numeric, numeric,
  text, jsonb, jsonb, timestamptz
);
CREATE OR REPLACE FUNCTION create_trade_entry_bridge(
  p_trade_id uuid,
  p_signal_uuid uuid,
  p_display_id text,
  p_setup_type text,
  p_direction text,
  p_entry_price numeric,
  p_stop_loss numeric,
  p_target_1 numeric,
  p_target_2 numeric,
  p_added_time_ist text,
  p_market_context jsonb,
  p_oi_data jsonb,
  p_entry_timestamp timestamptz,
  p_legacy_signal_id bigint
) RETURNS uuid
LANGUAGE plpgsql
AS $$
BEGIN
  INSERT INTO active_trades (
    id, signal_id, signal_uuid, display_id, setup_type, direction, entry_price,
    stop_loss, target_1, target_2, state, added_time_ist
  ) VALUES (
    p_trade_id, p_legacy_signal_id::text, p_signal_uuid, p_display_id,
    p_setup_type, p_direction, p_entry_price,
    p_stop_loss, p_target_1, p_target_2, 'OPEN', p_added_time_ist
  ) ON CONFLICT (id) DO NOTHING;

  INSERT INTO trade_analytics (
    id, signal_id, signal_uuid, setup_type, direction, entry_price,
    market_context, oi_data, entry_timestamp, result_state
  ) VALUES (
    p_trade_id, p_legacy_signal_id, p_signal_uuid, p_setup_type, p_direction, p_entry_price,
    p_market_context, p_oi_data, p_entry_timestamp, 'OPEN'
  ) ON CONFLICT (id) DO NOTHING;
  IF NOT EXISTS (
    SELECT 1 FROM active_trades
    WHERE id = p_trade_id
      AND signal_id IS NOT DISTINCT FROM p_legacy_signal_id::text
      AND signal_uuid IS NOT DISTINCT FROM p_signal_uuid
      AND display_id IS NOT DISTINCT FROM p_display_id
      AND setup_type IS NOT DISTINCT FROM p_setup_type
      AND direction IS NOT DISTINCT FROM p_direction
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
$$;

GRANT EXECUTE ON FUNCTION create_trade_entry_bridge(
  uuid, uuid, text, text, text, numeric, numeric, numeric, numeric,
  text, jsonb, jsonb, timestamptz, bigint
) TO authenticated, service_role;
