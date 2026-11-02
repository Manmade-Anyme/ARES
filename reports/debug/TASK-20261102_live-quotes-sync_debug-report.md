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
- All 33 unit and integration tests passed cleanly.
- Full regression suite passed (2,155 passed, 45 subtests).
- Zero failures, zero regressions.
