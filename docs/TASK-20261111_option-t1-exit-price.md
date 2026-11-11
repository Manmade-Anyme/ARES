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

## 3. Persistence Failure Hardening (`#7258`)
- **Root Cause**:
  - `supabase-py` defaults `SyncPostgrestClient` to `httpx.Client(http2=True, timeout=120)` and only retries idempotent `GET`/`HEAD` `503`/`520` responses — never `POST` RPCs (`apply_option_lifecycle_transition`) or `httpx.TransportError`, even though `apply_option_lifecycle_transition` is idempotent on `p_event_id` (`migrations/20261104_option_premium_protection_v2.sql`).
  - Concurrent schedulers shared `AnalyticsLogger` across worker threads over a single HTTP/2 connection without serialization, while `position_manager.py` called `logger.*` without defining `logger` and `_flush_option_trade` dropped exception context before emitting `PERSISTENCE_FAILED`.
  - `system_one/consumer.py` enabled root `INFO` logging without dampening `httpx`/`httpcore`.
- **Changes Implemented**:
  - `storage.py`: Added `_configure_http1_client` (`http2=False`, `httpx.Timeout(30.0, connect=10.0)` on real `SyncSupabaseClient` instances across `get_supabase_client`, `Storage`, `AnalyticsLogger`, `PredictionLogger`), `_is_transient_rpc_error` (`httpx.TransportError`, `ConnectionError`, `TimeoutError`, and gateway HTTP statuses `408, 429, 500, 502, 503, 504, 520-524`), `AnalyticsLogger._supabase_lock` (`threading.RLock`), `_execute_with_retry` (up to 3 attempts, lock released during `0.15s * (attempt + 1)` backoff), `_rpc_execute`, and `_option_capability_ready` across option lifecycle/reference RPCs and `get_option_lifecycle_state`. Non-transient errors (`40001` revision conflict, `PermissionError`, `ValueError`, `RuntimeError`) fail immediately without retry.
  - `position_manager.py`: Added `import logging` + `logger = logging.getLogger(__name__)` and `logger.warning(...)` in `_flush_option_trade` on persistence failures and conflict reload errors.
  - `system_one/consumer.py`: Configured local HTTP/1.1 (`http2=False`, `30s/10s` timeout) in `_create_supabase_client()` (respecting module isolation from `storage.py`) and set `httpx`/`httpcore` loggers to `logging.WARNING` inside `run()`.
  - `storage.py` & `position_manager.py` follow-up: Extracted shared `execute_with_retry`, wrapped `PositionManager.supabase` and `load_dhan_credentials_from_supabase` with `_configure_http1_client`, added `_supabase_call` (`RLock` + retry) on `_persist_trade_context`, `sync_execution`, and `_record_trade_update` (while keeping concurrent `create_trade_entry_*` RPCs unlocked over HTTP/1.1 inside `_persist_trade_entry`'s 3-attempt loop), and extended `AnalyticsLogger` retry coverage to `log_exit` and `apply_option_reference_weekly_backfill`.
  - `alerts.py` follow-up: Normalized variable-precision PostgreSQL timestamps (`1-5` fractional digits, `+00`/`+HHMM` offsets) inside `_parse_alert_timestamp` for Python 3.10 (`Dockerfile`) compatibility, reused it in `_format_wall_event_time`, added `None`-safe `legs` lookups, and surfaced `0 lots suggested` in the legacy `STOPPED_OUT_AT_BE` branch.
  - Tests (`tests/unit/test_persistence_failure_resilience.py`, `tests/unit/test_task210_system_one.py`, `tests/unit/test_option_lifecycle_alert_cards.py`): Added HTTP/1.1 checks across all 6 Supabase client factories, `PositionManager`/`log_exit`/weekly-backfill retry tests, multi-thread lock-release backoff contention test, Python 3.10 strict ISO parsing test, and legacy zero-lot / `None`-legs alert guards.
