-- =====================================================================
-- TASK-188 / TASK-189 — one-time production data repair
-- Run in the Supabase SQL Editor. Safe to run more than once (idempotent).
-- Order matters: section 1 must run before section 2.
-- =====================================================================
--
-- Fixes two independent problems:
--
--   1. Test fixtures written to the live tables. tests/conftest.py fetched
--      live Dhan credentials at session start, importing `storage` unmocked
--      before tests/unit/test_storage.py could patch supabase.create_client.
--      When that import-order-dependent patch lost the race, Storage() bound a
--      REAL client and test_storage.py's log_signal() fixtures landed in
--      production. Signature: spot/entry 24001.0, trigger 24000.0, reason
--      "Reason 1", Capital ₹10,000.00. All are OI_WALL_REJECTION. One is a
--      fabricated +99.0 T2_HIT that inflated the OI-wall average from +3.76 to
--      +14.34 pts/trade. (Code fix: PR #41.)
--
--   2. Naive-IST entry timestamps from before TASK-172 (shipped 2026-07-03).
--      Candle timestamps arrive naive-IST from Dhan; pre-TASK-172 they were
--      stored unlabeled, so Postgres read them as UTC — putting them 5h30m
--      ahead. NOTE: only the *entry* side is affected. exit_timestamp used
--      datetime.now(timezone.utc) all along and is already correct — do NOT
--      shift it. The symptom is negative hold times (e.g. -213 min).
--
-- =====================================================================


-- ---------------------------------------------------------------------
-- 0. PREVIEW — run this first and eyeball it. Changes nothing.
-- ---------------------------------------------------------------------
SELECT 'fixture signals'        AS what, count(*) FROM ares_signals    WHERE spot_at_signal = 24001.0
UNION ALL
SELECT 'fixture trades',              count(*) FROM trade_analytics WHERE entry_price = 24001.0
UNION ALL
SELECT 'signals w/ IST-as-UTC ts',    count(*) FROM ares_signals    WHERE timestamp > created_at + interval '1 hour'
UNION ALL
SELECT 'trades w/ IST-as-UTC entry',  count(*) FROM trade_analytics WHERE entry_timestamp > created_at + interval '1 hour';

-- Expected before running anything below:
--   fixture signals           4     (ares_signals ids 167-170)
--   fixture trades            9
--   signals w/ IST-as-UTC ts  31    (ids 156-186, 2026-06-24 .. 07-02)
--   trades w/ IST-as-UTC entry 30


-- ---------------------------------------------------------------------
-- 1. Delete the leaked test fixtures.
--    MUST run before section 2 — ids 167-170 sit inside the corrupted
--    timestamp range and would otherwise be shifted on the way out.
--    Matched on the fixture's placeholder price, not on id, so this stays
--    correct if ids ever differ.
-- ---------------------------------------------------------------------
BEGIN;

DELETE FROM trade_analytics WHERE entry_price     = 24001.0;
DELETE FROM ares_signals    WHERE spot_at_signal  = 24001.0;

COMMIT;


-- ---------------------------------------------------------------------
-- 2. Correct the pre-TASK-172 naive-IST timestamps.
--    Idempotent: corrupted rows sit ~330 min ahead of created_at; once
--    fixed they sit ~0 min from it, so the WHERE clause stops matching and
--    a second run is a no-op. This is why the predicate is a skew test and
--    not an id range.
--    exit_timestamp is deliberately untouched — it was always real UTC.
-- ---------------------------------------------------------------------
BEGIN;

UPDATE ares_signals
   SET timestamp = timestamp - interval '5 hours 30 minutes'
 WHERE timestamp > created_at + interval '1 hour';

UPDATE trade_analytics
   SET entry_timestamp = entry_timestamp - interval '5 hours 30 minutes'
 WHERE entry_timestamp > created_at + interval '1 hour';

COMMIT;


-- ---------------------------------------------------------------------
-- 3. Render timestamps as IST on read.
--    All timestamp columns are timestamptz, which stores an absolute UTC
--    instant — the offset on input is used to compute it and then discarded.
--    So this changes *display*, not stored data, and does not conflict with
--    storage.to_utc_iso(). Takes effect on NEW connections only.
--    Cannot run inside a transaction block; run it on its own.
-- ---------------------------------------------------------------------
ALTER DATABASE postgres SET timezone TO 'Asia/Kolkata';


-- ---------------------------------------------------------------------
-- 4. VERIFY — re-run section 0; all four counts must be 0.
--    Then confirm hold times are now positive and sane:
-- ---------------------------------------------------------------------
SELECT setup_type,
       count(*)                                                          AS closed,
       round(avg(EXTRACT(EPOCH FROM (exit_timestamp - entry_timestamp)) / 60), 1) AS avg_hold_min,
       round(avg(pnl_points), 2)                                          AS avg_pnl_pts
  FROM trade_analytics
 WHERE exit_timestamp IS NOT NULL
   AND pnl_points     IS NOT NULL
 GROUP BY setup_type
 ORDER BY setup_type;

-- avg_hold_min must be POSITIVE for every setup (it was negative before).
-- avg_pnl_pts after cleanup should read approximately:
--   EXHAUSTION_REVERSAL  +10.08 (n=26)
--   FAILED_BREAKOUT      +28.15 (n=10)
--   OI_WALL_REJECTION     +3.76 (n=8)    <- was a fake +14.34 with the fixtures
--   TREND_CONTINUATION    +3.23 (n=15)
