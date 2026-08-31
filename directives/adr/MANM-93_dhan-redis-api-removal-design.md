---
adr_id: "MANM-93"
title: "Dhan Redis API Removal & Local/Supabase State Architecture"
status: "proposed"
date: "2026-08-31"
author: "Software Architect Agent"
applies_to: "ARES Ingestion / Storage / Sizing / Engine"
---

# Architecture Decision Record: MANM-93 Dhan Redis API Removal Design

## 1. Status & Context
- **Status**: Proposed (Date: 2026-08-31)
- **Context**: 
  - The issue directives require producing an Architecture Decision Record (ADR) for completely removing the Dhan Redis API call (and any external Redis dependency in the Dhan market data path) without altering or degrading observable signal generation, tradebook/order processing, diagnostics, or failure visibility.
  - An audit of the `ARES` trading engine codebase reveals that `ARES` operates as a direct REST/WebSocket consumer of the DhanHQ API (`dhanhq`) and persists telemetry/signal/trade analytics to Supabase (`ares_signals`, `trade_analytics`, `ml_collection`, `api_keys`).
  - Analysis of callers, producers, and consumers shows that while no active Redis client exists in `ARES` core runtime (Dhan credentials and fund limits are loaded via Supabase REST and `dhanhq.get_fund_limits()`), any external Redis layer or auxiliary Dhan Redis cache in the architecture path introduces single-point-of-failure risks, unnecessary latency, and operational overhead.
  - Related prior ADRs: `directives/adr/TASK-005_dhan-pdh-pdl.md`, `directives/adr/TASK-004_supabase-rls-policy.md`, `directives/adr/TASK-007_trade-analytics-schema.md`.

## 2. Decision
We will enforce a zero-Redis architectural requirement for the Dhan API data path across all ingestion, market data, and account/sizing operations in `ARES`.

### Key Architectural Decisions:
1. **Direct Broker Ingestion & Local In-Memory Caching**:
   - Market data (1-minute intraday candles via `intraday_minute_data`, option chain via `option_chain`, and tick feed via `TickFeed` WebSocket) will continue to stream directly from Dhan HQ API into process-local, thread-safe/async memory structures (e.g., `PriceFetcher.cumulative_tp_vol`, `OIFetcher._prev_oi_snapshot`, `AresEngine.candle_buffer`).
2. **Account & Sizing Fallback (Supabase / Local State)**:
   - Account capital fetching (`fetch_dhan_capital` in `options_math.py`) will call `dhan_client.get_fund_limits()` directly in an executor thread. If `get_fund_limits()` fails or times out, the system will fall back to `settings.default_capital` (configurable via environment/Supabase metadata) without throwing unhandled exceptions or querying Redis.
3. **Credentials Management**:
   - Dhan credentials (`client_id`, `access_token`) are sourced directly from Supabase (`api_keys` table via `load_dhan_credentials_from_supabase()`). In case of HTTP 401 / authentication failure, credentials are reloaded from Supabase in real-time.
4. **Preservation of Signal Contract & Order Analytics**:
   - Signal generation contracts (`AresSignal`, `OHLCVCandle`, `ATMStrikes`, `SetupLevels`) remain 100% deterministic and unaffected by storage/cache layer changes.
   - All signal emissions and trade analytics exits continue to log asynchronously to Supabase (`ares_signals`, `trade_analytics`, `ml_collection`).

### System Architecture & Data Flow Diagram

```
[ DhanHQ API / WS ] ──(Direct REST / WS)──▶ [ Ingestion Layer ]
                                                  │
                                                  ▼
                                       [ Local Memory Buffers ]
                                       (Candles / VWAP / OI Snapshot)
                                                  │
                                                  ▼
                                        [ AresEngine & Detectors ]
                                                  │
                                                  ▼
                                        [ Signal & Trade Execution ]
                                                  │
                                                  ▼
                                      [ Storage Layer (Supabase) ]
                                  (ares_signals, trade_analytics, ml_collection)
```

## 3. Interface & Contract Specifications

### Ingestion & Sizing Contracts
- `PriceFetcher.fetch_latest_candle() -> OHLCVCandle`: Directly queries Dhan `intraday_minute_data`, accumulates VWAP locally in instance state `self.cumulative_tp_vol` and `self.cumulative_vol`.
- `OIFetcher.fetch_chain(spot_price, expiry) -> Tuple[ATMStrikes, List[Dict[str, Any]]]`: Fetches option chain via Dhan `option_chain`, tracks previous cycle OI in `self._prev_oi_snapshot` (dictionary in process memory).
- `options_math.fetch_dhan_capital(dhan_client) -> float`: Queries `dhan_client.get_fund_limits()`. On failure, falls back to `float(settings.default_capital)`.

### State Consistency & Thread Safety
- **Single Event Loop Concurrency**: `ARES` runs on a single asyncio event loop (`main.py`). All state mutations (`candle_buffer`, `_prev_oi_snapshot`, `active_trades`) occur sequentially within co-routines on the main loop.
- **Async Executors**: Blocking Dhan SDK synchronous calls (`historical_daily_data`, `intraday_minute_data`, `option_chain`, `get_fund_limits`) run in `loop.run_in_executor(None, ...)` to prevent blocking the asyncio event loop.
- **Idempotency**: Signal logging generates a database ID (`db_id`) which is attached to `AresSignal` and used as the foreign key `signal_id` in `trade_analytics` and `ml_collection`.

## 4. Failure Modes, Observability & Rollback Strategy

### Failure Modes & Mitigations
| Failure Scenario | Local / Supabase Behavior | System Impact |
|---|---|---|
| Dhan API Transient Error / Timeout | Retry loop (max 3 retries, exponential backoff) in `fetch_latest_candle` / `fetch_chain` | Temporary pause in cycle execution; no state corruption |
| Dhan 401 Auth Failure | Catch 401 -> call `load_dhan_credentials_from_supabase()` -> re-init SDK client | Automatic recovery within 60 seconds |
| `get_fund_limits()` API Failure | Exception caught -> returns `settings.default_capital` | Position sizing falls back safely to default capital |
| Supabase Persistence Outage | Async background tasks swallow DB write exceptions (`log_signal`, `log_entry`) | Core trading loop continues generating signals uninterrupted |

### Diagnostics & Log Cross-Comparison
- **Local Terminal Output**: Colorized ANSI console logs print real-time signal detections, VWAP resets, heartbeats, and error alerts.
- **Fly.io Production Logs**: `fly logs` captures stdout/stderr. Terminal UI outputs match structured heartbeat logs: `💓 HEARTBEAT: Spot=... | Buffers=... | ML Snapshots=...`.
- **Supabase Telemetry**: `ares_signals` and `trade_analytics` rows are compared against Fly logs using `created_at` / `timestamp` ISO 8601 UTC strings (`to_utc_iso()`).

### Rollback Strategy
- Since the architecture eliminates external Redis dependencies in favor of local memory state and Supabase persistence, rollback consists of deploying the stable container revision (`fly deploy`) or reverting code changes in `fetchers/` and `options_math.py`.

## 5. Testing & Verification Checklist

- [ ] **Unit Tests**: Run `pytest tests/` to ensure 100% pass rate across `test_price_fetcher.py`, `test_oi_fetcher.py`, `test_options_math.py`, `test_storage.py`.
- [ ] **Redis Absence Verification**: Verify `grep -rn "redis" .` in `ARES` returns no active client imports or connections in production code.
- [ ] **Fund Limits Fallback Test**: Mock `dhan_client.get_fund_limits()` to raise an exception or return failure; verify `fetch_dhan_capital()` returns `settings.default_capital`.
- [ ] **Signal Integrity**: Verify `AresEngine.tick()` produces bit-identical `AresSignal` objects before and after Redis removal design application.
- [ ] **Supabase Verification**: Verify `log_signal()` populates `db_id` and links correctly to `trade_analytics` and `ml_collection`.

## 6. Implementation & QA Handoff Notes

### For Code Generator Agent:
1. Ensure `PriceFetcher`, `OIFetcher`, and `options_math.py` retain zero Redis imports or Redis helper calls.
2. Confirm `fetch_dhan_capital` in `options_math.py` cleanly handles any Dhan API response structure or exception by returning `settings.default_capital`.
3. Keep process-local caching structures (`_prev_oi_snapshot`, `cumulative_tp_vol`, `_cached_expiry`) in instance memory.

### For QA Agent:
1. Run full test suite: `pytest tests/unit/`.
2. Inspect log outputs to verify no Redis connection attempts or missing dependency errors surface.
3. Validate signal generation against historic test fixtures (`test_task188_oi_wall_wick_gate.py`, etc.).
