# ADR-153: Machine Learning Prediction Persistence in `ml_predictions` for Model Auditing & Drift Monitoring

- **Status**: Proposed
- **Date**: 2026-09-12
- **Task ID**: TASK-153 / MANM-153
- **Author**: Software Architect Agent (`d2d4e328-096d-4658-8d90-44aa7b51ed05`)
- **Target Components**: `storage.py`, `models.py`, `main.py`, `position_manager.py`, `ml_signal/live.py`, `ml_signal/signal_consumer.py`, `schema.sql`, `ml_signal/schema.sql`, `migrations/`

---

## 1. Executive Summary & Problem Statement

### Problem Statement
The `ml_predictions` table in Supabase currently contains 0 rows in production. While production ARES currently uses the in-process `SignalPredictor` (`ml_signal/predictor.py`) inside `main.py` to score fired trading signals and enrich live Discord alerts with forward win probabilities (e.g. `🤖 ML Prediction: 46% (MEDIUM)`), **predictions are never persisted to the database**.

### Impact of Missing Persistence
1. **Model Auditing Gap**: There is zero historical traceability of model outputs, model versions, and feature inputs at the exact instant trade decisions are made.
2. **Calibration Inability**: Without persisted probabilities mapped to trade executions, ARES cannot construct reliability diagrams, calculate Brier scores, or calibrate forward probability against realized win rates.
3. **Drift & Concept Degradation**: Distributional shifts in market volatility, Open Interest, or price action cannot be evaluated across model versions without an immutable audit trail of inference features.
4. **Threshold Optimization Blocked**: Dynamic threshold tuning for confidence tiers (`HIGH`, `MEDIUM`, `LOW`) requires historical inference distribution data that does not exist.

### Objectives
1. Persist model predictions to `ml_predictions` in Supabase on **every inference event**.
2. Capture all required audit fields: `timestamp`, `probability`, `confidence_tier`, `model_version`, `signal_id`, `trade_id` (when available), `spot`, `source`, and `feature_snapshot`.
3. Implement a **100% non-blocking, asynchronous** logging mechanism: Supabase latency, network failure, or JSON serialization issues must **never** block, delay, or crash the core trading loop.
4. Provide unit and integration test specifications covering persistence success, graceful error suppression, and JSON sanitization.
5. Establish a formal data retention and privacy policy.

---

## 2. Database Schema & Migration Specification

### Current State vs Required State
In `ml_signal/schema.sql`, the existing table definition contains:
- `features jsonb` (field name mismatch with PM directive `feature_snapshot`)
- `trade_id uuid`
- Missing foreign key indexes for `trade_id` and `model_version`
- Not included in the root `schema.sql`

### Standardized `ml_predictions` Schema
The table schema must be standardized across both root `schema.sql` and `ml_signal/schema.sql`:

```sql
CREATE TABLE IF NOT EXISTS ml_predictions (
  id bigserial primary key,
  timestamp timestamptz not null,

  -- Prediction Metrics
  probability numeric not null,
  confidence_tier text not null,       -- 'HIGH' | 'MEDIUM' | 'LOW'
  model_version text not null,         -- e.g. 'v1', 'v2', 'v2.joblib'

  -- Entity Linkage
  signal_id text,                      -- Display signal ID (e.g. '0042') or UUID
  trade_id uuid,                       -- Links to active_trades.id / trade_analytics.id when executed
  
  -- Market Context
  spot numeric not null,
  source text not null default 'event_triggered', -- 'event_triggered' | 'continuous'

  -- Feature Payload
  feature_snapshot jsonb not null,     -- Canonical key-value dictionary of model input features

  created_at timestamptz not null default now()
);

-- Query optimization indexes
CREATE INDEX IF NOT EXISTS idx_ml_pred_timestamp ON ml_predictions (timestamp desc);
CREATE INDEX IF NOT EXISTS idx_ml_pred_signal ON ml_predictions (signal_id);
CREATE INDEX IF NOT EXISTS idx_ml_pred_trade ON ml_predictions (trade_id);
CREATE INDEX IF NOT EXISTS idx_ml_pred_model_version ON ml_predictions (model_version);
CREATE INDEX IF NOT EXISTS idx_ml_pred_confidence ON ml_predictions (confidence_tier);
CREATE INDEX IF NOT EXISTS idx_ml_pred_source ON ml_predictions (source);
```

### Database Migration: `migrations/2026-09-12-task153-ml-predictions-schema.sql`
Because `ml_predictions` contains 0 rows in production, column renaming is completely non-breaking. To guarantee safe execution across all environments:

```sql
-- =====================================================================
-- TASK-153: Standardize ml_predictions schema for prediction persistence
-- Safe and idempotent across clean installs and existing tables.
-- =====================================================================

CREATE TABLE IF NOT EXISTS ml_predictions (
  id bigserial primary key,
  timestamp timestamptz not null,
  probability numeric not null,
  confidence_tier text not null,
  model_version text not null,
  signal_id text,
  trade_id uuid,
  spot numeric not null,
  source text not null default 'event_triggered',
  feature_snapshot jsonb not null,
  created_at timestamptz not null default now()
);

DO $$
BEGIN
  -- Handle migration from legacy column name 'features' to 'feature_snapshot'
  IF EXISTS (
    SELECT 1 FROM information_schema.columns 
    WHERE table_name = 'ml_predictions' AND column_name = 'features'
  ) AND NOT EXISTS (
    SELECT 1 FROM information_schema.columns 
    WHERE table_name = 'ml_predictions' AND column_name = 'feature_snapshot'
  ) THEN
    ALTER TABLE ml_predictions RENAME COLUMN features TO feature_snapshot;
  END IF;

  -- Ensure trade_id column exists
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.columns 
    WHERE table_name = 'ml_predictions' AND column_name = 'trade_id'
  ) THEN
    ALTER TABLE ml_predictions ADD COLUMN trade_id uuid;
  END IF;

  -- Ensure source column has NOT NULL and default
  IF EXISTS (
    SELECT 1 FROM information_schema.columns 
    WHERE table_name = 'ml_predictions' AND column_name = 'source'
  ) THEN
    ALTER TABLE ml_predictions ALTER COLUMN source SET DEFAULT 'event_triggered';
  END IF;
END $$;

-- Idempotent index creation
CREATE INDEX IF NOT EXISTS idx_ml_pred_timestamp ON ml_predictions (timestamp desc);
CREATE INDEX IF NOT EXISTS idx_ml_pred_signal ON ml_predictions (signal_id);
CREATE INDEX IF NOT EXISTS idx_ml_pred_trade ON ml_predictions (trade_id);
CREATE INDEX IF NOT EXISTS idx_ml_pred_model_version ON ml_predictions (model_version);
CREATE INDEX IF NOT EXISTS idx_ml_pred_confidence ON ml_predictions (confidence_tier);
CREATE INDEX IF NOT EXISTS idx_ml_pred_source ON ml_predictions (source);
```

---

## 3. Architecture: Non-Blocking Prediction Logging Mechanism

### Thread Isolation & Bounded Concurrency
The trading loop in `main.py` processes live market ticks and candles where latencies must remain under 100 milliseconds. Supabase network roundtrips typically require 50-300ms, and during network degradation or rate-limiting can stall for several seconds.

To guarantee zero impact on trading execution:
1. **Dedicated Worker Pool**: Persistence operations must execute in a private, bounded `ThreadPoolExecutor(max_workers=2, thread_name_prefix="prediction_logger")`.
   - *Rationale*: Do NOT use the default asyncio thread pool (`loop.run_in_executor(None, ...)`), because high network latency in Supabase could exhaust the shared worker pool, starving critical engine operations.
2. **Fire-and-Forget Dispatch**: The core event loop dispatches the database insert task to the executor without `await`ing the network response. Dispatch overhead is $< 0.05$ ms.
3. **Synchronous Fallback**: When called outside an active event loop (such as in offline evaluation scripts or unit test suites), the logger falls back to safe synchronous execution without crashing.
4. **Complete Exception Containment**: The internal insert routine catches all subclasses of `Exception`, emits a structured warning to stderr/logging, and never propagates errors up the stack.

### Component Architecture Diagram

```
+-------------------------------------------------------------------------+
|                              ARES Engine                                |
|                                                                         |
|  [Market Tick / Signal]                                                 |
|          |                                                              |
|          v                                                              |
|  SignalPredictor.predict_from_raw()                                     |
|          |                                                              |
|          +---> Enriches AresSignal (probability, tier, features)        |
|          |                                                              |
|          v                                                              |
|  PositionManager.add_trade(signal, spot)                                |
|          |                                                              |
|          +---> Generates trade_id (UUID)                                |
|          +---> Stores trade_id on AresSignal                            |
|          |                                                              |
|          v                                                              |
|  PredictionLogger.log_prediction()                                      |
|          |                                                              |
|          | (Fire-and-Forget, < 0.05ms)                                  |
|          +-------------------------------+                              |
+------------------------------------------|------------------------------+
                                           |
                                           v
                        [Dedicated ThreadPoolExecutor]
                               (max_workers = 2)
                                           |
                                           v
                              sanitize_features(snapshot)
                                (NumPy -> Python native,
                                 NaN/Inf -> None)
                                           |
                                           v
                           Supabase PostgREST Client Insert
                           (table: "ml_predictions")
                                           |
                                           v
                       +---------------------------------------+
                       | Success: Row inserted                 |
                       | Failure: Catch & log warning (silent) |
                       +---------------------------------------+
```

### Feature Serialization & Sanitization Contract
Raw features generated by `ml_signal/features.py` contain:
- `numpy.float32`, `numpy.float64`, `numpy.int64`
- `numpy.nan` or `math.nan` (unresolved support/resistance levels)
- `numpy.inf` or `-numpy.inf`
- `datetime` objects

PostgreSQL JSONB and Python's `json` encoder reject `NaN`, `Infinity`, and raw NumPy datatypes with `ValueError` or `TypeError`.

The `PredictionLogger` must implement a pure sanitization function `sanitize_feature_snapshot`:
```python
def sanitize_feature_snapshot(features: Dict[str, Any]) -> Dict[str, Any]:
    """Sanitizes feature dictionary for safe JSONB serialization in PostgreSQL.
    
    Rules:
    - NumPy float/int converted to built-in float/int.
    - NaN and Inf converted to None (JSON null).
    - Float values rounded to 6 decimal places.
    - Datetime objects converted to UTC ISO-8601 strings.
    """
```

### Entity Linkage Lifecycle (`signal_id` & `trade_id`)
To prevent race conditions between trade entry and prediction logging:
1. When a signal is generated, `signal.signal_id` is created (e.g. 4-digit display code or canonical identifier).
2. `PositionManager.add_trade` creates `trade_id = str(uuid.uuid4())`.
3. `PositionManager.add_trade` must store `trade_id` directly onto `signal.trade_id`.
   - Update `models.AresSignal`: add `trade_id: Optional[str] = None`.
4. If a signal is skipped due to trade deduplication (`settings.trade_dedupe_tolerance_pts`), `signal.trade_id` remains `None`.
5. `PredictionLogger.log_prediction` is called immediately following `add_trade`, logging in a **single atomic INSERT** with both `signal_id` and `trade_id` (if executed).
   - *Advantage*: Completely eliminates the need for a secondary `UPDATE ml_predictions SET trade_id = ...` on trade close, avoiding race conditions and halving Supabase write volume.

---

## 4. API & Class Contracts

### 1. `PredictionLogger` Class (`storage.py`)

```python
class PredictionLogger:
    """Handles asynchronous, non-blocking persistence of ML predictions to Supabase."""

    def __init__(self, supabase_client: Optional[Client] = None, max_workers: int = 2):
        self.supabase = supabase_client or create_client(
            settings.supabase_url, settings.supabase_key
        )
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix="ml_pred_logger"
        )

    def log_prediction(
        self,
        probability: float,
        confidence_tier: str,
        model_version: str,
        spot: float,
        feature_snapshot: Dict[str, Any],
        signal_id: Optional[str] = None,
        trade_id: Optional[str] = None,
        source: str = "event_triggered",
        timestamp: Optional[Any] = None,
    ) -> None:
        """Dispatches an asynchronous insert to ml_predictions.
        
        Guaranteed non-blocking and safe against all runtime exceptions.
        """
        ...
```

### 2. Signal Model Enhancement (`models.py`)

```python
@dataclass
class AresSignal:
    ...
    signal_id: str = field(default_factory=lambda: f"{random.randint(0, 9999):04d}")
    db_id: Optional[int] = None
    trade_id: Optional[str] = None  # UUID populated if a trade is executed by PositionManager
    ...
```

### 3. Position Manager Integration (`position_manager.py`)

In `PositionManager.add_trade`:
```python
trade_id = str(uuid.uuid4())
signal.trade_id = trade_id  # Expose trade_id to signal for downstream prediction linkage
```

### 4. Engine Integration (`main.py`)

In `main.py` signal handling block:
```python
# 1. Inference
if ml_predictor:
    try:
        ml_pred = ml_predictor.predict_from_raw(...)
        signal.ml_prediction = ml_pred
    except Exception as pred_err:
        print(f"⚠️ ML Prediction failed: {pred_err}")

# 2. Options sizing & signal storage
...
await storage.log_signal(signal, spot)

# 3. Position management (sets signal.trade_id if executed)
try:
    position_manager.add_trade(signal, spot, atm=atm)
except Exception as pm_err:
    print(f"⚠️ Position manager add_trade failed: {pm_err}")

# 4. Asynchronous Prediction Persistence
if ml_predictor and getattr(signal, "ml_prediction", None):
    try:
        prediction_logger.log_prediction(
            probability=signal.ml_prediction["probability"],
            confidence_tier=signal.ml_prediction["confidence_tier"],
            model_version=signal.ml_prediction["model_version"],
            spot=spot,
            feature_snapshot=signal.ml_prediction.get("features", {}),
            signal_id=getattr(signal, "signal_id", None),
            trade_id=getattr(signal, "trade_id", None),
            source="event_triggered",
            timestamp=now,
        )
    except Exception as log_err:
        print(f"⚠️ Non-blocking prediction dispatch error: {log_err}")
```

### 5. Standalone Runners Refactor (`ml_signal/live.py` & `ml_signal/signal_consumer.py`)
Replace ad-hoc `self._supabase.table("ml_predictions").insert(...)` calls with unified `PredictionLogger.log_prediction` to guarantee consistent schema usage (`feature_snapshot`) and error handling across continuous and event-triggered processes.

---

## 5. Data Retention & Privacy Policy

### Storage Growth Projections
- **Event-Triggered Mode (`source = 'event_triggered'`)**:
  - Frequency: 2 to 10 predictions per trading day.
  - Payload size: ~2.5 KB per row (with 50 sanitized features).
  - Annual volume: ~500 - 2,500 rows/year ($\approx 1.25 - 6.25 \text{ MB/year}$).
- **Continuous Mode (`source = 'continuous'`)**:
  - Frequency: 1 prediction per minute (375 predictions/day).
  - Annual volume: ~93,000 rows/year ($\approx 230 \text{ MB/year}$).

### Data Retention Policy Specification
1. **Event-Triggered Predictions (`source = 'event_triggered'`)**:
   - **Retention Period**: **Permanent (Indefinite)**.
   - **Rationale**: Mission-critical for historical model auditing, regulatory audit trails, cross-model comparison, and long-term backtesting. Storage footprint is negligible.
2. **Continuous Predictions (`source = 'continuous'`)**:
   - **Retention Period**: **90-day rolling window** in active Supabase storage.
   - **Purge Schedule**: Monthly maintenance query:
     ```sql
     DELETE FROM ml_predictions 
     WHERE source = 'continuous' 
       AND timestamp < NOW() - INTERVAL '90 days';
     ```
   - **Cold Archival**: Prior to purge, continuous rows are exported to compressed Parquet files in `reports/ml/archive/` for offline research.

### Privacy & Information Security
1. **Zero Personally Identifiable Information (PII)**:
   - `ml_predictions` stores exclusively market microstructure data (spot price, technical indicators, Greek values, open interest) and model scores.
   - Broker account IDs, Dhan client IDs, API tokens, user identifiers, and IP addresses are strictly excluded from `feature_snapshot` and all schema columns.
2. **Row Level Security (RLS) & Access Control**:
   - `ml_predictions` access is restricted to the backend service role (`authenticator` / `postgres`).
   - Public anonymous read and write access is disabled.
3. **Immutable Audit Record**:
   - Rows in `ml_predictions` are append-only. Application code never issues `UPDATE` or `DELETE` on event-triggered prediction records.

---

## 6. Alternatives Considered & Why Rejected

| Alternative | Evaluation | Why Rejected |
|---|---|---|
| **Synchronous `await` in `main.py`** | Wait for Supabase HTTP response on every signal | Unacceptable latency risk. Any HTTP delay (100ms - 5s) stalls candle processing and leads to trade execution slippage. |
| **External Message Broker (Redis / RabbitMQ / Celery)** | Queue prediction jobs in Redis worker | Over-engineering (violates Ponytail principle). Redis was intentionally removed in MANM-93. In-process `ThreadPoolExecutor` provides sufficient decoupling without external infra dependencies. |
| **Two-Phase Write (INSERT then UPDATE on trade close)** | Insert prediction at signal time, update with `trade_id` later | Introduces worker thread race conditions, doubles PostgREST roundtrips, and risks orphaned trade links if the UPDATE fails. |
| **Store Predictions inside `ml_collection` table** | Add prediction columns to existing training collection table | Violates separation of concerns. `ml_collection` is an inert training dataset (~350 rows/day). `ml_predictions` is an operational audit table for live model inference performance. |
| **Asyncio Task without ThreadPool (`asyncio.create_task`)** | Run Supabase call via coroutine in event loop | Supabase Python client's `.execute()` uses synchronous `httpx` under the hood. Calling synchronous HTTP inside an asyncio task blocks the event loop thread unless offloaded to an executor. |

---

## 7. Implementation Task Assignment for Code Generator Agent

Upon human approval of this ADR, assign implementation of **TASK-153** to the **Code Generator Agent** with the following file-level instructions:

### Task Breakdown

1. **`migrations/2026-09-12-task153-ml-predictions-schema.sql`**:
   - Author idempotent SQL migration adjusting `ml_predictions` to use `feature_snapshot jsonb`, `trade_id uuid`, `source text`, and required indexes.

2. **`schema.sql` & `ml_signal/schema.sql`**:
   - Update `ml_predictions` table definition and index definitions to match Section 2.

3. **`models.py`**:
   - Add `trade_id: Optional[str] = None` field to `AresSignal` dataclass.

4. **`storage.py`**:
   - Implement `sanitize_feature_snapshot(features: Dict[str, Any]) -> Dict[str, Any]` converting NumPy types and guarding NaN/Inf.
   - Implement `PredictionLogger` with dedicated `ThreadPoolExecutor(max_workers=2)`.
   - Implement `PredictionLogger.log_prediction(...)` with fire-and-forget async dispatch, sync fallback, and full exception suppression.

5. **`position_manager.py`**:
   - In `PositionManager.add_trade`, assign `signal.trade_id = trade_id`.

6. **`main.py`**:
   - Initialize `prediction_logger = PredictionLogger()`.
   - In the `if signal:` block, after `position_manager.add_trade`, invoke `prediction_logger.log_prediction(...)`.

7. **`ml_signal/live.py` & `ml_signal/signal_consumer.py`**:
   - Update prediction logging to use `PredictionLogger.log_prediction` with `source="continuous"` and `source="event_triggered"`.

8. **`docs/data_retention_and_privacy.md`**:
   - Author comprehensive data retention and privacy documentation reflecting Section 5.

9. **`tests/unit/test_task153_prediction_persistence.py`**:
   - Unit tests for:
     - `test_sanitize_feature_snapshot`: Verifies NumPy numbers, NaNs, infinities, and datetimes are cleanly sanitized.
     - `test_prediction_logger_dispatch_async`: Verifies asynchronous fire-and-forget insert to Supabase mock.
     - `test_prediction_logger_sync_fallback`: Verifies behavior when no event loop is running.
     - `test_prediction_logger_exception_suppression`: Verifies that network/HTTP exceptions are caught and suppressed without raising.
     - `test_prediction_logger_missing_optional_fields`: Verifies persistence when `trade_id` or `signal_id` is None.
     - `test_signal_trade_id_linkage`: Verifies `PositionManager.add_trade` populates `signal.trade_id`.

---

## 8. Definition of Done

- [ ] ADR-153 approved by human reviewer.
- [ ] Idempotent SQL migration script created in `migrations/`.
- [ ] `schema.sql` and `ml_signal/schema.sql` synchronized.
- [ ] `PredictionLogger` implemented with dedicated thread pool and feature sanitization.
- [ ] `PositionManager` links `trade_id` to `AresSignal`.
- [ ] `main.py`, `live.py`, and `signal_consumer.py` persist predictions on inference.
- [ ] 100% unit test suite passing for prediction persistence and error suppression.
- [ ] Zero regression across existing test suites (`pytest tests/unit`).
- [ ] Data retention and privacy policy documented in `docs/data_retention_and_privacy.md`.
