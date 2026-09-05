---
adr_id: "TASK-073"
title: "OI-wall directional bias, delayed entry qualification, and telemetry"
status: "proposed"
date: "2026-09-05"
author: "Software Architect Agent"
applies_to: "ARES Core / Detectors / Engine / Storage / ML"
issue: "MANM-73 / 01a032c1-b430-700c-acc0-a3681a5c6c5e"
---

# Architecture Decision Record: [TASK-073] OI-wall directional bias, delayed entry qualification, and telemetry

## 1. Status & Context

- **Status**: Proposed; implementation is blocked until a human approves this ADR.
- **Problem**: The OI-wall detector currently turns an early wall rejection into an order-ready `AresSignal`. The useful information is often directional, but the first entry is vulnerable to the opening-session pullback. The requested change improves entry quality without widening the existing stop.
- **Observed failure mode**: The Phase 1 analysis reports 12 of 18 reviewed trades stopped out, with 8 later reaching T1 in the predicted direction after the initial shakeout. This is an entry-timing problem, not authorization to increase stop distance.
- **Current coupling**:
  - `detectors/oi_wall.py::OIWallDetector.update()` owns wall detection, confirmation, confidence, and construction of an `AresSignal`.
  - `engine.py::AresEngine.tick()` receives that signal, applies per-type SL/T1/T2 levels, evaluates R:R, and returns it as a live trade.
  - `storage.py` persists emitted signals/trades, while `ml_signal/collector.py` records generic per-cycle snapshots. Neither is a complete audit trail for a wall that was observed but not entered.
- **Relevant existing constraints**:
  - OI-wall state must advance on every candle, including during cooldown or when a higher-priority detector fires (TASK-188 behavior).
  - Per-setup risk levels remain centrally applied by the engine. Phase 1 must not widen, trail, or otherwise change the fixed SL policy.
  - Existing Supabase writes are best-effort and asynchronous; telemetry must not block the trading loop or call Dhan APIs.

## 2. Decision

Adopt a two-phase OI-wall flow:

1. **Directional phase** — identify and track a qualifying wall rejection as an `OIWallBias`. The bias records wall identity, direction, wall strength, persistence, and context. It is not an order and must not start cooldown or create a trade.
2. **Entry phase** — an `OIWallEntryFilter` consumes the bias and subsequent candles. It qualifies an entry only after the same wall remains dominant for at least three consecutive evaluation snapshots, price makes a favourable excursion away from the wall, and a later pullback/re-test rejects the wall in the bias direction.
3. **Trade construction** — only a qualified entry is converted to the existing `AresSignal`. The engine then applies the existing per-type SL/T1/T2 policy and R:R gate unchanged.
4. **Telemetry phase** — persist the bias and entry-filter state on every relevant evaluation, including non-entry outcomes. Persist the same normalized context on an emitted signal/trade for outcome joins.

The Phase 1 re-test reference is the identified wall strike. VWAP, opening-range midpoint, and other intraday levels are recorded as context when available, but are not alternate entry gates in this phase; adding them as gates requires a later ADR because their session construction and precedence are not currently uniform.

### State and entry rules

- A wall identity is `(wall_option_type, strike)`, where a CE wall above spot is bearish and a PE wall below spot is bullish.
- A qualifying snapshot requires the existing wall size and OI-change thresholds. The same identity must qualify on `oi_wall_persistence_snapshots` consecutive engine evaluations; the default is `3`.
- A wall identity change, invalid wall, session reset, or missing chain resets the consecutive counter and marks the prior bias expired. A consumed wall cannot produce a second entry until it resets.
- Persistence alone never arms a re-test. The filter must first observe an initial wall interaction at any point during `TRACKING` or `PERSISTENT`: a closed candle whose range enters the wall's `oi_wall_initial_interaction_distance_pts` band. A wrong-side close invalidates the bias; a defended-side close records the interaction and is never itself an entry. Interaction and excursion flags are preserved while persistence continues; reaching the third snapshot must not erase an interaction seen on snapshot 1 or 2.
- Favourable excursion is quantitative and is measured only on later closed candles: for a CE wall, a low at least `oi_wall_min_excursion_pts` below the strike; for a PE wall, a high at least that distance above the strike. The candle that records the initial interaction cannot also establish the excursion.
- **Bearish CE wall**: after the price has moved away below the wall, a later candle tests the wall within the configured re-test tolerance and closes back below it as a rejection. **Bullish PE wall** is the mirror image: price moves away above the wall, then tests within tolerance and closes back above it.
- The first wall touch and the initial breakdown/bounce are never entry events. The re-test candle close is the trigger price; the existing `entry_zone_offset_pts` remains the entry-zone convention.
- A `QUALIFIED` decision is provisional. The filter does not consume the wall until the engine acknowledges that the final signal passed cooldown, detector priority, per-type levels, and R:R. Cooldown or priority suppression clears the pending decision, retains `RETEST_READY`, and requires a fresh later re-test; R:R rejection expires the wall. Cooldown never suppresses bias/filter advancement or telemetry.

### System architecture and data flow

```text
OIFetcher + closed candle
          |
          v
  AresEngine.tick()
          |  updates latest_oi_wall_context
          v
  OIWallDetector.update()          main.py: MLCollector.snapshot(
          |                         oi_wall_context=engine.latest_...)
          v                                      |
     OIWallBias                                 v
          |                             ml_collection.oi_wall_context
          v                             (every relevant evaluation)
          |
          v
  OIWallEntryFilter.update()
          |
     wait / expire / qualify
          |
          v
  OIWallDetector.build_signal()
          |
          v
  existing SL/T1/T2 + R:R + cooldown
          |
          +--> Storage.log_signal / AnalyticsLogger.log_entry
                ares_signals.oi_wall_context / trade_analytics.market_context
```

## 3. Phase 1 Implementation Specification

### 3.1 Component boundaries and file assignments

| File | Responsibility after approval |
|---|---|
| `models.py` | Add immutable bias, filter-decision, acknowledgement, and telemetry contracts; keep new fields optional for compatibility. |
| `detectors/oi_wall.py` | Detect wall candidates, maintain wall identity/persistence, calculate relative percentile, and build a signal only from an approved entry decision. It must not emit a live signal directly from the first touch. |
| `detectors/oi_wall_entry.py` | New stateful, deterministic re-test filter. Own favourable-excursion tracking, re-test confirmation, consumed/expired state, and rejection reason. No Supabase or broker imports. |
| `engine.py` | Advance the bias detector and entry filter on every candle; acknowledge only the final outcome after cooldown/priority/risk gates; retain existing risk-level and R:R behavior. Expose the latest wall context through a read-only property without making the collector infer trading state. |
| `main.py` | Pass `engine.latest_oi_wall_context` into the existing `MLCollector.snapshot()` call on every cycle after `engine.tick()`. When `settings.oi_wall_enable_watchlist_alert` is True and `engine.latest_watchlist_event` is present, dispatch `alerts.send_watchlist_alert()`. |
| `alerts.py` | Define `send_watchlist_alert(bias: OIWallBias, spot: float)` with dry-run test seams, render enriched wall context in `send_discord`, and preserve existing alert contracts. |
| `config.py` / `config_profiles.py` | Add profile-backed persistence and re-test parameters with the defaults specified below, plus the opt-in `oi_wall_enable_watchlist_alert` setting. Existing wall thresholds and stop settings remain unchanged. |
| `storage.py` | Persist wall telemetry on `ares_signals` and `trade_analytics` using the normalized serialization contract. Preserve exception suppression and async executor behavior. |
| `ml_signal/collector.py` | Accept optional per-cycle wall context and write it even when no trade signal is emitted. Do not add API calls or change existing feature calculations. |
| `ml_signal/schema.sql` | Document the new `ml_collection.oi_wall_context` JSONB column and indexes if required. |
| `migrations/2026-09-05-task073-oi-wall-entry-telemetry.sql` | Add nullable `ares_signals.oi_wall_context` and `ml_collection.oi_wall_context` JSONB columns; use idempotent `ADD COLUMN IF NOT EXISTS`. `trade_analytics.market_context` already supports the nested payload. |
| `tests/unit/test_oi_wall.py` | Preserve detector coverage and add bias/persistence cases. |
| `tests/unit/test_task073_oi_wall_entry.py` | Cover the filter state machine, both directions, reset/consumption, and no-entry cases. |
| `tests/unit/test_engine_oi_wall_entry.py` | Verify per-candle advancement during cooldown/higher-priority signals and unchanged risk/R:R behavior. |
| `tests/unit/test_storage.py` / `tests/unit/test_ml_collector.py` | Verify serialization (including optional VWAP and opening range), null handling, non-blocking writes, and persistence of non-entry wall observations. |

### 3.2 Public contracts

The following are contracts for implementation; names may only change through an ADR update.

```python
@dataclass(frozen=True)
class OIWallBias:
    wall_key: str                         # stable key: f"{wall_option_type}:{strike}"
    wall_strike: float
    wall_option_type: str                 # "CE" or "PE"
    direction: Direction                  # CE wall -> BEARISH; PE wall -> BULLISH
    trade_option_type: str                # CE for bullish, PE for bearish
    wall_oi: int
    wall_oi_change_pct: float
    relative_percentile: Optional[float] # 0..100 among non-zero same-side strikes
    first_seen: datetime
    last_seen: datetime
    persistence_snapshots: int
    persistence_duration_seconds: float
    state: str                            # TRACKING | PERSISTENT | INTERACTED | RETEST_READY | EXPIRED | CONSUMED
    initial_interaction_timestamp: Optional[datetime]
    initial_interaction_price: Optional[float]
    favourable_excursion_pts: float
    reasons: Tuple[str, ...]


@dataclass(frozen=True)
class OIWallTelemetry:
    bias: Optional[OIWallBias]
    entry_status: str                     # NO_WALL | WAITING | QUALIFIED | EXPIRED | CONSUMED
    filter_state: Optional[str]           # TRACKING | PERSISTENT | INTERACTED | RETEST_READY | QUALIFIED | CONSUMED | EXPIRED | NO_WALL
    rejection_reason: Optional[str]       # Specific rejection/expiration reason or None
    initial_interaction_timestamp: Optional[datetime]
    initial_interaction_price: Optional[float]
    favourable_excursion_pts: Optional[float]
    retest_timestamp: Optional[datetime]
    reference_price: Optional[float]
    vwap: Optional[float]                 # Intraday VWAP from candle context when available
    opening_range: Optional[Dict[str, Optional[float]]]

    def to_dict(self) -> Dict[str, Any]: ...


@dataclass(frozen=True)
class OIWallEntryDecision:
    status: str                           # NO_WALL | WAITING | QUALIFIED | EXPIRED | CONSUMED
    wall_key: Optional[str]
    decision_id: Optional[str]
    bias: Optional[OIWallBias]             # latest filter-owned bias state
    telemetry: OIWallTelemetry             # same-cycle state for engine/main/collector
    trigger_price: Optional[float]
    retest_timestamp: Optional[datetime]
    rejection_reason: Optional[str]
    reference_price: Optional[float]
```

Required method contracts:

```python
OIWallDetector.update(
    self,
    spot: float,
    full_chain: List[Dict[str, Any]],
    candle: OHLCVCandle,
    levels: List[ResistanceLevel],
) -> Optional[OIWallBias]

OIWallEntryFilter.update(
    self,
    bias: Optional[OIWallBias],
    candle: OHLCVCandle,
    levels: List[ResistanceLevel],
) -> OIWallEntryDecision

OIWallEntryFilter.acknowledge(
    self,
    decision: OIWallEntryDecision,
    outcome: str,  # EMITTED | SUPPRESSED_BY_COOLDOWN | SUPPRESSED_BY_PRIORITY | REJECTED_BY_RR
) -> OIWallEntryDecision

OIWallDetector.build_signal(
    self,
    decision: OIWallEntryDecision,
    candle: OHLCVCandle,
    spot: float,
    levels: List[ResistanceLevel],
) -> AresSignal  # consumes decision.bias/telemetry; rejects non-QUALIFIED decisions

AresEngine.latest_oi_wall_context -> Optional[Dict[str, Any]]  # read-only, updated every tick

MLCollector.snapshot(..., oi_wall_context: Optional[Dict[str, Any]] = None) -> None
```

`AresSignal` receives an optional serialized `oi_wall_context` field (last in the dataclass to preserve existing constructor compatibility). Non-OI-wall signals leave it `None`.

### 3.3 Configuration

| Setting | Type | Phase 1 default | Contract |
|---|---:|---:|---|
| `oi_wall_persistence_snapshots` | `int` | `3` | Minimum consecutive qualifying snapshots before re-test qualification is allowed. Must be >= 1. |
| `oi_wall_initial_interaction_distance_pts` | `float` | existing `oi_wall_test_distance` | Distance band that proves the first wall interaction. It is observation geometry, not a stop buffer. |
| `oi_wall_min_excursion_pts` | `float` | existing `oi_wall_test_distance` | Minimum later favourable move from the wall strike before a re-test can arm. Must be measured on a later candle. |
| `oi_wall_retest_distance_pts` | `float` | existing `oi_wall_test_distance` | Maximum wall-distance for the secondary re-test. Reuse the current test-distance default; do not widen stops. |
| `oi_wall_retest_confirmation_candles` | `int` | `1` | One re-test candle must close back on the defended side of the wall. Phase 1 does not add a multi-candle tuning surface. |
| `oi_wall_enable_watchlist_alert` | `bool` | `False` | Opt-in toggle to send Discord watchlist heads-up on transition to `RETEST_READY`. Off by default. |

Existing `oi_wall_min_oi`, `oi_wall_min_oi_change_pct`, confidence scoring, `entry_zone_offset_pts`, per-type stop settings, `target_*`, and `signal_cooldown_minutes` retain their current profile values.

### 3.4 Telemetry payload

`OIWallTelemetry.to_dict()` is the single serialization shape used by Supabase and the ML collector:

```json
{
  "wall_key": "CE:24100",
  "wall_strike": 24100.0,
  "wall_option_type": "CE",
  "direction": "BEARISH",
  "trade_option_type": "PE",
  "wall_oi": 4600000,
  "wall_oi_change_pct": 6.5,
  "relative_percentile": 92.0,
  "persistence_snapshots": 3,
  "persistence_duration_seconds": 120.0,
  "initial_interaction_timestamp": "2026-08-17T03:48:00Z",
  "initial_interaction_price": 24098.0,
  "favourable_excursion_pts": 35.0,
  "entry_status": "WAITING",
  "filter_state": "RETEST_READY",
  "rejection_reason": null,
  "retest_timestamp": null,
  "reference_price": 24100.0,
  "vwap": 24085.5,
  "opening_range": {
    "high": 24135.0,
    "low": 24060.0,
    "midpoint": 24097.5,
    "complete": true
  }
}
```

The opening-range object and VWAP are nullable and must not be fabricated when the session context is unavailable. Relative percentile is the percentage of non-zero same-side strikes with OI less than or equal to the selected wall; no synthetic/interpolated strike is introduced.

Storage requirements:

- `Storage.log_signal` writes the payload to `ares_signals.oi_wall_context`.
- `AnalyticsLogger.log_entry` nests the same payload under `trade_analytics.market_context["oi_wall"]` and retains existing reasons/OI/options-sizing fields.
- `MLCollector.snapshot` writes the payload to `ml_collection.oi_wall_context` on every cycle with a tracked wall, including `WAITING`, `EXPIRED`, and `CONSUMED`; no wall means `NULL`.
- All timestamps use the existing `to_utc_iso` path. No credentials, raw option-chain dumps, or broker response tokens may enter telemetry.

The Discord formatter and watchlist formatter must have deterministic unit-test seams. A dry-run test fixture may render the sample payload without contacting a live webhook; live Discord delivery is verified separately with a test webhook only after implementation approval.

Engine acknowledgement order is mandatory: (1) advance detector/filter, (2) if cooldown suppresses emission, acknowledge `SUPPRESSED_BY_COOLDOWN`, (3) if another detector wins, acknowledge `SUPPRESSED_BY_PRIORITY`, (4) apply per-type levels and R:R, acknowledging `REJECTED_BY_RR` on failure, and (5) acknowledge `EMITTED` only after the OI-wall signal is the final returned trade signal. The returned acknowledgement decision supplies the authoritative post-transition bias/telemetry to the engine. No path may mark a wall consumed before step 5.

### 3.5 Discord Alert & Logging Specification

Discord notifications maintain ARES standard embed styling while making the two-phase lifecycle fully transparent:

1. **Signal Detected Alert (`send_discord`)**:
   Emitted ONLY when secondary re-test confirmation qualifies (`QUALIFIED` -> `CONSUMED`). Embed title, color, and structure remain consistent with existing detectors:
   - **Embed Color**: Green (`#2ecc71` / `3066993`) for Bullish / CE, Red (`#e74c3c` / `15158332`) for Bearish / PE.
   - **Header**: `🚨 🐻 🔴 #{signal_id} SIGNAL DETECTED: OI_WALL_REJECTION (BEARISH)`
   - **Fields**:
     * 🕒 **Time**: `{timestamp} IST`
     * 📍 **Spot**: `{spot:.2f}` (inline)
     * ⚡ **Trade**: `{strike} {option_type}` (inline)
     * ⭐ **Confidence**: `{confidence}` (inline)
     * ✅ **Entry**: `{entry_min:.2f} - {entry_max:.2f}` (Secondary re-test close) (inline)
     * 🛑 **SL**: `{stop_loss:.2f} (Spot Ref)` (+16 pts from trigger, shielded by wall) (inline)
     * 🎯 **Target**: `T1={target_1:.2f} | T2={target_2:.2f}` (inline)
     * 🛡️ **Wall Context**: `{wall_strike} {wall_option_type} ({wall_oi_lakhs:.1f}L contracts, +{oi_change_pct:.1f}%) | {persistence_snapshots}/3 snapshots persistent` (inline: false)
     * 📐 **Option Sizing Calculator**: Suggested lots, premium, option SL/target, delta.
     * 🤖 **ML Prediction**: Model probability & version.
     * 📝 **Reasons**: Bullet list detailing:
       - Wall magnitude and active growth.
       - Confirmed 3+ snapshot persistence without shift.
       - Secondary pullback re-test rejection holding defended side.
       - Structural SL buffer relation to wall strike.

2. **Watchlist / Heads-Up Alert (Opt-in via `oi_wall_enable_watchlist_alert`)**:
   When `settings.oi_wall_enable_watchlist_alert` is set to `True` (default `False`), `main.py` checks `engine.latest_watchlist_event` following `engine.tick()`. If a new wall reaches `RETEST_READY`, `main.py` dispatches `await alerts.send_watchlist_alert(event, spot)` to the configured webhook. A session-level deduplication latch ensures each `wall_key` emits at most one watchlist notification per session, keeping manual traders informed of developing morning structure without issuing an order or triggering cooldown:
   - **Header**: `🛡️ 🟡 #{wall_key} SETUP WATCH: OI_WALL_PERSISTENT ({direction})`
   - **Body**: Wall strike, size, persistence duration, and guidance: *"Awaiting pullback re-test near {wall_strike}. Do NOT chase breakdown."*

3. **Trade Lifecycle Updates (`send_trade_update`)**:
   Standard trade progress updates continue seamlessly:
   - `T1_HIT`: Target 1 Reached! Stop Loss trailed to Entry.
   - `T2_HIT`: Target 2 Reached! Trade Closed with Full Profit.
   - `STOPPED_OUT_AT_BE`: Trailing Stop Loss Hit at Entry. T1 Profit Locked; Trade Closed.
   - `SL_HIT`: Stop Loss Hit. Trade Closed (-16 pts).

### 3.6 Failure Modes and Invalidation Semantics

The system deterministically isolates two distinct failure classes:

1. **Pre-Entry Setup Invalidation (Setup Fails to Qualify)**:
   - **Wall Breach / Penetration**: If price aggressively cuts through the wall and closes on the wrong side (e.g. above CE wall for bearish setup or below PE wall for bullish setup), the secondary re-test fails to close on the defended side. The bias transitions immediately to `EXPIRED` -> `none`.
   - **Wall Shift / Dissolution**: If option writers unwind/roll or another strike becomes dominant, the tracked bias expires.
   - **Runaway Trend Without Pullback**: If price trends strongly away without testing the re-test tolerance band, `RETEST_READY` times out on session reset or when the wall moves.
   - **Pre-Entry Outcome**: Zero orders emitted, zero capital risked, zero cooldown locks. The trader receives only the informational watchlist heads-up (if enabled) without false fills.

2. **Post-Entry Trade Failure (Qualified Signal Hits Stop Loss)**:
   - **Stop-Loss Execution**: If the market absorbs the wall after entry and spot moves 16 points against the position, `position_manager` exits immediately at the fixed 16-pt SL (`SL_HIT`).
   - **Discord Notification**: `alerts.send_trade_update` broadcasts: `🛑 Stop Loss Hit at {price}. Trade Closed (-16.0 pts)`.
   - **Single-Entry Wall Consumption Policy**: Once a signal qualifies, the filter transitions the wall to `CONSUMED`. In Phase 1, **no re-entry is permitted on the same wall identity** during that session. This eliminates multi-stop churn on a broken or compromised strike.
   - **Engine Cooldown**: Configured profile cooldown (`settings.signal_cooldown_minutes`, 15m default profile / 20m expiry profile) engages upon entry, suppressing immediate re-triggers and enforcing trading discipline.
   - **Telemetry Audit**: Full entry-to-exit lifecycle with wall persistence snapshots and opening range context is recorded to `trade_analytics` for post-session postmortem and model retraining.

## 4. Alternatives Considered

1. **Widen the stop** — rejected. It changes the risk budget and does not address the observed premature entry; the issue explicitly keeps SL unchanged.
2. **Add a single fixed delay after the current signal** — rejected. A time delay is blind to wall persistence, price excursion, and re-test structure, and would increase stale-signal risk.
3. **Keep detector output as an order-ready `AresSignal` and filter in storage/alerts** — rejected. The order decision already occurs in `AresEngine`; downstream filtering cannot prevent execution or cooldown mutation.
4. **Use ML as the entry gate in Phase 1** — rejected. It adds model/version and training-data dependencies before the deterministic signal path is measurable. ML telemetry remains observational.
5. **Use VWAP/opening-range levels as alternate mandatory gates now** — deferred. The context is valuable for audit, but a uniform session-level contract and replay evidence are not yet established. Wall re-test is the smallest deterministic Phase 1 gate.

## 5. Performance, Reliability, and Security

- The detector/filter operate on already-fetched candle and chain data. No additional Dhan calls are permitted.
- Wall percentile is O(number of chain strikes) per evaluation, bounded by the existing option-chain size. Do not sort or serialize the raw chain for telemetry.
- Persistence is in-memory and reset on process/session reset; durable audit comes from ML snapshots and emitted signal/trade rows. Database writes remain executor-backed/best-effort.
- The engine must still call the detector and filter on every candle during cooldown and higher-priority detector emission. Only final signal emission is gated.
- Payloads are bounded JSON with numeric/string fields only. Apply existing Supabase RLS/credential handling; never log secrets or raw broker payloads.
- Schema migration must be additive and nullable so old rows, old collectors, and rollback to pre-Phase-1 code remain readable.

## 6. Testing and Verification

- Unit-test wall identity, three-snapshot persistence, reset on identity/validity change, relative percentile, and both bullish/bearish mapping.
- Unit-test that first touch, first breakdown/bounce, insufficient persistence, failed re-test, and cooldown produce no `AresSignal`.
- Unit-test that a valid secondary re-test produces exactly one signal and that the same consumed wall cannot re-enter.
- Assert existing per-type SL/T1/T2 values, fixed stop distance, R:R gate, cooldown, and non-OI detectors are unchanged.
- Assert that an interaction/excursion observed before snapshot 3 is preserved when persistence reaches 3; the common touch-then-move-away sequence must not be forced to touch a third time.
- Assert collector/storage payload equality, null handling, UTC timestamp normalization, and no extra broker/API call.
- Assert `main.py` passes the engine's latest wall context into `MLCollector.snapshot()` for non-entry observations.
- Assert Discord execution and watchlist payloads: no signal alert before final acknowledgement, watchlist output is opt-in, enriched wall fields render, and existing non-OI-wall alert fields remain unchanged. Use a mocked webhook/test channel; do not require a live webhook in the unit suite.
- Replay the 18-trade production sample plus available tick/candle data before enabling live entries. Compare baseline vs Phase 1 on: entry count, SL-hit rate, T1 capture rate, median time-to-SL, average R:R, and P&L. A replay result is a release gate, not a claim of success in this ADR.

## 7. Definition of Done

- This ADR is approved by a human before implementation starts.
- The deterministic two-phase contracts and configuration defaults are implemented with backward-compatible construction paths.
- Wall context is recorded for pending, rejected, qualified, and consumed states without blocking the loop.
- Existing stop policy and non-OI detector behavior pass regression tests.
- The additive migration is applied in the target environment and old rows remain readable.
- Replay evidence is attached to the implementation/QA handoff; live rollout is disabled until the replay acceptance thresholds are agreed.

## 8. Implementation Gate

No production code, tests, migrations, or runtime configuration are changed by this ADR task. After human approval, Project Manager may route the file assignments above to implementation and QA workers.
