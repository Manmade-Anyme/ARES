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

2. **Linkage Disconnect**:
   - In `MLCollector.snapshot` (`ml_signal/collector.py`), `signal_id` is set to `str(signal.db_id)` (the `ares_signals.id` BIGSERIAL primary key).
   - In `PositionManager.add_trade` (`position_manager.py`), `trade_data["signal_id"]` is initialized to `signal.signal_id` (the 4-digit display string).
   - When `AnalyticsLogger.log_entry` (`storage.py`) creates a row in `trade_analytics`, it sets `"signal_id": getattr(signal, "db_id", None)`.
   - If `Storage.log_signal` fails or completes asynchronously after `PositionManager.add_trade`, `signal.db_id` is `None` when `trade_analytics` is written. This leaves `trade_analytics.signal_id = NULL` (an orphaned trade).
   - Furthermore, when `AnalyticsLogger.log_exit` fires, it attempts to back-fill `ml_collection` using `eq("signal_id", str(signal_id))`. If `trade_analytics.signal_id` is `NULL`, `log_exit` aborts early and does not update `ml_collection`.

3. **User & PM Directive**:
   - Per Project Manager and team instructions, linkage will utilize the **4-digit randomly generated trade/signal ID** (`AresSignal.signal_id`), which is synchronously available at signal creation, stored on `AresSignal`, passed into `active_trades`, and present across runtime alerts and telemetry.

---

## 2. Decision & Architectural Changes

To ensure 100% robust, deterministic, and fail-safe linkage between `ares_signals`, `active_trades`, `trade_analytics`, and `ml_collection`:

1. **4-Digit Signal ID Schema Standardization**:
   - Both `trade_analytics.signal_id` and `ml_collection.signal_id` will consistently store the 4-digit `signal.signal_id` (with optional fallback or compound keying `display_signal_id` / `db_id` when `db_id` is available).
   - `AresSignal` maintains its guaranteed 4-digit `signal_id` string upon instantiation (`models.py`).

2. **Fail-Safe Trade Analytics Logging**:
   - `AnalyticsLogger.log_entry` in `storage.py` will record `signal.signal_id` (the 4-digit string) in `trade_analytics.signal_id` even if `signal.db_id` has not yet resolved.
   - If `signal.db_id` exists, it will also be stored in a dedicated `signal_db_id` column or `market_context` metadata.

3. **Dual-Key / 4-Digit Reconciliation in ML Collection & Backfill**:
   - `MLCollector.snapshot` will write the 4-digit `signal_id` (`signal.signal_id`) to `ml_collection.signal_id`.
   - `AnalyticsLogger.log_exit` will back-fill `ml_collection` by matching on `signal_id` (4-digit code) or timestamp+setup fallback.
   - Update `ml_signal/backfill_labels.py` to include a 4-digit `signal_id` reconciliation phase that resolves the single orphaned trade among the 233 `trade_analytics` records to its corresponding `ml_collection` snapshot.

4. **Orphan Prevention & Retry Reconciliation**:
   - If `ml_collection` update fails during `log_exit`, a secondary background reconciliation job / function will retry linking unlinked `trade_analytics` rows to `ml_collection` using `(signal_id, setup_type, entry_timestamp)`.

---

## 3. Implementation Task Assignment for Code Generator Agent

Assign implementation of ticket **MANM-151** to the **Code Generator Agent** with the following explicit file tasks:

### Task Breakdown

1. **`models.py`**:
   - Ensure `AresSignal.signal_id` is always formatted as a non-null 4-digit string (`f"{random.randint(0, 9999):04d}"`).

2. **`storage.py` (`AnalyticsLogger`)**:
   - Update `log_entry` to populate `trade_analytics.signal_id` with `signal.signal_id` (4-digit ID), ensuring non-null linkage even if `signal.db_id` is `None`.
   - Update `log_exit` to attempt back-filling `ml_collection` using `signal_id` (4-digit display code) and fallback matching on `(setup_type, entry_timestamp)`.

3. **`ml_signal/collector.py` (`MLCollector`)**:
   - Update `snapshot` to record `signal.signal_id` (4-digit string) as `signal_id` in `ml_collection`.

4. **`ml_signal/backfill_labels.py`**:
   - Update `repair_orphan_trades` / `repair_join_key` to support 4-digit `signal_id` matching to isolate and link the single orphaned trade out of the 233 trades.

5. **`tests/unit/test_task151_trade_ml_linkage.py`**:
   - Add unit and integration tests verifying:
     a) Trade close writes complete ML outcome records to `ml_collection` using the 4-digit `signal_id`.
     b) Fallback reconciliation successfully links unlinked trades.

---

## 4. Definition of Done & Acceptance Criteria

- [ ] All 233 trades in `trade_analytics` are properly accounted for in `ml_collection`.
- [ ] Orphaned trade UUID identified and reconciled via 4-digit `signal_id`.
- [ ] Unit tests pass in `pytest`.
- [ ] No regression in signal generation or position management.
