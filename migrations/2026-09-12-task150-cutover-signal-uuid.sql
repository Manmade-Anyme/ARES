-- Cutover mode schema changes for MANM-150

-- 1. Take schema lock and stop bridge-mode writers
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
