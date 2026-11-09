# TASK-20261109: Dual-Write `market_context.atr_trailing` for SpanQuant Frontend Timeline Compatibility

**Date:** 2026-11-09
**Status:** implementation in progress
**Assigned to:** PM / Code Generator / QA / PR Reviewer

## Goal
Implement dual-writing in `position_manager.py` so that whenever Option ATR trailing transitions occur, both `market_context.option_trailing` (canonical option data) and `market_context.atr_trailing` (SpanQuant frontend mirror) are staged in memory and persisted in Supabase (`trade_analytics.market_context`). This enables SpanQuant's live activity timeline parser to synthesize `TRAILING STOP · ADJUSTED` events automatically with zero schema migrations and zero breaking changes.

## Preserved Invariants
- Zero database schema migrations or table modifications.
- Zero mutations to immutable outbox event payloads (`apply_option_lifecycle_transition` RPC contract untouched).
- `market_context.option_trailing` remains the single source of truth for execution, accounting, and milestone scoring.
- All existing tests continue passing without regression.

## Implementation Details

1. **`position_manager.py::_record_option_transition`**:
   Whenever an option lifecycle event is staged, mirror active option trailing state into `trade.setdefault("market_context", {})["atr_trailing"]`:
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

2. **`position_manager.py::_record_trade_update`**:
   Pass `atr_snapshot = deepcopy((trade.get("market_context") or {}).get("atr_trailing"))` to `self._persist_trade_context(...)` so that `trade_analytics.market_context` in Supabase is updated with the mirrored `atr_trailing` dictionary.

3. **`position_manager.py::_persist_trade_context`**:
   Ensure graceful return if `getattr(self, "supabase", None) is None` for offline unit testing environments.

## Acceptance Criteria
- On `T1_HIT`, `market_context["atr_trailing"]["stop_loss"]` equals the break-even cost basis and `last_bar` is set.
- On `OPTION_SL_RATCHETED` / `ATR_SL_UPDATED`, `market_context["atr_trailing"]["stop_loss"]` reflects the tightened option SL.
- On trade exit, `status` reflects the exit reason (`STOPPED_OUT_AT_BE`, `SL_HIT`, `ATR_SL_HIT`, etc.).
- `pytest tests/unit/` passes 100%.
