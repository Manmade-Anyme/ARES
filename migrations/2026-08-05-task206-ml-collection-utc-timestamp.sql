-- =====================================================================
-- TASK-206 — ml_collection timestamp naive-IST to UTC normalization
-- Run in the Supabase SQL Editor. Safe to run more than once (idempotent).
-- =====================================================================
--
-- Prior to TASK-206, MLCollector.snapshot recorded ts.isoformat() from naive
-- IST wall-clock datetimes. Postgres timestamptz columns interpreted naive ISO
-- strings as UTC, shifting recorded ml_collection timestamps +5h30m into the
-- future relative to true UTC created_at values and other system tables.
--
-- This migration normalizes legacy ml_collection timestamps back by 5.5 hours.
-- It guards on created_at - timestamp > interval '5 hours', so reruns are no-ops
-- and newly collected rows (where abs(created_at - timestamp) < 2 min) are untouched.
--

UPDATE ml_collection
SET timestamp = timestamp - interval '5.5 hours'
WHERE created_at - timestamp > interval '5 hours';
