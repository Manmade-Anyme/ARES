# ADR-151: Fix Trade ML Linkage via 4-Digit Display Signal ID Reconciliation

- **Status**: Proposed
- **Date**: 2026-09-11
- **Task ID**: MANM-151
- **Author**: Software Architect Agent (`d2d4e328-096d-4658-8d90-44aa7b51ed05`)

---

## 1. Executive Summary & Root Cause Analysis

### Problem Statement
`trade_analytics` records 233 trades, whereas `ml_collection` records only 232 unique linked trade IDs. Exactly one trade is orphaned and lacks ML linkage in `ml_collection`.

### Root Cause
Investigation into the ARES signal logging, position management, and ML collection pipeline reveals:

1. **Dual Signal Identifier Model**:
   - `ares_signals.id`: PostgreSQL BIGSERIAL primary key (e.g. `243`).
   - `AresSignal.signal_id`: 4-digit randomly generated display string (e.g. `"4829"` via `f"{random.randint(0, 9999):04d}"`).
   - `active_trades.signal_id`: Stores the 4-digit `signal_id`.

2. **Linkage Disconnect & Executor Race Condition**:
   - In `MLCollector.snapshot` (`ml_signal/collector.py`), `signal_id` is set to `str(signal.db_id)` (the `ares_signals.id` BIGSERIAL primary key).
   - In `PositionManager.add_trade` (`position_manager.py`), `trade_data["signal_id"]` is initialized to `signal.signal_id` (the 4-digit display string).
   - When `AnalyticsLogger.log_entry` (`storage.py`) creates a row in `trade_analytics`, it sets `"signal_id": getattr(signal, "db_id", None)`.
   - If `Storage.log_signal` fails or completes asynchronously after `PositionManager.add_trade`, `signal.db_id` is `None` when `trade_analytics` is written. This leaves `trade_analytics.signal_id = NULL` (an orphaned trade).
   - **Main-Loop Candle Race Condition**: When a trade opens and closes within the same candle or consecutive fast ticks, `PositionManager.add_trade` schedules `log_entry` on an executor thread before `update_trades` schedules `log_exit`. `log_exit` currently performs an immediate query `.select(...).eq("id", trade_id)`. If `log_entry` has not finished writing to PostgreSQL, `log_exit` finds no row and returns permanently. Furthermore, `active_trades` historically only persists `{"state": trade["state"], "stop_loss": trade["stop_loss"]}` on state change, meaning terminal event details (`exit_price`, `exit_timestamp`, `exit_type`, `pnl_points_override`) were lost if `log_exit` failed. Consequently, `trade_analytics` remains in `result_state = 'OPEN'`, and `log_exit`'s `ml_collection` backfill is permanently skipped.

3. **Database Schema & Documentation Incompatibility**:
   - `schema.sql` and `README.md` (lines 401 & 223) currently define `trade_analytics.signal_id` as `bigint`.
   - `ml_collection.signal_id` is defined as `text`.
   - Storing a zero-padded 4-digit display string (e.g., `"0007"`) in a `bigint` column causes PostgreSQL to cast or strip leading zeros to `7`. In PostgreSQL, altering a populated `bigint` column to `text` requires an explicit `USING signal_id::text` clause, or the migration will fail with a syntax/cast error.

4. **Outcome Collision & Label Corruption Risk in `ml_collection`**:
   - 4-digit display IDs have a domain of only 10,000 values ($[0000, 9999]$). Across 233+ trades, the collision probability exceeds 93.4%.
   - Updating `ml_collection` using bare `.eq("signal_id", signal_id)` will overwrite all historical snapshots sharing the same 4-digit code, corrupting prior trade outcomes with newer trades.

5. **Break-Even P&L Repair Disconnect**:
   - `repair_be_after_t1` in `ml_signal/backfill_labels.py` currently looks up `trade_analytics.signal_id` directly in `ares_signals.id` (`BIGSERIAL`) when `active_trades` has no row.
   - If `trade_analytics.signal_id` is changed to store the 4-digit display code, `repair_be_after_t1` will fail or mistakenly match an unrelated signal whose serial `id` happens to equal the 4-digit integer value.
   - Preserving the exact database primary key (`ares_signals.id`) is mandatory for `repair_be_after_t1` to query `ares_signals.target_1` accurately.

6. **Existing Unit Test Invariant Scope**:
   - Tests in `tests/unit/test_task194_ml_labels_and_oi_distribution.py`, `tests/unit/test_storage.py`, `tests/unit/test_task195_orphan_and_sentinel_repair.py`, and `tests/unit/test_manm66_be_pnl_repair.py` assert legacy BigSerial/integer `signal_id` invariants.
   - All 4 test files must be migrated to the new text/4-digit invariant to maintain 100% test suite pass rate.

7. **User & PM Directive**:
   - Per Project Manager and team instructions, linkage will utilize the **4-digit randomly generated trade/signal ID** (`AresSignal.signal_id`), which is synchronously available at signal creation, stored on `AresSignal`, passed into `active_trades`, and present across runtime alerts and telemetry.

---

## 2. Decision & Architectural Changes

To ensure 100% robust, deterministic, and fail-safe linkage between `ares_signals`, `active_trades`, `trade_analytics`, and `ml_collection`:

1. **4-Digit Signal ID Schema Standardization, Migration & Documentation Sync**:
   - Create a database migration script `migrations/2026-09-11-task151-trade-analytics-signal-id-text.sql` with explicit cast:
     ```sql
     ALTER TABLE trade_analytics ALTER COLUMN signal_id TYPE text USING signal_id::text;
     ```
   - Update `schema.sql` and `README.md` (lines 223 & 401) to specify `trade_analytics.signal_id text` (matching `active_trades.signal_id` and `ml_collection.signal_id`).
   - Both `trade_analytics.signal_id` and `ml_collection.signal_id` will consistently store the string representation of the 4-digit `signal.signal_id` (e.g., `"0007"`), preserving leading zeros across new and existing deployments.

2. **Preserving Database Primary Key in Metadata**:
   - Update `AnalyticsLogger.log_entry` in `storage.py` to record `signal.signal_id` (4-digit text string) in `trade_analytics.signal_id`.
   - Simultaneously preserve the database primary key `signal.db_id` in `market_context` metadata (`market_context["signal_db_id"]`) whenever available.
   - Update `repair_be_after_t1` in `ml_signal/backfill_labels.py` to read `market_context ->> 'signal_db_id'` when resolving `ares_signals` for BE target lookups, falling back safely to timestamp/setup matching instead of querying `ares_signals.id` with the 4-digit display code.

3. **Collision-Resistant ML Collection Backfill**:
   - When updating `ml_collection` during `log_exit` or backfill, never filter on bare `signal_id` alone.
   - Use composite predicates:
     - `signal_id = <4-digit-id>`
     - `trade_id IS NULL` (ensures previously closed trades sharing the same 4-digit code are never overwritten)
     - Timestamp correlation window (within engine evaluation tolerance of `entry_timestamp`) and setup matching (`signal_setup_type = <setup>`).

4. **Serialized Entry/Exit Persistence & Terminal State Telemetry**:
   - Update `PositionManager.update_trades` (`position_manager.py`) to persist terminal telemetry (`exit_price`, `exit_timestamp`, `exit_type`, `pnl_points_override`) into `active_trades` upon trade exit. This ensures background reconciliation has full, ground-truth telemetry to reconstruct any `trade_analytics` exit that raced `log_entry`.
   - Update `AnalyticsLogger.log_exit` to implement exponential backoff retry (3 retries with 100ms delay) when the initial `trade_analytics.select()` query for `trade_id` returns no row.
   - Update `ml_signal/backfill_labels.py` reconciliation to repair trades stuck in `OPEN` in `trade_analytics` by reading the persisted terminal telemetry from `active_trades` and backfilling `ml_collection`.

5. **Multi-Phase Backfill & Orphan Resolution**:
   - Update `ml_signal/backfill_labels.py` to support 4-digit `signal_id` matching with collision-guarding across `repair_orphan_trades`, `repair_join_key`, and `repair_be_after_t1`.

---

## 3. Implementation Task Assignment for Code Generator Agent

Assign implementation of ticket **MANM-151** to the **Code Generator Agent** with the following explicit file tasks:

### Task Breakdown

1. **`migrations/2026-09-11-task151-trade-analytics-signal-id-text.sql`**, **`schema.sql`**, & **`README.md`**:
   - Add migration script:
     ```sql
     ALTER TABLE trade_analytics ALTER COLUMN signal_id TYPE text USING signal_id::text;
     ```
   - Update `schema.sql` definition of `trade_analytics.signal_id` to `text`.
   - Update `README.md` lines 223 & 401 to reflect 4-digit display code tracking and `signal_id text` schema definition.

2. **`models.py`**:
   - Ensure `AresSignal.signal_id` is always formatted as a non-null 4-digit string (`f"{random.randint(0, 9999):04d}"`).

3. **`position_manager.py`**:
   - Update `update_trades` when a trade closes (`CLOSED`/`STOPPED_OUT`) to persist terminal state telemetry into `active_trades`:
     `{"state": trade["state"], "stop_loss": trade["stop_loss"], "exit_price": event_price, "exit_type": update_type, "exit_timestamp": datetime.now(timezone.utc).isoformat(), "pnl_points_override": pnl_points_override}`.

4. **`storage.py` (`AnalyticsLogger`)**:
   - Update `log_entry` to populate `trade_analytics.signal_id` with `signal.signal_id` (4-digit string).
   - Store `signal.db_id` inside `market_context["signal_db_id"]` if non-null.
   - Update `log_exit` to include retry logic (3 retries with 100ms pause) if `select("entry_price", "direction", "signal_id").eq("id", trade_id)` initially returns empty.
   - In `log_exit`, backfill `ml_collection` using collision-resistant composite filters (`eq("signal_id", str(signal_id)).is_("trade_id", "null")` plus setup match) instead of bare `signal_id`.

5. **`ml_signal/collector.py` (`MLCollector`)**:
   - Update `snapshot` to record `signal.signal_id` (4-digit string) as `signal_id` in `ml_collection`.

6. **`ml_signal/backfill_labels.py`**:
   - Update `repair_orphan_trades` / `repair_join_key` to support 4-digit `signal_id` matching with collision-guarding.
   - Update `repair_be_after_t1` to extract `market_context ->> 'signal_db_id'` for `ares_signals` lookup, preventing misattribution or invalid lookup against `ares_signals.id`.
   - Add reconciliation support for `OPEN` trades in `trade_analytics` by extracting terminal state telemetry from `active_trades`.

7. **Test Suite Migration & Expansion**:
   - Update legacy test assertions to expect 4-digit string `signal_id` in:
     - `tests/unit/test_task194_ml_labels_and_oi_distribution.py`
     - `tests/unit/test_storage.py`
     - `tests/unit/test_task195_orphan_and_sentinel_repair.py`
     - `tests/unit/test_manm66_be_pnl_repair.py`
   - Add new unit and integration test file `tests/unit/test_task151_trade_ml_linkage.py` verifying:
     a) PostgreSQL migration SQL executes `USING signal_id::text`.
     b) Collision resistance: `ml_collection` update targets only the matching unlabelled snapshot without corrupting prior trades with colliding 4-digit IDs.
     c) Terminal telemetry persistence in `active_trades` and successful reconciliation of raced `OPEN` trades.
     d) Column data type compatibility for 4-digit string IDs with leading zeros (e.g. `"0007"`).
     e) `repair_be_after_t1` correctly uses `market_context["signal_db_id"]`.
     f) Fresh schema setup from `README.md` / `schema.sql` creates `trade_analytics.signal_id` as `text`.
     g) 100% pass rate across entire test suite (`pytest`).

---

## 4. Definition of Done & Acceptance Criteria

- [ ] Schema migration created altering `trade_analytics.signal_id` to `text` using `USING signal_id::text`.
- [ ] `schema.sql` and `README.md` documentation updated to define `trade_analytics.signal_id text`.
- [ ] `active_trades` updated to persist terminal exit telemetry on closure.
- [ ] `AnalyticsLogger.log_exit` retries when racing concurrent `log_entry` inserts.
- [ ] `ml_collection` update uses collision-resistant composite filters (`signal_id`, `trade_id IS NULL`, setup/timestamp).
- [ ] `repair_be_after_t1` updated to use `market_context["signal_db_id"]` preserving database primary key lookups.
- [ ] Existing tests in `test_task194`, `test_storage`, `test_task195`, and `test_manm66` updated to reflect 4-digit `signal_id` invariant.
- [ ] All 233 trades in `trade_analytics` are properly accounted for in `ml_collection`.
- [ ] Orphaned trade UUID identified and reconciled via 4-digit `signal_id`.
- [ ] 100% of unit and integration tests pass in `pytest`.
- [ ] No regression in signal generation or position management.
