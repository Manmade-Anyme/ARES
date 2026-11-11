# Problem Brief — TASK-20261111-option-t1-exit-price
**Date:** 2026-11-11
**Status:** approved

## Problem Statement
On live trade `#3574` (`25400 CE`, `security_id=45406`, `2026-11-11`), spot reached `T1` (`25390.15`) at `11:16:29.311037 IST` while live option premium `dhan_last_trade` in `option_trailing.last_option_quote` was already `237.50` (`11:16:29+05:30`). However:
1. `position_manager.py` never recorded incoming validated live option quotes into `_reference_price_evidence["option_quotes"]`, and `_enrich_reference_trade` did not consider `opt["last_option_quote"]`. Consequently `accounting.legs.T1.option_price` was `None` when `T1_HIT` committed.
2. Because `legs.T1.option_price` was `None`, `alerts.py` (`_option_lifecycle_estimate`) fell back to pre-trade projected `options_sizing.target_1` (`243.10`), reporting `Estimated Option Exit: ₹ 243.10 (+₹ 711.75 / lot)` on Discord instead of `237.50`.
3. Background `execution/reference_recovery.py` skipped `fetch_option_observation` after 5s and permanently froze DB `legs.T1.option_price = 239.00` (`11:16–11:17` `completed_1m_close`), booking `option_total_points = +2.975` after `STOPPED_OUT_AT_BE` (`231.25`) instead of `+2.225`.
4. `alerts.py` suppressed `Estimated Trade PnL` on `STOPPED_OUT_AT_BE` whenever `suggested_lots == 0`.
5. On live trade `#7258` (`25450 CE`, `security_id=45408`, `2026-11-11`), a transient Supabase/HTTP/2 transport error triggered `Database persistence failure. Memory state active; unconfirmed in database.` without retry or exception logging (and `position_manager.py` called `logger.*` without defining `logger`), while `[httpx]` `INFO` polling logs flooded container output.

## Approved Behavior & Scope
1. **Option Quote Reference Capture (`±10s`)**:
   - Add `record_reference_option_quote` on `PositionManager` and call it for validated incoming quotes in `update_option_market_events` before timeline replay (covering prior, same-second, and same-batch quotes).
   - Widen live option evidence matching in `_enrich_reference_trade` (`option_target_evidence(..., max_delta_seconds=10)`) and live quote lookup in `execution/reference_recovery.py` (`0 <= age <= 10`) to `±10s`, plus allow `opt["last_option_quote"]` as a candidate observation in `_enrich_reference_trade`.
2. **Discord Option Exit & PnL Alert Formatting (`alerts.py`)**:
   - On `T1_HIT` / `T2_HIT` / `T2_REACHED`, use captured `accounting.legs[T1/T2].option_price`. Never fall back to pre-trade synthetic `options_sizing.target_1` or `target_2`.
   - Option B fallback when leg price is absent: use `opt["last_option_quote"]` if within `<= 30s` of event time, tagged `(stale quote)`; otherwise render `unavailable`.
   - `Estimated Trade PnL` calculation: if `lots > 0`, multiply by `lots`; if `lots == 0` (or unavailable), compute reference PnL on a `1 lot` basis labeled clearly `(1-lot reference accounting; 0 lots suggested)` across `STOPPED_OUT_AT_BE` and closed multi-leg exits with `option_total_points`.
3. **Audited Backfill for `#3574`**:
   - Update closed trade `#3574` (`4fd271bf-ce10-5fa3-84d1-d116edefcbc4`) in Supabase `trade_analytics` (`legs.T1` `239.0 -> 237.5`, `option_source='dhan_last_trade'`, `approximate=false`, `option_booked_points=2.675`, `option_total_points=2.225`, `option_pnl_points=2.225`).
4. **Persistence Failure Hardening (`#7258`)**:
   - Disable HTTP/2 (`http2=False`, `httpx.Timeout(30.0, connect=10.0)`) on real Supabase clients in `storage.py`, serialize `AnalyticsLogger` PostgREST calls under `threading.RLock`, and retry transient transport/timeout/gateway errors (up to 3 attempts, releasing the lock during backoff) on idempotent option lifecycle/reference RPCs and state reads while failing non-transient errors (e.g. `40001` revision conflict) immediately.
   - Define module `logger` in `position_manager.py` and log `_flush_option_trade` persistence/conflict-reload failures via `logger.warning(...)`.
   - Suppress high-frequency `httpx`/`httpcore` `INFO` logs to `WARNING` in `system_one/consumer.py`.

## Acceptance Criteria
- Failing TDD tests reproduce `#3574` before fixes and pass after implementation.
- Existing 5s unit contracts on `option_target_evidence` remain unbroken by default parameter design.
- Full `pytest` suite passes with high coverage on modified paths; docs/CHANGELOG/Obsidian synced.
