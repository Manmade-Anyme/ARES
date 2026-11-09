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
1. **In-Memory Dual-Writing (`position_manager.py::_record_option_transition`)**:
   Whenever an option lifecycle event (`T1_HIT`, `OPTION_SL_RATCHETED`, `ATR_SL_UPDATED`, `T2_HIT`, `STOPPED_OUT_AT_BE`, `SL_HIT`, `ATR_SL_HIT`) is staged, mirror the trailing stop state into `trade.setdefault("market_context", {})["atr_trailing"]`:
   ```python
   # Mirror active option ATR state for frontend timeline consumers (SpanQuant)
   trade.setdefault("market_context", {})["atr_trailing"] = {
       "last_bar": opt.get("last_processed_option_bar_end") or opt.get("t1_at") or event_ts,
       "stop_loss": opt.get("locked_option_sl") or (opt.get("original_option_entry") if opt.get("phase") in {"POST_T1", "POST_T2"} else None) or opt.get("original_option_sl"),
       "previous_sl": opt.get("previous_locked_option_sl"),
       "trailing_mode": "OPTION_ATR",
       "status": update_type or opt.get("phase"),
   }
   ```
2. **Supabase Persistence (`position_manager.py::_record_trade_update`)**:
   - Queue `_persist_trade_context` with `atr_snapshot = deepcopy((trade.get("market_context") or {}).get("atr_trailing"))`.
   - `_persist_trade_context` writes `context["atr_trailing"] = atr_snapshot` to Supabase `trade_analytics` table.
   - Guarded with `if getattr(self, "supabase", None) is None: return` for clean offline unit test execution.
3. **Preserved Invariants**:
   - Zero schema migrations. Zero database table alterations.
   - Zero mutations to immutable event outbox payloads (`apply_option_lifecycle_transition` RPC contract preserved).
   - `trade["market_context"]["option_trailing"]` remains single source of truth for execution, accounting, and milestone scoring.

---

## 3. Verification & Test Evidence
- Unit test suite: `tests/unit/test_option_atr_lifecycle_comprehensive.py`
  - `test_dual_write_atr_trailing_on_t1_hit`: asserts `atr_trailing["stop_loss"]` equals break-even cost basis and `last_bar` is set.
  - `test_dual_write_atr_trailing_on_option_sl_ratcheted`: asserts `atr_trailing["stop_loss"]` reflects ratcheted option SL and `previous_sl` is tracked.
  - `test_dual_write_atr_trailing_on_trade_exit`: asserts `status` reflects exit reason (`ATR_SL_HIT`).
  - `test_dual_write_atr_trailing_stopped_out_at_be`: asserts `status` reflects `STOPPED_OUT_AT_BE`.
  - `test_dual_write_atr_trailing_persists_to_supabase`: asserts `_persist_trade_context` persists `atr_trailing` to Supabase `trade_analytics.market_context`.
- Full unit test suite: **2,593 passed, 0 failed** in 68.98s.
