# QA Report — TASK-20261102: Live Market Quotes Persistence for SpanQuant Frontend

Date: 2026-11-02. Verdict: PASS for local implementation and pipeline regression suite. Database migration file created (not executed on production Supabase per user directive).

| Check | Result |
|---|---|
| Quote Service Unit Tests (`test_quote_service.py`) | 34 passed |
| Quote Integration Tests (`test_quote_integration.py`) | 9 passed |
| Related Pipeline Suite (`test_quote_*.py`, `test_tick_feed.py`, `test_position_manager.py`, `test_main.py`) | 149 passed |
| Confirmed-signal + System-one Coverage Gate (`test_task210*.py`, `test_task211*.py`) | 398 passed, 35 subtests (100% statements, 100% branches) |
| SQL Migration Static Verification (`migrations/20261102_latest_quotes.sql`) | Clean syntax, idempotent table & RLS definitions, RPC helper |
| Option Contract Strict Identification | Passed: exactly validates `expiry` (YYYY-MM-DD), `strike` (> 0), `option_type` (`CE`/`PE`), `security_id` (digits) |
| No-Inference Guard | Passed: missing/empty/invalid field aborts selection without guessing |
| Throttled Batch Persistence | Passed: 1.0s in-memory throttle eliminates per-tick DB inserts; table capped at <= 2 rows |
| Credential Boundary Check | Passed: Dhan and Supabase keys strictly server-side; anon/public client has read-only RLS |
| PR #153 Review Fix: Supabase Client Init | Passed: `storage.get_supabase_client()` provides client; auto-initialization in singleton |
| PR #153 Review Fix: IST Timezone Normalization | Passed: naive candle timestamps properly convert IST to UTC without +5.5h drift |
| PR #153 Review Fix: Buffered Ticks Flush | Passed: WebSocket tick loop flushes quote cache before loop continuation |
| PR #153 Review Fix: Terminal Option Cleanup | Passed: `option_atr_v1` trades invoke `clear_selected_option()` on terminal state |
| PR #153 Review Fix: Non-null Bulk Metadata | Passed: non-null `raw_metadata: {}` on all rows; separate row upserts prevent schema union nulls |
| PR #153 Review Fix: Stale Feed Preservation | Passed: stalled ticks do not refresh timestamp with current time; old quotes age naturally |
| PR #153 Review Fix: Pending Deletion Retention | Passed: transient Supabase delete failures preserve `_clear_option_pending` until successful |
| PR #153 Review Fix: Nonblocking Persistence | Passed: synchronous Supabase `.execute()` offloaded from event loop via `run_in_executor` |
| PR #153 Review Fix: Older Observation Rejection | Passed: older historical candle closes cannot overwrite newer live tick quotes |
| PR #153 Review Fix: Empty Memory Cache Deletion | Passed: `clear_selected_option()` schedules deletion even when local cache is empty |
| PR #153 Review Fix: Startup Reconciliation | Passed: worker startup purges orphaned `OPTION:SELECTED` row if no active trade exists |

## Commands Executed

```sh
# Focused quote service & pipeline integration tests
python -m pytest tests/unit/test_quote_service.py tests/unit/test_quote_integration.py -v

# Related monitoring and position manager suites
python -m pytest tests/unit/test_quote_service.py tests/unit/test_quote_integration.py tests/unit/test_tick_feed.py tests/unit/test_position_manager.py tests/unit/test_reference_lifecycle_accounting_new.py -q

# Main loop tests
python -m pytest tests/unit/test_main.py -q

# Coverage gate
python -m pytest tests/unit/test_task210*.py tests/unit/test_task211*.py -q --cov=system_one --cov-branch --cov-report=term-missing --cov-fail-under=100

# Full test suite
python -m pytest tests/ -q
```

## Behavior Verified

1. **Exact Frontend Contract:**
   The output JSON contract matches the exact structure required by SpanQuant frontend:
   ```json
   {
     "underlying": {
       "symbol": "NIFTY",
       "currentSpot": 23300.8,
       "updatedAt": "2026-11-02T04:52:00Z"
     },
     "selectedOption": {
       "expiry": "2026-11-03",
       "strike": 23300,
       "optionType": "CE",
       "securityId": "53940",
       "currentPremium": 125.5,
       "updatedAt": "2026-11-02T04:52:00Z"
     }
   }
   ```
2. **Freshness Tiering:**
   - `FRESH`: Observation age $\le 10\text{s}$.
   - `STALE`: Observation age between $10\text{s}$ and $60\text{s}$.
   - `EXPIRED`: Observation age $> 60\text{s}$.
3. **No-Guessing Contract Matcher:**
   Attempts to match option quotes with missing `security_id`, missing `expiry`, invalid strike, or incorrect option type fail strict validation and return `None`.
4. **Data Isolation:**
   Live option LTP (`currentPremium`) and live NIFTY spot (`currentSpot`) are tracked independently in `latest_quotes` and never overwrite trade `entry_price`, `signal_spot`, or initial reference premiums in `active_trades`.
5. **No Per-Tick Database Growth:**
   Keyed on fixed primary keys `UNDERLYING:NIFTY` and `OPTION:SELECTED`. Upserts replace existing rows in-place.

## Recommendation

Ready for PR review and merge. Database migration `migrations/20261102_latest_quotes.sql` should be executed on Supabase prior to deploying worker updates to production.
