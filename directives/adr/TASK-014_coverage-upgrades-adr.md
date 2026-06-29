# Architecture Decision Record — TASK-014 (Core Modules 100% Coverage Upgrades)

## Problem Statement
The core trading, sizing, and alerting layers of ARES currently average ~59% test coverage. To ensure system stability and meet the target of 100% test coverage for Phase 1 modules without modifying any production business logic, we must design structured, resilient mock-based unit tests for all uncovered execution branches (specifically exception handling, fallback logic, and client integrations).

## Target Files and Missing Line Coverage
1. **`alerts.py` (55% -> 100%)**
   - *Untested*: Webhook exception handling, HTTP client network errors, and specialized alert formatting (e.g. startup banners and error notifications).
2. **`detectors/breakout.py` (90% -> 100%)**
   - *Untested*: Real breakouts holding configuration periods (state reset), target edge-case branches, and short-circuit conditions.
3. **`detectors/exhaustion.py` (91% -> 100%)**
   - *Untested*: Zero candle range handling, early queue warm-up returns, and target fallbacks.
4. **`detectors/expiry_detector.py` (47% -> 100%)**
   - *Untested*: Expiry cache hit checks, Dhan API weekly expiry fetching success/failure states, and the Tuesday weekly fallback logic.
5. **`detectors/oi_wall.py` (51% -> 100%)**
   - *Untested*: `detect()` method scanning option chains for nearest CE/PE walls, verifying distance bounds, candle high/low testing, and option writer defense confirmations.
6. **`options_math.py` (71% -> 100%)**
   - *Untested*: Greece/Delta fallbacks, zero premium edge cases, and client error handling.
7. **`position_manager.py` (30% -> 100%)**
   - *Untested*: DB updates, Supabase trade persistence on exits, and memory trade log cleanup.
8. **`storage.py` (29% -> 100%)**
   - *Untested*: Database logging inserts and Supabase network exception handling.

## Testing & Mocking Decisions

### 1. Mocking External HTTP Clients & Webhooks
- Use `unittest.mock.patch` to intercept all `httpx.AsyncClient` calls inside `alerts.py`.
- Verify error pathways (network down, 4xx/5xx responses) do not propagate exceptions outside of the alert wrappers.

### 2. Mocking Dhan API Weekly Expiries
- Mock `dhanhq` client context responses inside `test_expiry_detector.py`.
- Test transient retries (first 2 calls fail, 3rd succeeds) and permanent failures that activate the Tuesday Weekly Fallback logic.

### 3. Mocking Supabase Client Database Transactions
- Mock `supabase.table().insert()`, `.update()`, and `.select()` calls inside `storage.py` and `position_manager.py`.
- Test database-down scenarios to ensure ARES handles DB exceptions gracefully without crashing the main orchestration loop.

### 4. TDD / Assertions
- All assertions will verify observable outcomes (Signal properties, alerts raised, memory trade status updates, DB payloads) and not mock call counts or internal implementation details.

## Definition of Done
- Line and branch coverage of the 8 target files reaches 100% (or closest maximum, ignoring unreachable code comments).
- All 56 existing tests continue to pass.
