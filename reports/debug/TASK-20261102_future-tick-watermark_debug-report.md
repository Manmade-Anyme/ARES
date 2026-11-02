# Debug Review — TASK-20261102 Future Tick Watermark

**Date:** 2026-11-02  
**Scope:** Read-only review of TickFeed future-timestamp fallback, main-loop tick replay, and post-T1 ATR watermark recovery.

## Follow-up Re-review

**Status:** Resolved. The earlier limitations around receipt-only option-policy protection and mixed-batch interleaving have been addressed and verified with tests (`158 passed` in focused suite).

### Resolved — Receipt-only ticks protect option-policy trades

`_apply_buffered_spot_ticks_in_event_order()` (`main.py:165-188`) replaces the earlier receipt-only helper. For fresh receipt-only ticks (`0 <= age <= 5s`), `update_trades()` runs with full option-policy evaluation enabled (`include_option_policy=True`), protecting `option_atr_v1` trades against index stop crosses or detecting transitions. For stale receipt ticks (`age > 5s`), `include_option_policy=False, allow_t2_milestone=False` is enforced so out-of-date prices cannot advance option lifecycle state.

### Resolved — Event-order interleaving in mixed batches

In mixed batches (`ordered_ticks and receipt_ticks`), both `_sleep_with_tick_exits` (`main.py:321-325`) and `_fetch_candle_with_protection` (`main.py:546-550`) sort all buffered ticks by event time (`_buffered_spot_event_time`) and apply them in unified causal order before completed-bar replay (`_monitor_completed_atr`). When mixed batches are handled up front, `_monitor_completed_atr` receives `index_ticks=None` to avoid duplicate replay.

### Verification & Coverage

- `test_fetch_candle_protection_applies_receipt_only_tick_to_option_trade`: Verified receipt-only tick closes an active `option_atr_v1` trade upon hitting index stop loss.
- `test_mixed_tick_replay_keeps_exchange_stop_before_later_receipt_t1`: Verified earlier exchange stop is processed before later receipt-time T1 in a mixed batch.
- `test_new_policy_tick_uses_recent_receipt_and_skips_stale_lifecycle`: Verified fresh receipt tick evaluates option policy while stale receipt tick skips it.
- `test_receipt_tick_records_t2_before_later_completed_atr_close`: Verified receipt tick marks T2 before later ATR close.

## Confirmed Path

- The implementation rejects provider times more than five seconds ahead of receipt and preserves the price with receipt-time provenance (`fetchers/tick_feed.py:39-66, 76-90`).
- PositionManager independently rejects a far-future source time and can record T1 activation at receipt time (`position_manager.py:627-635, 941-965`).
- A future `atr_trailing.last_bar` is repaired from `activated_at` or the recorded T1 milestone, then completed bars after that anchor are replayed (`position_manager.py:1346-1391`).
- DhanHQ's Python SDK converts LTT epoch values to UTC `HH:MM:SS`; this supports rejecting an actually future timestamp rather than reinterpreting it as India local time ([official SDK source](https://github.com/dhan-oss/DhanHQ-py/blob/main/src/dhanhq/marketfeed.py)).

## Verification Limits

The QA report records 155 focused unit tests passing. This re-review inspected the revised call paths and tests but did not rerun them, run the full repository suite, or exercise live Dhan, Fly, or Supabase behavior. No implementation code or tests were changed in this review.
