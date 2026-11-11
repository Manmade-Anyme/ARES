# TASK-20261111: Live Option Price Capture at T1/T2 & 1-Lot Zero-Budget PnL Display

**Date:** 2026-11-11  
**Status:** Implemented & Verified  
**Branch:** `feature/TASK-20261111-option-t1-exit-price`

---

## 1. Root Cause (`#3574`)
On live trade `#3574` (`25400 CE`, `security_id=45406`):
- Spot touched `T1` (`25390.15`) at `11:16:29.311037 IST` while `option_trailing.last_option_quote` already held `₹ 237.50` (`dhan_last_trade`, `11:16:29+05:30`).
- `PositionManager` did not cache validated incoming option quotes into `_reference_price_evidence["option_quotes"]`, and `_enrich_reference_trade` did not include `last_option_quote`.
- Consequently `accounting.legs.T1.option_price` was `None` when `T1_HIT` committed, `alerts.py` fell back to synthetic pre-trade `options_sizing.target_1` (`243.10`), background `reference_recovery.py` skipped live quote fetch past `5s` and locked `completed_1m_close` (`239.00`), and `Estimated Trade PnL` was omitted whenever `lots == 0`.

## 2. Changes Implemented
1. **Option Quote Reference Capture (`position_manager.py`, `execution/reference_prices.py`, `execution/reference_recovery.py`)**:
   - Extracted shared `_normalize_reference_quote` and `_safe_quote_sort_key` and added `PositionManager.record_reference_option_quote` (`[-200:]` per `security_id`).
   - Updated `PositionManager.update_option_market_events` to call `record_reference_option_quote` for validated post-entry matching quotes before `_replay_option_timeline`.
   - Added keyword `max_delta_seconds=5` to `_nearest_observation` and `option_target_evidence`; passed `max_delta_seconds=10` and `opt.get("last_option_quote")` in `_enrich_reference_trade`; widened `reference_recovery.py` live option quote window from `5s` to `10s`.
2. **Discord Option Alert & PnL Formatting (`alerts.py`)**:
   - Passed `event_time` from `format_trade_update_payload` into `_option_lifecycle_estimate` and unified `T1_HIT` / `T2_HIT` / `T2_REACHED` lookup.
   - For versioned trades (`option_atr_v1`, `option_premium_protection_v2`), removed fallback to pre-trade projected `options_sizing.target_1` / `target_2`; added `_stale_quote_option_price` (`last_option_quote` within `<= 30s`, labeled `(stale quote)`), else `unavailable`.
   - Added `_reference_pnl_lots` so `STOPPED_OUT_AT_BE` and closed multi-leg exits compute 1-lot PnL when `lots == 0` (`(1-lot reference accounting; 0 lots suggested)`) and scale by `lots` when `lots > 0`.
3. **Supabase Historical Backfill (`#3574`)**:
   - Updated `trade_analytics` row `4fd271bf-ce10-5fa3-84d1-d116edefcbc4`: `legs.T1.option_price` `239.0 -> 237.5`, `option_source = "dhan_last_trade"`, `option_observed_at = "2026-11-11T11:16:29+05:30"`, `approximate = false`, `option_booked_points = 2.675`, `option_total_points = 2.225`, `option_pnl_points = 2.225` (`option_reference_pnl_points = 0.25` unchanged).

## 3. Separate Open Scope (`#7258`)
- Database persistence-failure hardening from trade `#7258` (`_flush_option_trade` exception logging, Supabase RPC retry/HTTP/2 locking, `[httpx]` INFO log suppression) is **not** included in this change set and remains separate.
