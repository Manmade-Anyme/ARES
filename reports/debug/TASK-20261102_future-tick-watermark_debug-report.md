# Debug Review — TASK-20261102 Future Tick Watermark

**Date:** 2026-11-03
**Scope:** PR #152 review findings for mixed tick/bar ordering and stale receipt timestamps, alongside the future-tick and post-T1 watermark protections.

## Findings addressed

### Mixed ticks and completed bars share one event timeline

`_sleep_with_tick_exits()` and `_fetch_candle_with_protection()` now pass accepted exchange-time and receipt-time observations into `_monitor_completed_atr()`. `_replay_spot_atr_with_ticks()` applies only completed spot candles through each tick's event time before applying that tick, then drains remaining completed candles. Option-policy replay receives the same ticks, and `PositionManager.update_option_market_events()` orders them with spot and premium candles. This prevents a later T2 tick from advancing a trade after an earlier completed ATR bar closed it.

Option-history requests use the initial observation time for the provider query, then receipt-tick freshness is evaluated against a new timestamp sampled after those requests finish. A slow history fetch cannot make an old receipt fallback appear fresh.

### Stale receipt observations retain their timestamps

Every buffered tick keeps its receipt time as `event_time`; accepted exchange timestamps remain separate `market_event_time` values. Stale receipt-only observations still reach ordinary spot protection with T2 milestone and option-policy progression disabled. Their original receipt time lets `PositionManager` exclude observations received before trade entry. Stale observations are also excluded from option-policy timeline replay.

## Regression coverage

- `test_protection_poll_replays_spot_atr_closes_between_buffered_ticks`: an earlier ATR stop closes before later exchange and receipt-only T2 observations.
- `test_stale_receipt_tick_before_entry_cannot_close_new_trade`: a stale below-stop observation from before entry cannot close the newer trade.
- `test_option_policy_uses_only_fresh_receipt_fallback_ticks`: fresh receipt ticks progress option policy; stale receipt ticks do not.
- `test_option_tick_freshness_uses_replay_time_after_history_fetch`: a receipt tick that ages past five seconds during an option-history fetch is evaluated at replay time.
- Mixed-batch protection tests verify exchange-time and receipt-time ticks reach the shared spot and option timelines rather than being applied ahead of candle replay.

## Verification

- Focused tick, position, lifecycle, and source-validation suite: **190 passed**.
- Session/manual tracking regressions: **50 passed**.
- Full repository suite: **2,133 passed, 12 warnings, 45 subtests**.
- `git diff --check`: passed.

## Limits

No live Dhan feed, Fly worker, or Supabase runtime was exercised. These results verify local replay and persistence tests only; they do not establish deployed-worker or live-market behavior.
