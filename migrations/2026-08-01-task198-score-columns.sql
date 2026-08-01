-- =====================================================================
-- TASK-198 — multi-class trade scoring: the two score columns
-- Run in the Supabase SQL Editor. Safe to run more than once (idempotent).
-- =====================================================================
--
-- TASK-198 added `trade_analytics.score` and `ml_collection.trade_score` to
-- schema.sql and ml_signal/schema.sql, but those files are CREATE TABLE
-- reference definitions — editing them does nothing to a table that already
-- exists. The columns were applied to production by hand on 2026-08-01; this
-- file is the missing record of that change, so a rebuilt or restored database
-- reaches the same shape.
--
-- Without these columns, storage.AnalyticsLogger.log_exit fails on every trade
-- close: `score` is part of the trade_analytics UPDATE that also writes
-- exit_timestamp, exit_price, pnl_points and result_state, and PostgREST
-- rejects the whole statement with PGRST204. The failure is silent — log_exit
-- schedules _update via run_in_executor without awaiting it — so exits would
-- simply stop being recorded with nothing in the logs.
--
-- Scoring, as implemented in storage.log_exit and ml_signal.labeling:
--
--   T2_HIT             2  -- ran to the second target
--   T1_HIT             1  -- first target booked
--   STOPPED_OUT_AT_BE  1  -- T1 was touched (40% booked) then trailed SL hit
--   SL_HIT             0  -- original stop
--   TIME_STOP          0  -- forced to break-even without ever touching T1
--   STOPPED_OUT        0  -- legacy pre-TASK-198 state
--
-- NOTE: this deviates from directives/TASK-198_multiclass-scoring.md, which
-- specified STOPPED_OUT=0 and SL_HIT=-1. The implemented mapping is the correct
-- one for how these trades are actually run: STOPPED_OUT_AT_BE can only occur
-- after T1 is touched, at which point 40% is already booked, so it is a win;
-- TIME_STOP forces break-even without ever reaching T1, so it is not. The
-- directive has been updated to match. Scores are non-negative so the column
-- maps directly onto the ML label without an offset.

-- ---------------------------------------------------------------------
-- 1. trade_analytics.score
-- ---------------------------------------------------------------------
ALTER TABLE trade_analytics ADD COLUMN IF NOT EXISTS score integer;

COMMENT ON COLUMN trade_analytics.score IS
  'Point-based outcome (TASK-198): T2_HIT=2, T1_HIT/STOPPED_OUT_AT_BE=1, SL_HIT/TIME_STOP/STOPPED_OUT=0. NULL while OPEN.';

-- ---------------------------------------------------------------------
-- 2. ml_collection.trade_score
-- ---------------------------------------------------------------------
ALTER TABLE ml_collection ADD COLUMN IF NOT EXISTS trade_score integer;

COMMENT ON COLUMN ml_collection.trade_score IS
  'Back-filled from trade_analytics.score when the trade closes (TASK-198). NULL until then.';

-- ---------------------------------------------------------------------
-- 3. Back-fill score on trades that closed before the column existed.
--    Only touches rows where score IS NULL, so re-running is a no-op.
-- ---------------------------------------------------------------------
UPDATE trade_analytics
SET score = CASE result_state
    WHEN 'T2_HIT'            THEN 2
    WHEN 'T1_HIT'            THEN 1
    WHEN 'STOPPED_OUT_AT_BE' THEN 1
    WHEN 'SL_HIT'            THEN 0
    WHEN 'TIME_STOP'         THEN 0
    WHEN 'STOPPED_OUT'       THEN 0
END
WHERE score IS NULL
  AND result_state IN ('T2_HIT', 'T1_HIT', 'STOPPED_OUT_AT_BE',
                       'SL_HIT', 'TIME_STOP', 'STOPPED_OUT');

-- ---------------------------------------------------------------------
-- 4. Propagate to ml_collection for rows already joined to a trade.
--    ml_collection.signal_id is text; trade_analytics.signal_id is the
--    ares_signals row id (see TASK-194) — hence the cast.
-- ---------------------------------------------------------------------
UPDATE ml_collection AS m
SET trade_score = t.score
FROM trade_analytics AS t
WHERE m.signal_id = t.signal_id::text
  AND t.score IS NOT NULL
  AND m.trade_score IS NULL;

-- Verify:
--   SELECT result_state, score, count(*) FROM trade_analytics GROUP BY 1,2 ORDER BY 1;
--   SELECT count(*) FROM ml_collection WHERE trade_score IS NOT NULL;
