# TASK-20261109: Dual-Write `market_context.atr_trailing` for SpanQuant Frontend Timeline Compatibility

**Date:** 2026-11-09  
**Status:** Completed  
**Branch:** `feature/TASK-20261109-dual-write-atr-trailing-context`  

---

## 1. Context & Problem Statement
SpanQuant frontend polls `/api/ares` and parses `ExecutionTimelineEvent[]` from Supabase:
- **Timeline Step 3 (In-Trade Adjustments)**:
  - Source: `trade_analytics.market_context.atr_trailing`
  - Timestamp trigger: `atr_trailing.last_bar`
  - Extracted fields: `stop_loss`, `current_atr`
- **Prior Behavior in ARES**:
  - ARES wrote option trailing state exclusively to `trade_analytics.market_context.option_trailing` (`locked_option_sl`, `last_processed_option_bar_end`, `highest_milestone`, `phase`).
  - Because `market_context.atr_trailing` was not updated during option ATR trails, the SpanQuant frontend timeline missed live stop-tightening events.

---

## 2. Solution & Architectural Invariants
1. **In-Memory Dual-Writing (`position_manager.py::_mirror_option_atr_trailing`)**:
   Whenever an option lifecycle event (`T1_HIT`, `OPTION_SL_RATCHETED`, `ATR_SL_UPDATED`, `T2_HIT`, `STOPPED_OUT_AT_BE`, `SL_HIT`, `ATR_SL_HIT`) is staged or reconciled, mirrors the trailing stop state into `trade.setdefault("market_context", {})["atr_trailing"]`.
   - **Timestamp Normalization**: Normalizes all `last_bar` timestamps to ISO strings (via `_normalize_iso_timestamp`) to ensure valid JSON serialization when `OptionCandle.end_time` contains datetime objects.
   - **Lifecycle Gating**: Gated against `PRE_T1` routine quotes and non-adjusting quote watermarks to prevent SpanQuant from emitting false adjustment events before T1 or without an actual stop move.
   - **Revision Conflict Reconciliation**: In `_flush_option_trade`, rebuilds the mirror upon conflict resolution and queues a context write so frontend consumers never retain a stale or lower stop than canonical `option_trailing`.
2. **Supabase Persistence & Retries (`position_manager.py::_record_trade_update`, `_persist_trade_context`)**:
   - Queues `_persist_trade_context` with `atr_snapshot = deepcopy((trade.get("market_context") or {}).get("atr_trailing"))`.
   - Tracks pending writes in `self._pending_atr_trailing_context`.
   - Retries pending ATR trailing context across `update_trades` ticks and shutdown drains (`flush_pending_trade_updates`), clearing only on confirmed DB persistence (`updated.data`).
   - Guarded with `if getattr(self, "supabase", None) is None: return` for clean offline unit test execution.
3. **Preserved Invariants**:
   - Zero schema migrations. Zero database table alterations.
   - Zero mutations to immutable event outbox payloads (`apply_option_lifecycle_transition` RPC contract preserved).
   - `trade["market_context"]["option_trailing"]` remains single source of truth for execution, accounting, and milestone scoring.

---

## 3. Verification & Test Evidence
- Unit test suite: `tests/unit/test_option_atr_lifecycle_comprehensive.py` (25 tests)
  - `test_dual_write_atr_trailing_on_t1_hit`: asserts `atr_trailing["stop_loss"]` equals break-even cost basis and `last_bar` is set.
  - `test_dual_write_atr_trailing_on_option_sl_ratcheted`: asserts `atr_trailing["stop_loss"]` reflects ratcheted option SL and `previous_sl` is tracked.
  - `test_dual_write_atr_trailing_on_trade_exit`: asserts `status` reflects exit reason (`ATR_SL_HIT`).
  - `test_dual_write_atr_trailing_stopped_out_at_be`: asserts `status` reflects `STOPPED_OUT_AT_BE`.
  - `test_dual_write_atr_trailing_persists_to_supabase`: asserts `_persist_trade_context` persists `atr_trailing` to Supabase `trade_analytics.market_context`.
  - `test_pre_t1_routine_quote_does_not_create_atr_trailing`: asserts PRE_T1 quotes do not create or emit ATR trailing context.
  - `test_post_t1_routine_quote_does_not_advance_last_bar`: asserts routine quotes in POST_T1 do not advance `last_bar`.
  - `test_option_candle_datetime_normalized_to_iso_string`: asserts `OptionCandle.end_time` (datetime) is converted to ISO string and JSON serializable.
  - `test_revision_conflict_reconciles_and_persists_atr_trailing_mirror`: asserts revision conflict adopts stronger stop in mirror and queues write.
  - `test_pending_atr_trailing_context_retained_on_failure_and_cleared_on_success`: asserts persistence failure retains pending state and clears on success.
  - `test_flush_pending_trade_updates_retries_pending_atr_trailing`: asserts shutdown drain retries unpersisted ATR trailing context.
  - `test_naive_datetime_localized_to_ist_in_iso_string`: asserts `_normalize_iso_timestamp` localizes naive datetimes to IST (+05:30) before formatting.
  - `test_dual_write_atr_trailing_on_t2_hit`: asserts on T2_HIT, `atr_trailing.status` reflects `T2_HIT`.
  - `test_dual_write_atr_trailing_multiple_sequential_ratchets`: asserts multiple sequential stop ratchets maintain valid stop_loss and previous_sl progression.
- Full unit test suite: **2,602 passed, 0 failed** in 78.86s.
