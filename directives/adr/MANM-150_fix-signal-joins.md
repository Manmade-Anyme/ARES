# ADR-150: Canonical Signal-to-Trade UUID Joins and Safe Historical Backfill Plan

- **Status**: Proposed (Supersedes MANM-151)
- **Date**: 2026-09-12
- **Task ID**: MANM-150
- **Author**: Software Architect Agent (`d2d4e328-096d-4658-8d90-44aa7b51ed05`)

---

## 1. Executive Summary & Root Cause Analysis

### Problem Statement
In production, `trade_analytics.signal_id` is populated for 232 rows, but none join to `ares_signals.id`. Furthermore, `active_trades.signal_id` is populated for all 233 rows with four-digit display strings (e.g. `"4829"`). Relational queries joining `ares_signals`, `active_trades`, `trade_analytics`, and `ml_collection` fail completely because the systems store mismatched identifier types without a single canonical primary/foreign key contract.

### Flaw in Previous Approach (MANM-151)
ADR-151 proposed standardizing the schema on the 4-digit display code (`signal_id text`). This approach is fundamentally flawed and must be superseded:
1. **Collision Risk (Birthday Paradox)**: The domain of a 4-digit string is only 10,000 values ($[0000, 9999]$). Across 233 trades, collision probability exceeds 93.4%. By 1,000 trades, collisions are virtually guaranteed. Using non-unique display strings as relational join keys corrupts `ml_collection` outcomes by overwriting multiple trades that share the same display ID.
2. **Missing Primary Key on `ares_signals`**: `ares_signals.id` was defined as `bigserial primary key` (integers $1, 2, \dots$). Standardizing downstream tables on 4-digit display strings left them completely disconnected from `ares_signals.id` (0 of 232 rows joined).
3. **No Foreign Key Integrity**: 4-digit display strings provide zero referential integrity and cannot support database constraints.

### Root Cause of Join Disconnect
1. **Server-Side BigSerial Dependency and Failed-Insert Path**:
   - `ares_signals.id` was generated server-side by PostgreSQL `BIGSERIAL` upon insert.
  - In the reviewed runtime, `main.py` awaits `storage.log_signal()` before `position_manager.add_trade()` and the ML snapshot. `Storage.log_signal()` also awaits its executor future, so this ordering is not itself a race.
  - When the insert fails, `signal.db_id` remains `None`; downstream writes then record `NULL` (analytics/ML) or the legacy 4-digit display code (active trades). Historical deployments must be checked separately before attributing additional rows to an ordering race.
2. **Type Discrepancies Across Tables**:
   - `ares_signals.id`: `bigserial` (PostgreSQL bigint)
   - `active_trades.signal_id`: `text` (holding 4-digit random display codes)
   - `trade_analytics.signal_id`: `bigint` (holding 4-digit display codes cast to integer, or NULL)
   - `ml_collection.signal_id`: `text` (holding either 4-digit display codes or integer string representations)

---

## 2. Decision & Architectural Specification

### 2.1 Canonical Client-Generated UUID Primary Key
We establish a client-generated UUID as the single canonical join key across all signal and trade tables:
- **Generation In-Memory at Creation**: `AresSignal` generates a unique UUID `signal.id = str(uuid.uuid4())` synchronously upon instantiation.
- **No Key-Availability Race**: Because `signal.id` is available immediately in memory before any I/O begins, downstream components can receive the same canonical key without waiting for a database-generated identifier. Network latency or executor scheduling can therefore not change which signal UUID they reference; insert failures still require normal error handling and reconciliation.
- **Collision Resistant**: UUIDv4 provides $2^{122}$ bits of entropy, making accidental collisions extraordinarily unlikely across distributed processes, restarts, backtests, and parallel runners. The database still enforces uniqueness.

### 2.2 Preservation of 4-Digit Display ID for UX/Alerts
To preserve concise human-readable tags in Discord alerts, console banners, and UI telemetry:
- `AresSignal` maintains a separate `display_id: str = field(default_factory=lambda: f"{random.randint(0, 9999):04d}")`.
- Stored in a dedicated `display_id text` column across tables where human inspection is required.
- **Strict Separation of Concerns**: Display IDs are never used for joins, foreign keys, or ML label attribution.

### 2.3 Target Schema Contract

| Table | Primary Key | Signal Join Key Column | Display Column | Notes |
|---|---|---|---|---|
| `ares_signals` | `id uuid PRIMARY KEY DEFAULT gen_random_uuid()` | `id` (PK) | `display_id text` | Canonical source of truth for signals |
| `active_trades` | `id uuid PRIMARY KEY` | `signal_id uuid REFERENCES ares_signals(id)` | `display_id text` | Active in-memory and persisted positions |
| `trade_analytics` | `id uuid PRIMARY KEY` | `signal_id uuid REFERENCES ares_signals(id)` | In `market_context` | Analytical trade performance records |
| `ml_collection` | `id bigserial PRIMARY KEY` | `signal_id uuid` | `signal_display_id text` | Feature snapshots and post-hoc trade outcomes |

```mermaid
erDiagram
    ares_signals ||--o{ active_trades : "signal_id (UUID)"
    ares_signals ||--o{ trade_analytics : "signal_id (UUID)"
    ares_signals ||--o{ ml_collection : "signal_id (UUID)"
    active_trades ||--|| trade_analytics : "id (UUID)"
    trade_analytics ||--o{ ml_collection : "trade_id (UUID)"

    ares_signals {
        uuid id PK
        text display_id
        text setup_type
        text direction
        numeric trigger_price
        timestamptz timestamp
    }
    active_trades {
        uuid id PK
        uuid signal_id FK
        text display_id
        text setup_type
        numeric entry_price
        text state
    }
    trade_analytics {
        uuid id PK
        uuid signal_id FK
        numeric entry_price
        numeric exit_price
        numeric pnl_points
        text result_state
        jsonb market_context
    }
    ml_collection {
        bigserial id PK
        uuid signal_id
        uuid trade_id
        text trade_outcome
        numeric trade_pnl
        integer trade_score
    }
```

---

## 3. Alternatives Considered & Rationale

| Option | Pros | Cons | Verdict |
|---|---|---|---|
| **A. 4-Digit Display ID as Join Key (MANM-151)** | Easy to type, already in some columns | 93.4% collision rate, corrupts ML labels, no referential integrity, no PK match | **REJECTED** (Superseded) |
| **B. Server-Side BigSerial with Synchronous I/O** | Retains numeric integer IDs | Adds 100–300ms blocking HTTP roundtrip to tick loop; trade entry fails if DB lags | **REJECTED** (Performance risk) |
| **C. Client-Side UUID Primary Key (Chosen)** | Microsecond generation, no insert-order dependency, collision-resistant, clean separation of concerns | 36-char string vs 4 digits (solved by dedicated `display_id` column) | **ADOPTED** |

---

## 4. Transition & Safe Historical Backfill Plan

Per PM directive and issue acceptance criteria:
> "Historical data is left untouched until backfill is fully planned and unambiguous."
> "Formulate a safe read-only validation query and a separately documented historical backfill plan."

### 4.1 Read-Only Validation Query
After the additive bridge columns in section 4.2 exist, execute the following read-only SQL validation query before any backfill. It checks the signal, active-trade, analytics, and ML paths independently and reports missing or mismatched links:

```sql
-- Read-Only Audit: Inspect join fidelity and identify legacy ID formats
SELECT
    ta.id AS trade_id,
    ta.signal_id AS trade_analytics_signal_id,
  ta.signal_uuid AS trade_analytics_signal_uuid,
    at.signal_id AS active_trades_signal_id,
  at.signal_uuid AS active_trades_signal_uuid,
  sig.id AS legacy_ares_signals_id,
  sig.signal_uuid AS ares_signals_uuid,
  mc.id AS ml_collection_id,
  mc.signal_id AS ml_collection_signal_id,
  mc.signal_uuid AS ml_collection_signal_uuid,
    sig.setup_type AS signal_setup_type,
    ta.setup_type AS trade_setup_type,
    ta.entry_timestamp,
    sig.timestamp AS signal_timestamp,
  CASE
    WHEN sig.signal_uuid IS NULL THEN 'SIGNAL_MISSING'
    WHEN at.id IS NULL THEN 'ACTIVE_TRADE_MISSING'
    WHEN ta.signal_uuid IS NULL THEN 'ANALYTICS_SIGNAL_UNRESOLVED'
    WHEN at.signal_uuid IS DISTINCT FROM ta.signal_uuid THEN 'ACTIVE_TRADE_SIGNAL_MISMATCH'
    WHEN mc.id IS NULL THEN 'ML_ROW_MISSING'
    WHEN mc.signal_uuid IS DISTINCT FROM ta.signal_uuid THEN 'ML_SIGNAL_MISMATCH'
    WHEN ta.signal_id IS NULL THEN 'LEGACY_NULL_KEY'
    WHEN ta.signal_id::text ~ '^[0-9]{1,4}$' THEN 'LEGACY_4DIGIT_OR_INT'
    WHEN ta.signal_id::text ~ '^[0-9a-fA-F-]{36}$' THEN 'CANONICAL_UUID'
        ELSE 'OTHER_FORMAT'
  END AS join_status
FROM trade_analytics ta
LEFT JOIN active_trades at ON ta.id = at.id
LEFT JOIN ares_signals sig ON sig.signal_uuid = ta.signal_uuid
LEFT JOIN ml_collection mc ON mc.trade_id = ta.id
ORDER BY ta.entry_timestamp DESC;
```

### 4.2 Database Schema Transition (Backward Compatible)
The target schema uses `ares_signals.id uuid` as the primary key. Existing deployments cannot safely write a UUID into their current bigint `id` column as an additive migration. They therefore use `signal_uuid` as a temporary canonical bridge key until the explicit primary-key cutover below has been validated. No runtime writer may use `id` for UUIDs before that cutover.

```sql
-- Additive bridge migration. Do not alter legacy signal_id columns yet.
CREATE EXTENSION IF NOT EXISTS "pgcrypto";

ALTER TABLE ares_signals ADD COLUMN IF NOT EXISTS signal_uuid uuid;
UPDATE ares_signals SET signal_uuid = gen_random_uuid() WHERE signal_uuid IS NULL;
ALTER TABLE ares_signals ALTER COLUMN signal_uuid SET DEFAULT gen_random_uuid();
ALTER TABLE ares_signals ALTER COLUMN signal_uuid SET NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS uq_ares_signals_signal_uuid ON ares_signals (signal_uuid);
ALTER TABLE ares_signals ADD COLUMN IF NOT EXISTS display_id text;

ALTER TABLE active_trades ADD COLUMN IF NOT EXISTS signal_uuid uuid;
ALTER TABLE active_trades ADD COLUMN IF NOT EXISTS display_id text;
ALTER TABLE trade_analytics ADD COLUMN IF NOT EXISTS signal_uuid uuid;
ALTER TABLE ml_collection ADD COLUMN IF NOT EXISTS signal_uuid uuid;
```

After the ambiguity-guarded backfill completes and the validation query reports no unresolved rows, create foreign keys on the bridge columns. For a greenfield database, create `ares_signals.id uuid` and use `signal_id uuid REFERENCES ares_signals(id)` directly. For an existing database, perform a separately reviewed primary-key cutover that promotes `signal_uuid` to `ares_signals.id`, updates the three bridge references, and only then removes the legacy bigint key.

### 4.3 Safe Historical Backfill Procedure
We define a separate, idempotent reconciliation script: `scripts/backfill_signal_uuids.py`.

#### Backfill Phasing
1. **Phase 1: Stable UUID Population on `ares_signals`**:
  - Ensure every legacy `ares_signals` row has a persistent, non-null, unique `signal_uuid`. The generated values are stable after this one-time write; they are not derived deterministically from existing data.
   - Populate `display_id` from legacy reasons or format if available.
2. **Phase 2: Active-Trade Reconciliation**:
  - Reconcile each `active_trades` row to a signal using its linked trade ID, setup, direction, and entry/created timestamp. If exactly one signal is corroborated, write its `signal_uuid`; otherwise do not update and flag it unresolved.
3. **Phase 3: Analytics Reconciliation**:
  - For any `trade_analytics` row whose legacy `signal_id` is numeric, do not treat numeric equality with `ares_signals.id` as proof: a zero-stripped display ID can equal an unrelated bigserial key. Require timestamp, setup, and direction corroboration and exactly one candidate before writing `signal_uuid`.
  - For rows holding 4-digit display IDs or NULL `signal_id`, candidate signals are searched within a temporal window:
     $$|\text{entry\_timestamp} - \text{signal\_timestamp}| \le 120\text{ seconds}$$
     matching `setup_type` and `direction`.
   - **Ambiguity Guard**:
     - If **exactly 1** candidate signal matches: Link safely.
     - If **0** or **>1** candidate signals match: **DO NOT UPDATE**. Flag as `AMBIGUOUS_UNRESOLVED` in the audit log.
4. **Phase 4: ML Collection Outcome Re-attribution**:
  - Populate `ml_collection.signal_uuid` from the verified trade and signal mapping. Update outcome fields only for the matching `trade_id`; never update rows by display ID alone.

---

## 5. Implementation Task Assignment for Code Generator Agent

The Code Generator Agent will implement MANM-150 following human approval of this ADR.

### Task Breakdown & Component Boundaries

#### 1. `models.py` (`AresSignal`)
- Add `id: str = field(default_factory=lambda: str(uuid.uuid4()))` as the primary identifier.
- Rename/preserve display code as `display_id: str = field(default_factory=lambda: f"{random.randint(0, 9999):04d}")`.
- Add backward-compatible property:
  ```python
  @property
  def signal_id(self) -> str:
      """Backward compatibility: returns display_id for UX/alerts or id for joins."""
      return self.display_id
  ```
- Deprecate `db_id: Optional[int]`.

#### 2. `storage.py` (`Storage` & `AnalyticsLogger`)
- **`Storage.log_signal`**:
  - In a greenfield schema, insert `{"id": signal.id, "display_id": signal.display_id, ...}` into `ares_signals`.
  - In an existing schema during the bridge period, insert `{"signal_uuid": signal.id, "display_id": signal.display_id, ...}` and never send the UUID to legacy bigint `id`.
  - Remove dependency on capturing auto-increment response for `signal.db_id` after the bridge is active.
- **`AnalyticsLogger.log_entry`**:
  - In a greenfield schema, write `"signal_id": signal.id` (canonical UUID) to `trade_analytics`.
  - During the legacy bridge, write `"signal_uuid": signal.id` and retain the old `signal_id` value until reconciliation is complete.
  - Store `"display_id": signal.display_id` inside `market_context`.
- **`AnalyticsLogger.log_exit`**:
  - Update `ml_collection` using the verified canonical UUID and matching `trade_id`; never update by display ID alone.

#### 3. `position_manager.py` (`PositionManager`)
- In `add_trade`:
  - Store `"signal_id": signal.id` (canonical UUID) in a greenfield schema, or `"signal_uuid": signal.id` during the legacy bridge.
  - Store `"display_id": signal.display_id` (4-digit string) in `trade_data`.
  - Push both fields to `active_trades`.

#### 4. `ml_signal/collector.py` (`MLCollector`)
- In `snapshot`:
  - Record `signal_id = signal.id` (canonical UUID) when `signal` is present in a greenfield schema, or the bridge UUID column during legacy migration.
  - Record `signal_display_id = signal.display_id`.

#### 5. `schema.sql` & Migrations
- Create migration script `migrations/2026-09-12-task150-canonical-signal-uuid.sql`.
- Update `schema.sql` and `README.md` to reflect canonical UUID join keys and `display_id` columns.

#### 6. Safe Backfill Script (`scripts/backfill_signal_uuids.py`)
- Standalone CLI utility with `--dry-run` default and `--apply` flag.
- Incorporates ambiguity guards and detailed reconciliation reporting.

#### 7. Test Suite Updates (`tests/`)
- Add `tests/unit/test_task150_signal_uuid_joins.py`:
  - Verify UUID propagation through the selected schema path: `AresSignal.id` $\to$ `active_trades.signal_id` $\to$ `trade_analytics.signal_id` $\to$ `ml_collection.signal_id` for greenfield databases, and through each `signal_uuid` bridge column for existing databases.
  - Verify `display_id` preservation across console formatting and alerts.
  - Verify backfill script skips ambiguous rows and handles dry-run correctly.
- Update legacy test assertions in `tests/unit/test_storage.py`, `tests/unit/test_position_manager.py`, and `tests/unit/test_task194_ml_labels_and_oi_distribution.py` to expect UUID string formats.

---

## 6. Definition of Done & Acceptance Criteria

- [ ] ADR-150 drafted, reviewed, and approved by human operator.
- [ ] No product code modified prior to human ADR approval.
- [ ] Canonical UUID join key contract established across `ares_signals`, `active_trades`, `trade_analytics`, and `ml_collection`, with the legacy bridge path explicitly selected before runtime rollout.
- [ ] 4-digit display IDs isolated to dedicated `display_id` columns for UX/alerts.
- [ ] Read-only validation query documented and verified.
- [ ] Historical backfill plan documented with mandatory ambiguity guard (no uncorroborated updates).
- [ ] Implementation assignments specified with exact file paths and contracts for Code Generator Agent.
- [ ] Test suite pass rate remains 100% (all 521+ tests passing).
