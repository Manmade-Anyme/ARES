-- Bridge mode schema changes for MANM-150

ALTER TABLE ares_signals ADD COLUMN IF NOT EXISTS signal_uuid uuid UNIQUE;
ALTER TABLE ares_signals ADD COLUMN IF NOT EXISTS display_id text;

ALTER TABLE active_trades ADD COLUMN IF NOT EXISTS signal_uuid uuid;
ALTER TABLE trade_analytics ADD COLUMN IF NOT EXISTS signal_uuid uuid;

ALTER TABLE ml_collection ADD COLUMN IF NOT EXISTS signal_uuid uuid;
ALTER TABLE ml_collection ADD COLUMN IF NOT EXISTS signal_display_id text;
ALTER TABLE ml_collection ADD COLUMN IF NOT EXISTS trade_binding_status text;


-- Install the atomic RPC in the bridge migration
CREATE OR REPLACE FUNCTION create_trade_entry_bridge(
  p_trade_id uuid,
  p_signal_id bigint,
  p_signal_uuid uuid,
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
    id, signal_id, signal_uuid, setup_type, direction, entry_price, 
    stop_loss, target_1, target_2, state, added_time_ist
  ) VALUES (
    p_trade_id, p_signal_id::text, p_signal_uuid, p_setup_type, p_direction, p_entry_price,
    p_stop_loss, p_target_1, p_target_2, 'OPEN', p_added_time_ist
  ) ON CONFLICT (id) DO NOTHING;

  INSERT INTO trade_analytics (
    id, signal_id, signal_uuid, setup_type, direction, entry_price,
    market_context, oi_data, entry_timestamp, result_state
  ) VALUES (
    p_trade_id, p_signal_id, p_signal_uuid, p_setup_type, p_direction, p_entry_price,
    p_market_context, p_oi_data, p_entry_timestamp, 'OPEN'
  ) ON CONFLICT (id) DO NOTHING;
  
  RETURN p_trade_id;
END;
$$;
