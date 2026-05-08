# Triage Brief — TASK-008
**Severity:** high
**Triggered by:** human

## Observed Behaviour
1. Supabase insert fails with `PGRST204`: "Could not find the 'signal_id' column of 'active_trades' in the schema cache".
2. Connection to Dhan API sporadically aborts with `ConnectionResetError(104, 'Connection reset by peer')`.

## Expected Behaviour
1. `signal_id` should be successfully persisted alongside the rest of the trade data in the `active_trades` table.
2. The `dhanhq` connection should handle transient network resets gracefully via retry logic without crashing or logging fatal exception traces that bypass the error handling.

## Known / Unknown
**Known:**
- `position_manager.py` was updated in a previous task to include `"signal_id"` in `trade_data`.
- The `active_trades` schema in Supabase has not been updated yet to include `signal_id`.
- `schema.sql` now reflects the addition of `signal_id`.
- The `dhanhq` Python SDK internally uses `requests` and handles POST requests, but logs exceptions directly via `logging.error` when the socket is closed by the peer, resulting in raw console traceback errors.

**Unknown:**
- Does the DhanHQ SDK raise an exception that the `PriceFetcher` catches, or does it return an empty dict/None when this logging error occurs? (Needs QA verification).

## Minimal Reproduction Steps
1. Run `position_manager.py` and trigger an `add_trade` with a `signal_id`. Observe the `PGRST204` exception.
2. Wait for a network interruption or DhanHQ server timeout while `dhanhq.intraday_minute_data()` is polling. Observe `ConnectionResetError(104)`.

## Delegation
→ Human (must apply `schema.sql` to Supabase SQL editor)
→ Debug Agent (investigate DhanHQ retry wrapper robustness)
