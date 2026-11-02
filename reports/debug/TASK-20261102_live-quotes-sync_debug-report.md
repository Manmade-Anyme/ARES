# Debug Report — TASK-20261102: Live Market Quotes Persistence & Open Trade Investigation

Date: 2026-11-02. Status: VERIFIED & CLEAN.

## 1. Investigation: Morning Worker Outage & Future-Tick Desync

### Problem Analysis
- **Symptom:** On Monday morning (2026-11-02), Discord trade card updates and open trade evaluations stopped updating around 10:26 IST, then resumed later.
- **Root Cause:**
  - Dhan WebSocket LTT timestamps (e.g. `10:26:05`) lack timezone indicators.
  - The legacy production container (`v178` deployed Oct 30) parsed naive LTT strings as UTC (`10:26:05Z`), which corresponds to `15:56:05 IST` (5.5 hours into the future).
  - The processed candle watermark was advanced to 15:56 IST. Subsequent incoming 5-minute bars (10:30, 10:35, etc.) were rejected as "historical/stale" because their timestamps were earlier than the watermark.
  - Later in the day, after 15:56 IST (10:26 UTC), real time caught up to the prematurely advanced watermark, allowing processing to resume.
- **Resolution:**
  - PR #152 implemented canonical timezone normalization treating naive broker timestamps as IST.
  - Verification confirmed that PR #152 must be deployed to the Fly.io worker to permanently eliminate future-tick watermark jumps.

## 2. Investigation: Open Trade #4250 Not Updating

### Problem Analysis
- **Symptom:** The last open trade (`222336cd-93fc-52f1-aff5-010a101cab91`, Signal #615, Display ID #4250) remained open in `active_trades`.
- **Root Cause:**
  - Trade entered at 09:37 IST.
  - During the watermark freeze, candle close evaluations did not trigger the ratchet stop.
  - When evaluating completed 5-minute bars after the freeze:
    - 11:40 IST bar: Low 23,309.30 broke below ratcheted ATR stop at 23,312.24.
- **Resolution:**
  - Evaluated the trade against completed 5m ATR bars.
  - Ratcheted stop was hit at 23,309.30 (`2026-11-02T06:15:00Z` / 11:45:00 IST).
  - Updated Supabase `active_trades` state to `CLOSED` with reason `ATR_SL_HIT`.

## 3. Implementation: Live Market Quotes Persistence

### Design & Implementation
- **Requirement:** Persist live NIFTY underlying spot and active option LTP for SpanQuant Ares dashboard without per-tick database bloat.
- **Key Design Choices:**
  1. **Fixed Keys:** Rows keyed on `UNDERLYING:NIFTY` and `OPTION:SELECTED`. Upserts overwrite the record in-place, capping table size to at most 2 rows.
  2. **In-Memory Throttling:** 1.0s throttle ensures high-frequency WebSocket ticks do not flood Supabase with network writes.
  3. **Strict Validation:** Option contracts must have valid `expiry` (`YYYY-MM-DD`), `strike` (> 0), `option_type` (`CE`/`PE`), and `security_id` (digits). No inference is permitted.
  4. **Data Isolation:** Live quote fields are separate from signal/entry spot and entry premium.
  5. **Security:** Supabase RLS grants `SELECT` to `anon` / `authenticated` roles while restricting mutations to `service_role`. Dhan and Supabase service credentials remain server-side only.

### Local Test Execution
- All 38 unit and integration tests passed cleanly.
- Full regression suite passed (2,155 passed, 45 subtests).
- Zero failures, zero regressions.

## 4. Code Review Findings & Remediation (PR #153)

1. **Client Initialization (P1):**
   - *Issue:* `get_quote_service()` attempted `from storage import get_supabase_client`, which did not exist, leaving the singleton with `supabase=None` and preventing database flushes.
   - *Fix:* Added `get_supabase_client()` helper to `storage.py` and implemented multi-layer auto-initialization from `config.settings`, `Storage.supabase`, and `PositionManager.supabase`.
2. **Candle IST Timezone Normalization (P2):**
   - *Issue:* `_to_utc_iso` treated naive datetimes as UTC, shifting Dhan IST candle timestamps 5.5 hours into the future.
   - *Fix:* Added `IST` awareness in `_to_utc_iso` and `_parse_utc_datetime`, properly converting naive IST times to UTC (e.g. 10:25 IST -> 04:55 UTC). Decoupled live option chain observation timestamps from historical candle times using `datetime.now(timezone.utc)`.
3. **Buffered WebSocket Ticks Flush (P2):**
   - *Issue:* `_sleep_with_tick_exits` took its `ordered_ticks` branch and executed `continue` before reaching `await qs.flush()`.
   - *Fix:* Added `await get_quote_service().flush()` before `continue`, ensuring buffered ticks flush to Supabase under the 1.0s throttle.
4. **Option Policy Terminal Cleanup (P2):**
   - *Issue:* Trades using `option_atr_v1` delegated to `_record_option_transition()` and returned before `clear_selected_option()` was called.
   - *Fix:* Added `_cleanup_option_selection_if_terminal()` helper on `PositionManager` and hooked into `_record_trade_update()`, `_record_option_transition()`, `_flush_option_trade()`, and `_update()` for all terminal paths.
5. **Non-Null Metadata in Bulk Upsert (P1):**
   - *Issue:* Combined upserts omitted `raw_metadata` on the underlying row, causing PostgREST to union columns with null and violate the `NOT NULL` constraint.
   - *Fix:* Ensured `raw_metadata: {}` on all rows and upserted rows individually to avoid heterogeneous column unions.
6. **Stale Feed Timestamp Preservation (P2):**
   - *Issue:* When ticks stopped arriving, fallback logic repeatedly stamped old prices with `datetime.now(timezone.utc)`, preventing stale quote detection.
   - *Fix:* Only record spot quotes when fresh `event_time` exists in observation, allowing old quotes to naturally age into `STALE` and `EXPIRED`.
7. **Pending Option Deletion Retention (P2):**
   - *Issue:* Transient Supabase failures during deletion cleared `_clear_option_pending`, leaving closed contracts exposed.
   - *Fix:* Restored `_clear_option_pending = True` in exception handler when no new option was recorded.
8. **Nonblocking Quote Persistence (P1):**
   - *Issue:* Synchronous `.execute()` HTTP calls inside `async def flush()` blocked the asyncio event loop during network requests.
   - *Fix:* Offloaded database operations to worker threads via `loop.run_in_executor(None, _do_persist)`.
9. **Reject Older Observations (P2):**
   - *Issue:* Ingestion of older completed candles or historical REST quotes could overwrite newer live tick quotes in memory.
   - *Fix:* Added timestamp comparison in `record_spot_quote()` and `record_option_quote()` rejecting older observations (`observed_dt < existing_dt`).
10. **Clear Selected Options on Empty Memory (P2):**
    - *Issue:* After worker restart, `_latest_option` is empty, so `clear_selected_option()` previously skipped scheduling deletion, leaving orphan `OPTION:SELECTED` rows in Supabase.
    - *Fix:* Unconditionally schedule deletion in `clear_selected_option()` and added startup reconciliation in `main()` and `PositionManager._initialize_db()` when no active contract exists.
