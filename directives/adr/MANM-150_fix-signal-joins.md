# ADR-150: Canonical Signal-to-Trade UUID Joins and Safe Historical Backfill Plan

- **Status**: Proposed (supersedes MANM-151)
- **Date**: 2026-09-13
- **Task ID**: MANM-150
- **Author**: Software Architect Agent

---

## 1. Problem Statement and Root Cause

`trade_analytics.signal_id`, `active_trades.signal_id`, and `ml_collection.signal_id` do not share one stable identifier. Existing rows contain a mixture of legacy `ares_signals.id` values, four-digit display codes, integer strings, and nulls. The resulting joins cannot be trusted for historical attribution.

The four-digit code is a presentation value, not an identifier. Its 10,000-value domain permits collisions and provides no foreign-key integrity, so MANM-151's proposal to use it as a join key is rejected.

The reviewed runtime does **not** establish a signal-insert race when persistence succeeds: `main.py` awaits `storage.log_signal()` before calling `PositionManager.add_trade()` and the ML snapshot, and `Storage.log_signal()` awaits its executor future. However, the method currently catches an insert failure and returns normally. Therefore, the actual failure path can still create downstream work without a verified parent row. The architecture must distinguish successful ordering from failed persistence and gate downstream creation on a verified signal insert.

---

## 2. Decision

Use a client-generated UUID as the canonical signal identity. `AresSignal.id` is generated synchronously at object creation, before any I/O. Every new trade and ML snapshot receives that same UUID. A four-digit `display_id` remains available for Discord, console, and UI output, but is never used for joins, foreign keys, or ML attribution.

There are two explicit schema modes. Existing deployments first use `signal_uuid` as a canonical bridge column while retaining legacy bigint/text columns for read-only reconciliation. No runtime writer may place a UUID into legacy `ares_signals.id` or legacy `signal_id` columns during this bridge. Greenfield deployments use `ares_signals.id` and downstream `signal_id` UUID foreign keys directly. The bridge-to-greenfield primary-key cutover is a separately reviewed migration and is not implied by adding bridge columns.

The schema mode is selected explicitly at startup (`bridge` by default until cutover, or `greenfield` after cutover); it must not be inferred from a failed write. An invalid mode fails closed rather than silently writing a legacy key.

### 2.1 Schema Contract

| Table | Existing bridge mode | Greenfield/cutover mode | Display value |
|---|---|---|---|
| `ares_signals` | Legacy `id bigint` retained; `signal_uuid uuid UNIQUE NOT NULL` is canonical | `id uuid PRIMARY KEY` | `display_id text` |
| `active_trades` | Legacy `signal_id` retained; `signal_uuid uuid` is canonical | `signal_id uuid REFERENCES ares_signals(id)` | `display_id text` |
| `trade_analytics` | Legacy `signal_id` retained; `signal_uuid uuid` is canonical | `signal_id uuid REFERENCES ares_signals(id)` | `market_context.signal_display_id` |
| `ml_collection` | Legacy `signal_id` retained; `signal_uuid uuid` is canonical | `signal_id uuid REFERENCES ares_signals(id)` and `trade_id uuid` | `signal_display_id text` |

`active_trades.id`, `trade_analytics.id`, and `ml_collection.trade_id` are trade UUIDs. `trade_analytics.id` and `active_trades.id` identify the same trade. `ml_collection` may have multiple feature rows, so its outcome update is constrained by both `trade_id` and the verified canonical signal UUID.

### 2.2 Identity, Persistence, and Runtime Contracts

```python
# models.py
class AresSignal:
    id: str                  # str(uuid.uuid4()), canonical identity
    display_id: str          # four-digit presentation value

# storage.py
async def log_signal(signal: AresSignal, spot: float) -> bool:
    """Return True only after the parent row is acknowledged and verified.

    Raise SignalPersistenceError on insert failure or key mismatch.
    """

# position_manager.py
@dataclass(frozen=True)
class TradeEntryResult:
    trade_id: str | None
    binding_status: Literal["BOUND", "DUPLICATE_SKIPPED"]

async def add_trade(signal: AresSignal, spot: float, atm=None) -> TradeEntryResult:
    """Atomically persist a trade entry and return its binding result."""

# ml_signal/collector.py
async def snapshot(..., signal=None, trade_id: str | None = None,
             trade_binding_status: str = "NOT_APPLICABLE") -> None:
    """Persist signal identity and the auditable trade-binding status.

    When trade_id is provided, await and verify the insert before returning.
    """
```

The required signal flow is:

```text
await Storage.log_signal(signal)       # verified parent or SignalPersistenceError
entry = await PositionManager.add_trade(signal)
await MLCollector.snapshot(..., signal=signal, trade_id=entry.trade_id,
                     trade_binding_status=entry.binding_status)
await PositionManager.update_trades(...)
```

`main.py` must not continue to `add_trade`, create analytics, or bind an ML snapshot when `log_signal` raises or does not return verified success. It records terminal persistence telemetry and skips that signal cycle. A missing parent is never replaced by an orphan UUID row.

`PositionManager.add_trade` calls the schema-mode-specific RPC `create_trade_entry_bridge` or `create_trade_entry_greenfield`. Each RPC inserts `active_trades` and `trade_analytics` in one database transaction and returns only after both inserts commit. If either insert fails, PostgreSQL rolls back both rows; the method does not append the trade to memory and propagates the failure. A retry reuses the same trade UUID and is idempotent: an existing matching pair is returned as `BOUND`, while a conflicting pair raises a persistence error.

This transaction replaces fire-and-forget analytics entry writes. `main.py` awaits `add_trade` before `update_trades`, so an immediate stop/target event cannot call `log_exit` before the analytics row exists. If recovery or an external caller still observes a missing analytics row, `log_exit` uses bounded retry plus terminal failure telemetry; it must not silently return and lose the outcome.

For a newly created trade, the collector stores `trade_id` at entry, not retroactively at exit. For a trade-bound snapshot, the collector awaits its insert future and verifies the committed row contains the exact trade UUID and canonical signal UUID before returning. `main.py` awaits this signal-bound snapshot before calling `update_trades`, so a same-cycle exit cannot race the snapshot. If the insert fails, the collector retries with the same snapshot identity and raises `MLPersistenceError` after the bounded retry budget; the caller records terminal telemetry and must not claim the ML label was written. `AnalyticsLogger.log_exit` also treats an affected-row count of zero as a failed label operation, retries using the same `(trade_id, canonical signal UUID)`, and emits terminal reconciliation telemetry rather than silently completing. A duplicate result is persisted as `trade_binding_status = 'DUPLICATE_SKIPPED'`; a genuine missing binding is persisted as `UNRESOLVED` and blocks cutover. Ordinary non-signal cycles use `NOT_APPLICABLE` and are excluded from the unresolved audit.

The old `signal_id` model property may remain temporarily for presentation compatibility and must return `display_id` only. New persistence and join code must use `id` in greenfield mode or `signal_uuid` in bridge mode. `db_id` is legacy metadata only and is never the canonical join key.

---

## 3. Alternatives Considered

| Option | Reason considered | Decision |
|---|---|---|
| Four-digit display ID as the join key | Human-readable and already present in alerts | **Rejected**: collisions and no referential integrity |
| Server-generated bigint as the only new key | Minimal schema change | **Rejected**: key availability depends on persistence and preserves the existing type mismatch |
| Client UUID with explicit bridge/cutover modes | Available before I/O, collision-resistant, supports safe rollout | **Chosen** |
| Allow live trades after failed signal persistence | Keeps the trading loop moving | **Rejected**: creates orphan active, analytics, and ML records |
| Separate active/analytics inserts with compensation deletes | Avoids a database function | **Rejected**: a lost response or cleanup failure can leave a restart-visible orphan; one transaction is authoritative |
| Background-only analytics entry with silent exit miss | Avoids waiting on the entry insert | **Rejected**: loses outcomes on fast exits; transaction completion is awaited instead |

---

## 4. Transition and Safe Historical Backfill

Historical data remains untouched until the read-only audit is complete and every mutation is backed by an unambiguous match.

### 4.1 Additive Bridge Migration

The additive migration must not alter legacy key columns or add runtime UUID writes to the legacy bigint `ares_signals.id`:

```sql
CREATE EXTENSION IF NOT EXISTS "pgcrypto";

ALTER TABLE ares_signals ADD COLUMN IF NOT EXISTS signal_uuid uuid;
UPDATE ares_signals
SET signal_uuid = gen_random_uuid()
WHERE signal_uuid IS NULL;
ALTER TABLE ares_signals
  ALTER COLUMN signal_uuid SET DEFAULT gen_random_uuid(),
  ALTER COLUMN signal_uuid SET NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS uq_ares_signals_signal_uuid
  ON ares_signals (signal_uuid);
ALTER TABLE ares_signals ADD COLUMN IF NOT EXISTS display_id text;

ALTER TABLE active_trades ADD COLUMN IF NOT EXISTS signal_uuid uuid;
ALTER TABLE active_trades ADD COLUMN IF NOT EXISTS display_id text;
ALTER TABLE trade_analytics ADD COLUMN IF NOT EXISTS signal_uuid uuid;
ALTER TABLE ml_collection ADD COLUMN IF NOT EXISTS signal_uuid uuid;
ALTER TABLE ml_collection ADD COLUMN IF NOT EXISTS signal_display_id text;
ALTER TABLE ml_collection
  ADD COLUMN IF NOT EXISTS trade_binding_status text
  DEFAULT 'NOT_APPLICABLE';

UPDATE ml_collection
SET trade_binding_status = 'UNRESOLVED'
WHERE signal_generated IS TRUE
  AND trade_id IS NULL
  AND trade_binding_status = 'NOT_APPLICABLE';
```

The migration also adds indexes on every bridge column and creates the two mode-specific `create_trade_entry_*` RPCs. Each function inserts the active-trade and analytics rows in one transaction, uses `SECURITY INVOKER`, and returns the trade UUID only after commit. `NOT VALID` plus `VALIDATE CONSTRAINT` may be used for foreign keys to avoid an unbounded deployment lock, but validation must complete before cutover.

### 4.2 Read-Only Audit Including Orphan Endpoints

The primary audit must start from the union of all trade endpoints. Starting only from `trade_analytics` would omit an `active_trades` or `ml_collection` row that has no analytics row and could incorrectly report a clean cutover.

```sql
WITH trade_keys AS (
    SELECT id AS trade_id FROM active_trades
    UNION
    SELECT id AS trade_id FROM trade_analytics
    UNION
    SELECT trade_id FROM ml_collection WHERE trade_id IS NOT NULL
)
SELECT
    k.trade_id,
    at.signal_uuid AS active_signal_uuid,
    ta.signal_uuid AS analytics_signal_uuid,
    mc.signal_uuid AS ml_signal_uuid,
    at.signal_id AS legacy_active_signal_id,
    ta.signal_id AS legacy_analytics_signal_id,
    mc.signal_id AS legacy_ml_signal_id,
    sig.signal_uuid AS referenced_signal_uuid,
    at.id IS NOT NULL AS has_active_trade,
    ta.id IS NOT NULL AS has_analytics,
    mc.id IS NOT NULL AS has_ml_row,
    CASE
        WHEN ta.id IS NULL THEN 'ANALYTICS_ROW_MISSING'
        WHEN ta.signal_uuid IS NULL THEN 'ANALYTICS_SIGNAL_UNRESOLVED'
        WHEN sig.signal_uuid IS NULL THEN 'SIGNAL_MISSING'
        WHEN at.id IS NULL THEN 'ACTIVE_TRADE_MISSING'
        WHEN at.signal_uuid IS NULL THEN 'ACTIVE_TRADE_SIGNAL_UNRESOLVED'
        WHEN at.signal_uuid IS DISTINCT FROM ta.signal_uuid THEN 'ACTIVE_TRADE_SIGNAL_MISMATCH'
        WHEN mc.id IS NULL THEN 'ML_ROW_MISSING'
        WHEN mc.signal_uuid IS NULL THEN 'ML_SIGNAL_UNRESOLVED'
        WHEN mc.signal_uuid IS DISTINCT FROM ta.signal_uuid THEN 'ML_SIGNAL_MISMATCH'
        ELSE 'JOIN_VALID'
    END AS join_status,
    ta.entry_timestamp,
    sig.setup_type AS signal_setup_type,
    ta.setup_type AS trade_setup_type,
    sig.timestamp AS signal_timestamp
FROM trade_keys k
LEFT JOIN active_trades at ON at.id = k.trade_id
LEFT JOIN trade_analytics ta ON ta.id = k.trade_id
LEFT JOIN ml_collection mc ON mc.trade_id = k.trade_id
LEFT JOIN ares_signals sig
  ON sig.signal_uuid = COALESCE(ta.signal_uuid, at.signal_uuid, mc.signal_uuid)
ORDER BY ta.entry_timestamp DESC NULLS LAST, k.trade_id;
```

The `ta.signal_uuid IS NULL` check intentionally precedes `sig.signal_uuid IS NULL`; otherwise an unresolved analytics key is mislabeled as a missing signal. The audit is read-only.

Run this companion query for signal-bearing ML snapshots whose trade binding is unresolved. Confirmed duplicate snapshots are explicitly excluded; ordinary non-signal cycles are excluded by `signal_generated IS TRUE`:

```sql
SELECT
    id AS ml_collection_id,
    timestamp,
    signal_uuid,
    signal_id AS legacy_ml_signal_id,
    signal_display_id,
    trade_binding_status,
    signal_setup_type,
    'SIGNAL_SNAPSHOT_TRADE_UNRESOLVED' AS join_status
FROM ml_collection
WHERE signal_generated IS TRUE
  AND trade_id IS NULL
  AND COALESCE(trade_binding_status, 'UNRESOLVED') <> 'DUPLICATE_SKIPPED'
ORDER BY timestamp DESC, id;
```

Any row returned by the companion query must be classified as a failed persistence path or unresolved trade binding before cutover. A `DUPLICATE_SKIPPED` row is retained as audit evidence but does not block cutover.

### 4.3 Ordered Backfill Phases

1. **Signal identity** — populate stable, unique `ares_signals.signal_uuid` values and preserve them. Populate `display_id` only from corroborated legacy presentation data; never derive a join from it.
2. **Active-trade reconciliation** — reconcile every `active_trades.signal_id` before changing its type or adding its final foreign key. Use trade ID plus setup, direction, and entry/created timestamp. Write `signal_uuid` only for exactly one corroborated signal; otherwise leave it unchanged and flag it unresolved.
3. **Analytics reconciliation** — for numeric legacy values, numeric equality with legacy `ares_signals.id` is not proof because a zero-stripped display code can match an unrelated bigint key. Require timestamp, setup, direction, and exactly one candidate. For null/display-code rows, search within `|entry_timestamp - signal_timestamp| <= 120 seconds` with the same setup and direction. Zero or multiple candidates are unresolved and are not updated.
4. **ML reconciliation and outcome binding** — populate `ml_collection.signal_uuid` from the verified trade/signal mapping and preserve `signal_display_id`. At close, update only rows matching both `trade_id` and the verified signal UUID. Never select a row by display ID alone. Mark confirmed duplicate snapshots `DUPLICATE_SKIPPED`; leave unknown signal-bearing null bindings `UNRESOLVED`.
5. **Validation gate** — rerun both audits. Any unresolved, mismatched, orphaned, or unbound signal snapshot, or ambiguous row blocks foreign-key validation and primary-key cutover.

### 4.4 Primary-Key Cutover

After the bridge audit is clean, a separate reviewed migration may:

1. stop bridge-mode writers and take the required schema lock;
2. retain the old bigint as `legacy_id`, promote `ares_signals.signal_uuid` to `ares_signals.id`, and recreate the primary key;
3. rename or copy each verified trade-table `signal_uuid` into canonical `signal_id uuid` columns and add foreign keys to `ares_signals(id)`;
4. switch the explicit schema mode to `greenfield` and verify runtime writes;
5. remove legacy columns only in a later cleanup migration after production verification.

No runtime rollout may assume this cutover occurred merely because the additive bridge migration ran.

---

## 5. Component Boundaries and Implementation Assignment

| File | Required change |
|---|---|
| `models.py` | Add client-generated `AresSignal.id`; rename the random code to `display_id`; retain a deprecated presentation-only `signal_id` compatibility property. |
| `storage.py` | Implement explicit bridge/greenfield writes; make `log_signal` return verified success or raise; expose the mode-specific atomic trade-entry RPC; bind ML outcomes by `(trade_id, canonical_signal_uuid)` with retry/terminal telemetry. |
| `position_manager.py` | Make `add_trade` awaitable; call the atomic entry RPC; append to memory only after commit; return `BOUND` or `DUPLICATE_SKIPPED`; reconcile legacy active trades before type enforcement. |
| `main.py` | Gate trade creation and ML binding on successful signal persistence; pass the returned trade ID and binding status to the snapshot; await signal-bound snapshot persistence and entry commit before `update_trades`. |
| `ml_signal/collector.py` | Accept optional `trade_id` and `trade_binding_status`; persist canonical signal identity, `signal_display_id`, and the auditable binding status. |
| `alerts.py` | Render persisted-trade updates from `trade.display_id`, with a legacy fallback only for rows that have not been migrated. |
| `schema.sql`, `ml_signal/schema.sql`, `migrations/` | Document both schema modes, add the bridge migration, add `trade_binding_status`, and create `create_trade_entry_bridge` plus the separately reviewed greenfield cutover migration. |
| `scripts/backfill_signal_uuids.py` | Fail closed in both default and `--apply` modes until the documented read-only reconciliation, ambiguity guards, orphan reporting, and separately reviewed writes are implemented. It must never claim that an audit or validation phase ran when it did not. |
| `tests/` | Cover UUID propagation, signal persistence gates, atomic rollback/idempotent retry, serialized entry/exit behavior, active-trade reconciliation, orphan audits, unresolved signal snapshots, duplicate classification, entry-time ML binding, display-ID alerts, dry-run behavior, and ambiguity skips. |

---

## 6. Definition of Done

- [ ] Human approves this ADR before product-code implementation begins.
- [ ] Bridge and greenfield schema contracts are explicit and mutually exclusive at runtime.
- [ ] A failed signal insert prevents active-trade, analytics, and ML trade creation.
- [ ] Active-trade and analytics entry persistence is one transaction; a partial write cannot leave a restart-visible orphan.
- [ ] Analytics entry commits before the trade can be processed for exits.
- [ ] A signal-bound ML snapshot commits before the trade can be processed for exits, with zero-row label updates retried and surfaced as terminal failures.
- [ ] No runtime writer uses a four-digit display value as a join key.
- [ ] `active_trades`, `trade_analytics`, and `ml_collection` all receive the canonical signal UUID in the selected schema mode.
- [ ] New ML signal snapshots receive the exact trade UUID at entry; exit labeling requires both trade UUID and canonical signal UUID.
- [ ] The read-only audit includes orphan rows from every trade endpoint, unresolved signal-bearing ML snapshots, and distinguishes unresolved analytics keys from missing signals.
- [ ] Confirmed duplicate snapshots have an auditable status and do not block validation; unknown/unresolved bindings do block it.
- [ ] Active-trade rows are reconciled before UUID type changes or final foreign keys.
- [ ] Backfill is dry-run by default and skips zero/multiple-candidate matches without mutation.
- [ ] Persisted trade alerts continue to render the four-digit `display_id` after cutover.
- [ ] Contract/integration tests and the existing repository test suite pass.

## 7. Known Risks and Mitigations

- **Historical ambiguity:** do not mutate zero- or multi-candidate rows; export them for manual review.
- **Partial signal persistence:** surface insert failure, prevent downstream creation, and never fabricate a legacy link.
- **Fast exits:** use the atomic entry transaction, await signal-bound snapshot persistence, and retain bounded zero-row exit-label retry with terminal telemetry.
- **Unbound signal snapshots:** audit signal-generated rows with null trade IDs; classify confirmed duplicates and block on unknown failures.
- **RPC availability:** deploy and validate the mode-specific entry RPC before enabling the corresponding runtime mode; do not silently fall back to two independent inserts.
- **Cutover drift:** gate writers on an explicit schema mode and validate foreign keys before switching modes.
- **Duplicate presentation codes:** keep `display_id` non-unique and prohibit it from all relational predicates.
