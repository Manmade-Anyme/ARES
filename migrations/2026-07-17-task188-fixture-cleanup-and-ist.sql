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
--
-- NOTE on the fixture predicate: matching on the 24001.0 price ALONE is not
-- safe. NIFTY genuinely trades in that range, so a real signal could sit at
-- exactly that spot and would be destroyed by a price-only delete. The
-- predicate below therefore requires the *whole* fixture signature AND the
-- confirmed ids. The decisive discriminator is reasons[0] = 'Reason 1':
-- production code can never emit it — OIWallDetector._build_signal always
-- writes "Price approached massive OI wall at <strike>" first. If either the
-- ids or the signature ever drift, these statements match zero rows and do
-- nothing, which is the intended failure mode.
-- ---------------------------------------------------------------------
SELECT 'fixture signals'        AS what, count(*) FROM ares_signals
 WHERE id IN (167, 168, 169, 170)
   AND setup_type      = 'OI_WALL_REJECTION'
   AND spot_at_signal  = 24001.0
   AND trigger_price   = 24000.0
   AND reasons ->> 0   = 'Reason 1'
UNION ALL
SELECT 'fixture trades', count(*) FROM trade_analytics
 WHERE id IN ('38191236-c717-487a-ad62-e6eb637c482e',
              '132a34db-0292-4760-8d2b-706e2e774213',
              '9138816b-47dd-4234-a206-14f5eb3037f9',
              '16b7fe7a-644e-43af-a147-609258f17b7f',
              '8c6714c0-bd42-4c77-9cc2-196746ca5b48',
              '1feee0a2-2eea-4ed5-935c-f5f83dc51233',
              'a4da1358-e76f-457e-a0a5-20e7c20d7ff8',
              '3bd130e1-7b4c-43f2-a514-fcd15da043cc',
              '0269584d-c41e-4e00-8948-e6eb59ffe124')
   AND setup_type   = 'OI_WALL_REJECTION'
   AND entry_price  = 24001.0
   AND signal_id IS NULL
   AND market_context -> 'reasons' ->> 0 = 'Reason 1'
UNION ALL
SELECT 'signals w/ IST-as-UTC ts',    count(*) FROM ares_signals    WHERE timestamp > created_at + interval '1 hour'
UNION ALL
SELECT 'trades w/ IST-as-UTC entry',  count(*) FROM trade_analytics WHERE entry_timestamp > created_at + interval '1 hour';

-- Expected before running anything below:
--   fixture signals           4     (ares_signals ids 167-170)
--   fixture trades            9
--   signals w/ IST-as-UTC ts  31    (ids 156-186, 2026-06-24 .. 07-02)
--   trades w/ IST-as-UTC entry 30
--
-- If "fixture signals" is not 4 or "fixture trades" is not 9, STOP and
-- re-inspect — do not loosen the predicate to make the numbers match.


-- ---------------------------------------------------------------------
-- 1. Delete the leaked test fixtures.
--    MUST run before section 2 — ids 167-170 sit inside the corrupted
--    timestamp range and would otherwise be shifted on the way out.
--    Predicate = confirmed ids AND the full fixture signature (see the note
--    in section 0). A price-only match would risk deleting a real signal that
--    happens to sit at 24001.0.
--    Both statements are guarded to delete at most their expected row count.
-- ---------------------------------------------------------------------
BEGIN;

DELETE FROM trade_analytics
 WHERE id IN ('38191236-c717-487a-ad62-e6eb637c482e',
              '132a34db-0292-4760-8d2b-706e2e774213',
              '9138816b-47dd-4234-a206-14f5eb3037f9',
              '16b7fe7a-644e-43af-a147-609258f17b7f',
              '8c6714c0-bd42-4c77-9cc2-196746ca5b48',
              '1feee0a2-2eea-4ed5-935c-f5f83dc51233',
              'a4da1358-e76f-457e-a0a5-20e7c20d7ff8',
              '3bd130e1-7b4c-43f2-a514-fcd15da043cc',
              '0269584d-c41e-4e00-8948-e6eb59ffe124')
   AND setup_type   = 'OI_WALL_REJECTION'
   AND entry_price  = 24001.0
   AND signal_id IS NULL
   AND market_context -> 'reasons' ->> 0 = 'Reason 1';

DELETE FROM ares_signals
 WHERE id IN (167, 168, 169, 170)
   AND setup_type      = 'OI_WALL_REJECTION'
   AND spot_at_signal  = 24001.0
   AND trigger_price   = 24000.0
   AND reasons ->> 0   = 'Reason 1';

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
--    So this changes *display* only; stored data is untouched and this does
--    not conflict with storage.to_utc_iso().
--    Cannot run inside a transaction block; run it on its own.
--
--    SCOPE — this matters. This Supabase project is SHARED: Gamma Blaster
--    (gb_*), Kronos, Phantom, Sniper, Order Flow and ARES all live in the same
--    `postgres` database. A database-wide `ALTER DATABASE postgres SET timezone`
--    would change what PostgREST renders for EVERY one of those apps. ARES and
--    Kronos are verified safe (both parse with datetime.fromisoformat, which
--    handles any offset, and render via ist_now()), but the others are separate
--    codebases — anything doing strptime(..., "…+00:00") or string-slicing the
--    offset would break or shift by 5h30m.
--
--    So scope it to the role you query as. This makes the SQL Editor and any
--    direct psql session show IST, and leaves every app's API connection
--    (PostgREST authenticates as `authenticator` → service_role/anon) exactly
--    as it is today.
-- ---------------------------------------------------------------------
ALTER ROLE postgres SET timezone TO 'Asia/Kolkata';

-- Reconnect (or open a new SQL Editor tab) for this to take effect —
-- role settings apply at connection time, not to the live session.
-- Confirm with:  SHOW timezone;   -->  Asia/Kolkata
--
-- If you ever DO want it database-wide, the statement is below. Only run it
-- after checking how Gamma Blaster / Phantom / Sniper / Order Flow parse
-- timestamps — it changes their reads too:
--
--   ALTER DATABASE postgres SET timezone TO 'Asia/Kolkata';


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
