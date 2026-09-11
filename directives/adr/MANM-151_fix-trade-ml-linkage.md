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

2. **Linkage Disconnect & Executor Race Condition (P1 Review Finding)**:
   - In `MLCollector.snapshot` (`ml_signal/collector.py`), `signal_id` is set to `str(signal.db_id)` (the `ares_signals.id` BIGSERIAL primary key).
   - In `PositionManager.add_trade` (`position_manager.py`), `trade_data["signal_id"]` is initialized to `signal.signal_id` (the 4-digit display string).
   - When `AnalyticsLogger.log_entry` (`storage.py`) creates a row in `trade_analytics`, it sets `"signal_id": getattr(signal, "db_id", None)`.
   - If `Storage.log_signal` fails or completes asynchronously after `PositionManager.add_trade`, `signal.db_id` is `None` when `trade_analytics` is written. This leaves `trade_analytics.signal_id = NULL` (an orphaned trade).
   - **Main-Loop Candle Race Condition**: When a trade opens and closes within the same candle or consecutive fast ticks, `PositionManager.add_trade` schedules `log_entry` on an executor thread before `update_trades` schedules `log_exit`. `log_exit` currently performs an immediate query `.select(...).eq("id", trade_id)`. If `log_entry` has not finished writing to PostgreSQL, `log_exit` finds no row and returns permanently. Consequently, `trade_analytics` remains in `result_state = 'OPEN'`, and `log_exit`'s `ml_collection` backfill is permanently skipped.

3. **Database Schema & Documentation Incompatibility (P2 Review Finding)**:
   - `schema.sql` and `README.md` (lines 401 & 223) currently define `trade_analytics.signal_id` as `bigint`.
   - `ml_collection.signal_id` is defined as `text`.
   - Storing a zero-padded 4-digit display string (e.g., `"0007"`) in a `bigint` column causes PostgreSQL to cast or strip leading zeros to `7`. When joining or querying `ml_collection.signal_id` (`text`), `"0007"` != `"7"`, breaking linkage for ~10% of generated codes in fresh deployments initialized from `README.md`.

4. **Break-Even P&L Repair Disconnect (P1 Review Finding)**:
   - `repair_be_after_t1` in `ml_signal/backfill_labels.py` currently looks up `trade_analytics.signal_id` directly in `ares_signals.id` (`BIGSERIAL`) when `active_trades` has no row.
   - If `trade_analytics.signal_id` is changed to store the 4-digit display code, `repair_be_after_t1` will fail or mistakenly match an unrelated signal whose serial `id` happens to equal the 4-digit integer value.
   - Preserving the exact database primary key (`ares_signals.id`) is mandatory for `repair_be_after_t1` to query `ares_signals.target_1` accurately.

5. **User & PM Directive**:
   - Per Project Manager and team instructions, linkage will utilize the **4-digit randomly generated trade/signal ID** (`AresSignal.signal_id`), which is synchronously available at signal creation, stored on `AresSignal`, passed into `active_trades`, and present across runtime alerts and telemetry.

---

## 2. Decision & Architectural Changes

To ensure 100% robust, deterministic, and fail-safe linkage between `ares_signals`, `active_trades`, `trade_analytics`, and `ml_collection`:

1. **4-Digit Signal ID Schema Standardization, Migration & Documentation Sync**:
   - Create a database migration script `migrations/2026-09-11-task151-trade-analytics-signal-id-text.sql` to alter `trade_analytics.signal_id` from `bigint` to `text` (`ALTER TABLE trade_analytics ALTER COLUMN signal_id TYPE text;`).
   - Update `schema.sql` and `README.md` (lines 223 & 401) to specify `trade_analytics.signal_id text` (matching `active_trades.signal_id` and `ml_collection.signal_id`).
   - Both `trade_analytics.signal_id` and `ml_collection.signal_id` will consistently store the string representation of the 4-digit `signal.signal_id` (e.g., `"0007"`), preserving leading zeros across new and existing deployments.

2. **Preserving Database Primary Key in Metadata**:
   - Update `AnalyticsLogger.log_entry` in `storage.py` to record `signal.signal_id` (4-digit text string) in `trade_analytics.signal_id`.
   - Simultaneously preserve the database primary key `signal.db_id` in `market_context` metadata (`market_context["signal_db_id"]`) whenever available.
   - Update `repair_be_after_t1` in `ml_signal/backfill_labels.py` to read `market_context ->> 'signal_db_id'` when resolving `ares_signals` for BE target lookups, falling back safely to timestamp/setup matching instead of querying `ares_signals.id` with the 4-digit display code.

3. **Serialized Entry/Exit Persistence & Entry-Race Reconciliation**:
   - Update `AnalyticsLogger.log_exit` to implement exponential backoff retry (e.g., 3 retries with 100ms delay) when the initial `trade_analytics.select()` query for `trade_id` returns no row, allowing async `log_entry` execution to complete.
   - Expand `ml_signal/backfill_labels.py` / background reconciliation to detect trades stuck in `result_state = 'OPEN'` that exist in `active_trades` as `CLOSED`/`STOPPED_OUT` or match closed signals, repairing the `trade_analytics` exit state and backfilling `ml_collection`.

4. **Dual-Key / 4-Digit Reconciliation in ML Collection & Backfill**:
   - `MLCollector.snapshot` will write the 4-digit `signal_id` (`signal.signal_id`) to `ml_collection.signal_id`.
   - `AnalyticsLogger.log_exit` will back-fill `ml_collection` by matching on `signal_id` (4-digit code) or timestamp+setup fallback.
   - Update `ml_signal/backfill_labels.py` to include 4-digit `signal_id` reconciliation across `repair_orphan_trades`, `repair_join_key`, and `repair_be_after_t1`.

5. **Orphan Prevention & Retry Reconciliation**:
   - If `ml_collection` update fails during `log_exit`, a secondary background reconciliation job / function will retry linking unlinked `trade_analytics` rows to `ml_collection` using `(signal_id, setup_type, entry_timestamp)`.

---

## 3. Implementation Task Assignment for Code Generator Agent

Assign implementation of ticket **MANM-151** to the **Code Generator Agent** with the following explicit file tasks:

### Task Breakdown

1. **`migrations/2026-09-11-task151-trade-analytics-signal-id-text.sql`**, **`schema.sql`**, & **`README.md`**:
   - Add migration script: `ALTER TABLE trade_analytics ALTER COLUMN signal_id TYPE text;`.
   - Update `schema.sql` definition of `trade_analytics.signal_id` to `text`.
   - Update `README.md` lines 223 & 401 to reflect 4-digit display code tracking and `signal_id text` schema definition.

2. **`models.py`**:
   - Ensure `AresSignal.signal_id` is always formatted as a non-null 4-digit string (`f"{random.randint(0, 9999):04d}"`).

3. **`storage.py` (`AnalyticsLogger`)**:
   - Update `log_entry` to populate `trade_analytics.signal_id` with `signal.signal_id` (4-digit string).
   - Store `signal.db_id` inside `market_context["signal_db_id"]` if non-null.
   - Update `log_exit` to include retry logic (3 retries with 100ms pause) if `select("entry_price", "direction", "signal_id").eq("id", trade_id)` initially returns empty due to concurrent `log_entry` execution.
   - Backfill `ml_collection` using `signal_id` (4-digit display code) and fallback matching on `(setup_type, entry_timestamp)`.

4. **`ml_signal/collector.py` (`MLCollector`)**:
   - Update `snapshot` to record `signal.signal_id` (4-digit string) as `signal_id` in `ml_collection`.

5. **`ml_signal/backfill_labels.py`**:
   - Update `repair_orphan_trades` / `repair_join_key` to support 4-digit `signal_id` matching.
   - Update `repair_be_after_t1` to extract `market_context ->> 'signal_db_id'` for `ares_signals` lookup, preventing misattribution or invalid lookup against `ares_signals.id`.
   - Add reconciliation support for `OPEN` trades in `trade_analytics` whose entry write raced `log_exit`.

6. **`tests/unit/test_task151_trade_ml_linkage.py`**:
   - Add unit and integration tests verifying:
     a) Column data type compatibility for 4-digit string IDs with leading zeros (e.g. `"0007"`).
     b) `repair_be_after_t1` correctly uses `market_context["signal_db_id"]` without misinterpreting 4-digit display strings as serial IDs.
     c) Fast-entry/exit race condition: `log_exit` successfully retries and updates `trade_analytics` and `ml_collection` when `log_entry` completes asynchronously.
     d) Fresh schema setup from `README.md` / `schema.sql` creates `trade_analytics.signal_id` as `text`.
     e) Trade close writes complete ML outcome records to `ml_collection` using the 4-digit `signal_id`.
     f) Fallback reconciliation successfully links unlinked trades.

---

## 4. Definition of Done & Acceptance Criteria

- [ ] Schema migration created altering `trade_analytics.signal_id` to `text`.
- [ ] `schema.sql` and `README.md` documentation updated to define `trade_analytics.signal_id text`.
- [ ] `AnalyticsLogger.log_exit` retries when racing concurrent `log_entry` inserts.
- [ ] `repair_be_after_t1` updated to use `market_context["signal_db_id"]` preserving database primary key lookups.
- [ ] All 233 trades in `trade_analytics` are properly accounted for in `ml_collection`.
- [ ] Orphaned trade UUID identified and reconciled via 4-digit `signal_id`.
- [ ] Unit tests pass in `pytest`.
- [ ] No regression in signal generation or position management.
