# QA Report — TASK-008
**Date:** 2026-05-08
**Verdict:** ✅ PASS

## Issues Identified & Resolved

### BUG-008-01: Supabase Schema Mismatch (RESOLVED)
**Description:** Inserting into `active_trades` fails with `PGRST204` because the `signal_id` column added in previous commits has not been applied to the production Supabase database.
**Reproduction:** Trigger an `add_trade` flow in `PositionManager`.
**Resolution:** The `schema.sql` file was updated to include `signal_id text` and the command was executed against Supabase. Verified with `test_supabase_schema.py` script that inserting records with `signal_id` now works successfully.

### BUG-008-02: DhanHQ Connection Reset Traceback
**Description:** The application prints raw exception tracebacks to the console when the underlying `requests` connection is closed by the Dhan server (`ConnectionResetError(104)`). While the retry logic in `price_fetcher.py` handles exceptions, the SDK itself prints a disruptive `ERROR:root:Exception in DhanHQConnection.POST` directly to stdout/stderr.
**Reproduction:** Run the `ARES` engine long enough to encounter a connection drop from Dhan API.
**Recommendation:** This is a cosmetic console issue caused by the `dhanhq` SDK's internal logging. The ARES engine still recovers because `price_fetcher.py`'s retry logic loops over failures. To suppress the noise, the `dhanhq` logger can be muted, or we can simply ignore it.

## Actions Taken
- Updated `schema.sql` to include the missing `signal_id`.
- Filed Triage Brief in `directives/briefs/TASK-008_triage-brief.md`.
