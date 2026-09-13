-- Cutover mode schema changes for MANM-150

-- 1. Take schema lock and stop bridge-mode writers
DROP FUNCTION IF EXISTS create_trade_entry_bridge(
  uuid, uuid, text, text, text, numeric, numeric, numeric, numeric,
  text, jsonb, jsonb, timestamptz, bigint
);
-- 2. Retain old bigint as legacy_id, promote signal_uuid to id
ALTER TABLE ares_signals RENAME COLUMN id TO legacy_id;
ALTER TABLE ares_signals RENAME COLUMN signal_uuid TO id;
ALTER TABLE ares_signals DROP CONSTRAINT ares_signals_pkey;
ALTER TABLE ares_signals ADD PRIMARY KEY (id);

-- 3. Rename or copy trade-table signal_uuid to canonical signal_id and add FKs
ALTER TABLE active_trades RENAME COLUMN signal_id TO legacy_signal_id;
ALTER TABLE active_trades RENAME COLUMN signal_uuid TO signal_id;
ALTER TABLE active_trades ADD CONSTRAINT fk_active_trades_signal FOREIGN KEY (signal_id) REFERENCES ares_signals(id);

ALTER TABLE trade_analytics RENAME COLUMN signal_id TO legacy_signal_id;
ALTER TABLE trade_analytics RENAME COLUMN signal_uuid TO signal_id;
ALTER TABLE trade_analytics ADD CONSTRAINT fk_trade_analytics_signal FOREIGN KEY (signal_id) REFERENCES ares_signals(id);

ALTER TABLE ml_collection RENAME COLUMN signal_id TO legacy_signal_id;
ALTER TABLE ml_collection RENAME COLUMN signal_uuid TO signal_id;
ALTER TABLE ml_collection ADD CONSTRAINT fk_ml_collection_signal FOREIGN KEY (signal_id) REFERENCES ares_signals(id);

-- 4. Install the post-cutover RPC before greenfield writers resume.
CREATE OR REPLACE FUNCTION create_trade_entry_greenfield(
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
  p_entry_timestamp timestamptz
) RETURNS uuid
LANGUAGE plpgsql
AS $$
BEGIN
  INSERT INTO active_trades (
    id, signal_id, display_id, setup_type, direction, entry_price,
    stop_loss, target_1, target_2, state, added_time_ist
  ) VALUES (
    p_trade_id, p_signal_uuid, p_display_id, p_setup_type, p_direction,
    p_entry_price, p_stop_loss, p_target_1, p_target_2, 'OPEN', p_added_time_ist
  ) ON CONFLICT (id) DO NOTHING;

  INSERT INTO trade_analytics (
    id, signal_id, setup_type, direction, entry_price,
    market_context, oi_data, entry_timestamp, result_state
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
$$;

GRANT EXECUTE ON FUNCTION create_trade_entry_greenfield(
  uuid, uuid, text, text, text, numeric, numeric, numeric, numeric,
  text, jsonb, jsonb, timestamptz
) TO authenticated, service_role;
