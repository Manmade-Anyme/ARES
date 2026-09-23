-- MANM-152: Exit Timestamp Validation and Flagging

-- 1. Add time_metrics_excluded to trade_analytics
ALTER TABLE trade_analytics
ADD COLUMN IF NOT EXISTS time_metrics_excluded boolean NOT NULL DEFAULT false;

-- 2. Add entry_timestamp and time_metrics_excluded to active_trades
ALTER TABLE active_trades
ADD COLUMN IF NOT EXISTS entry_timestamp timestamptz,
ADD COLUMN IF NOT EXISTS time_metrics_excluded boolean NOT NULL DEFAULT false;

-- 3. Backfill active_trades from trade_analytics
UPDATE active_trades AS active
SET entry_timestamp = analytics.entry_timestamp,
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
        '{"flag": "INVALID_NEGATIVE_DURATION", "reason": "Missing exit timestamp or unrecoverable", "investigation": "MANM-152"}'::jsonb
    )
WHERE exit_timestamp IS NULL AND result_state != 'OPEN';

UPDATE active_trades
SET time_metrics_excluded = true
WHERE exit_timestamp IS NULL AND state NOT IN ('OPEN');

-- 5. Add Constraints
ALTER TABLE trade_analytics
ADD CONSTRAINT chk_trade_analytics_exit_chronology
CHECK (time_metrics_excluded OR exit_timestamp IS NULL OR exit_timestamp >= entry_timestamp);

ALTER TABLE trade_analytics
ADD CONSTRAINT chk_trade_analytics_closed_requires_exit
CHECK (result_state = 'OPEN' OR exit_timestamp IS NOT NULL OR time_metrics_excluded = true);

ALTER TABLE active_trades
ALTER COLUMN entry_timestamp SET NOT NULL;

ALTER TABLE active_trades
ADD CONSTRAINT chk_active_trades_exit_chronology
CHECK (time_metrics_excluded OR exit_timestamp IS NULL OR exit_timestamp >= entry_timestamp);

ALTER TABLE active_trades
ADD CONSTRAINT chk_active_trades_closed_requires_exit
CHECK (state = 'OPEN' OR exit_timestamp IS NOT NULL OR time_metrics_excluded = true);
