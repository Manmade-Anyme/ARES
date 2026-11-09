# Problem Brief — TASK-20261109-dual-write-atr-trailing
**Date:** 2026-11-09
**Status:** approved

## Problem Statement (Original)
Implement dual-writing in `position_manager.py` so that whenever Option ATR trailing transitions occur, both `market_context.option_trailing` (canonical option data) and `market_context.atr_trailing` (SpanQuant frontend mirror) are persisted in Supabase (`trade_analytics.market_context`). This enables SpanQuant's live activity timeline parser to synthesize `TRAILING STOP · ADJUSTED` events automatically with zero schema migrations and zero breaking changes.

## Problem Statement (Simplified)
Mirror option ATR trailing state into `trade["market_context"]["atr_trailing"]` whenever option lifecycle events occur (`T1_HIT`, `OPTION_SL_RATCHETED`, `ATR_SL_UPDATED`, `T2_HIT`, `STOPPED_OUT_AT_BE`, `SL_HIT`, `ATR_SL_HIT`) and persist it to `trade_analytics.market_context` in Supabase without modifying database schemas or breaking existing transactional RPC contracts.

## What We Know
- SpanQuant frontend parses `ExecutionTimelineEvent[]` from `trade_analytics.market_context.atr_trailing` keyed on `last_bar` and extracts `stop_loss`, `current_atr`.
- ARES currently writes canonical option trailing solely to `trade_analytics.market_context.option_trailing`.
- Existing `apply_option_lifecycle_transition` RPC in PostgreSQL only updates the `option_trailing` subkey of `market_context`.
- `_persist_trade_context` in `position_manager.py` already supports merging `atr_snapshot` into `trade_analytics.market_context`.
- Invariants: Zero schema migrations, no breaking changes to RPCs, immutable outbox payloads preserved, `option_trailing` remains the source of truth for execution/accounting.

## Edge Cases Identified
1. **Timestamp Fallback:** If `last_processed_option_bar_end` is absent (e.g. before any 5m option candle completes), fallback to `t1_at` or `event_ts`.
2. **Break-Even Stop on T1:** On `T1_HIT`, `stop_loss` must equal the break-even cost basis (`original_option_entry` / `locked_option_sl`).
3. **Ratchet Progression:** On `OPTION_SL_RATCHETED` / `ATR_SL_UPDATED`, `stop_loss` must reflect the tightened option SL, and `previous_sl` must reflect the pre-ratchet SL.
4. **Exit Reason Status:** On terminal exit (`SL_HIT`, `STOPPED_OUT_AT_BE`, `ATR_SL_HIT`, etc.), `status` must capture the exit reason.
5. **Persistence Concurrency:** Background async write to `trade_analytics` must respect `self._context_locks[trade_id]` to prevent race conditions with `apply_option_lifecycle_transition`.

## Recommended Next Step
Proceed via Code-First mode: Write directive, write failing unit tests (TDD), implement dual-write in `position_manager.py`, verify full test suite passes, update changelog/docs/Obsidian, and create PR.
