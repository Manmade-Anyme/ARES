-- Bridge mode schema changes for MANM-150

ALTER TABLE ares_signals ADD COLUMN IF NOT EXISTS signal_uuid uuid UNIQUE;
ALTER TABLE ares_signals ADD COLUMN IF NOT EXISTS display_id text;

ALTER TABLE active_trades ADD COLUMN IF NOT EXISTS signal_uuid uuid;
ALTER TABLE trade_analytics ADD COLUMN IF NOT EXISTS signal_uuid uuid;

ALTER TABLE ml_collection ADD COLUMN IF NOT EXISTS signal_uuid uuid;
ALTER TABLE ml_collection ADD COLUMN IF NOT EXISTS signal_display_id text;
ALTER TABLE ml_collection ADD COLUMN IF NOT EXISTS trade_binding_status text;
