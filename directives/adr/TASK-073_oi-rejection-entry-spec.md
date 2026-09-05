# TASK-073 Phase 1 Implementation Specification

This specification is subordinate to `directives/adr/TASK-073_oi-rejection-entry-decoupling.md`. It is implementation guidance, not approval to modify runtime code.

## Behavioural sequence

1. On every closed-candle evaluation, `OIWallDetector` selects the nearest qualifying CE wall above spot or PE wall below spot using the existing thresholds.
2. It emits/updates an `OIWallBias` keyed by wall option side and strike. The bias is tracking-only; it does not create a trade, set cooldown, or apply SL/T1/T2.
3. Consecutive qualifying observations of the same key increment persistence. At three observations, the bias becomes `PERSISTENT`, but persistence alone is not enough for entry. Interaction and excursion are independent monotonic flags until reset, so an event on snapshot 1/2 is not discarded at snapshot 3.
4. The filter records the first wall interaction whenever it occurs, including while persistence is `TRACKING` on snapshot 1 or 2. A candle range entering `oi_wall_initial_interaction_distance_pts` followed by a defended-side close records the interaction; a wrong-side close expires the bias. This first interaction can never be the re-test entry, and its timestamp/price remain attached as persistence continues.
5. On a later candle only, the filter records a favourable excursion of at least `oi_wall_min_excursion_pts` from the wall strike: below the CE wall for bearish bias or above the PE wall for bullish bias. The excursion flag is retained even if persistence has not reached 3 yet. `RETEST_READY` requires both the retained interaction and excursion plus the persistence threshold.
6. On a still-later candle, the next return within `oi_wall_retest_distance_pts` is accepted only when it closes back on the defended side: below a CE resistance wall for bearish bias, above a PE support wall for bullish bias.
7. The filter returns a provisional `QUALIFIED` decision with a `decision_id`; it does not consume the wall. The engine applies cooldown/priority, per-type levels, and R:R, then acknowledges the final outcome.
8. `EMITTED` acknowledgement marks the wall `CONSUMED`. Cooldown/priority suppression returns the wall to `RETEST_READY` and requires a fresh later re-test. R:R rejection expires the wall. Invalidated or changed walls expire and reset the state.

## State transition table

| Current | Event | Next | Entry allowed |
|---|---|---|---|
| none | qualifying wall observed | `TRACKING` | no |
| `TRACKING` | same wall qualifies and count < 3 | `TRACKING` | no |
| `TRACKING` | same wall qualifies and count >= 3, no interaction | `PERSISTENT` | no |
| `TRACKING` | first wall interaction, defended-side close | `INTERACTED` | no; persistence count retained |
| `INTERACTED` | same wall qualifies, count < 3 | `INTERACTED` | no; interaction retained |
| `INTERACTED` | later excursion, persistence < 3 | `INTERACTED` | no; excursion retained |
| `INTERACTED` | persistence >= 3 and excursion already retained | `RETEST_READY` | no; wait for later re-test |
| `PERSISTENT` | first wall interaction, defended-side close | `INTERACTED` | no; persistence count retained |
| `INTERACTED` | later excursion >= configured minimum with persistence met | `RETEST_READY` | no |
| `RETEST_READY` | valid secondary re-test rejection | `QUALIFIED` pending engine acknowledgement | no, provisional |
| `QUALIFIED` | final signal emitted and acknowledged | `CONSUMED` | yes |
| `QUALIFIED` | cooldown/priority suppression acknowledged | `RETEST_READY` | no; require fresh re-test |
| `QUALIFIED` | R:R rejection acknowledged | `EXPIRED` | no |
| any active state | wall changes/disappears/session resets | `EXPIRED` then none | no |
| any state before `QUALIFIED` | cooldown or another detector emits | same state | no; continue advancing |
| none / no qualifying wall | ordinary evaluation | `NO_WALL` | no |

## Required invariants

- `AresEngine.tick()` remains externally compatible: it still returns `Optional[AresSignal]`.
- `OIWallDetector.update()` and the entry filter are called for every candle, even when the engine is in cooldown or another detector wins priority.
- `OIWallEntryFilter.update()` returns `NO_WALL` with `wall_key=None` and `decision_id=None` when no bias is present; telemetry serializes that state as a null wall context.
- No initial touch or immediate candle-2 continuation can create an order. A valid Phase 1 entry always includes persistence, initial interaction, a later quantitative favourable excursion, and a still-later secondary wall re-test.
- The filter never consumes a wall from `update()`. Every `OIWallEntryDecision` returns the latest filter-owned `bias` and `telemetry`; `acknowledge()` returns the post-transition decision so the engine/main/collector cannot observe stale state. Every `QUALIFIED` decision is acknowledged exactly once after the engine's final gates; no signal alert or cooldown is attached to a merely provisional decision.
- `stop_loss`, `target_1`, and `target_2` are populated only by the existing central engine policy after qualification; the filter never widens SL.
- Non-OI-wall detector paths do not receive or depend on OI-wall context.
- A missing opening-range context remains `NULL`, not a sentinel numeric value.

`AresEngine.latest_oi_wall_context` is the read-only handoff consumed by `main.py`. `main.py` passes it to the existing `MLCollector.snapshot(..., oi_wall_context=...)` call after each `engine.tick()`; the collector never reaches into engine/filter internals.

The engine acknowledgement order is deterministic: cooldown suppression, detector-priority suppression, per-type level/R:R rejection, and final signal emission each map to the acknowledgement outcomes in the ADR. `EMITTED` is the only outcome that transitions `QUALIFIED` to `CONSUMED`.

## Database contract

The migration is additive:

```sql
ALTER TABLE ares_signals
  ADD COLUMN IF NOT EXISTS oi_wall_context jsonb;

ALTER TABLE ml_collection
  ADD COLUMN IF NOT EXISTS oi_wall_context jsonb;

CREATE INDEX IF NOT EXISTS idx_ml_collection_oi_wall_strike
  ON ml_collection ((oi_wall_context ->> 'wall_strike'));
```

The index is optional if query plans show no benefit; JSON shape and null semantics are mandatory. `trade_analytics.market_context` already accepts the nested payload and needs no new column.

## Discord notification contract

- Zero Discord signal alerts are sent during `TRACKING`, `PERSISTENT`, or `RETEST_READY` states unless the optional observation watchlist channel is explicitly enabled.
- On `QUALIFIED`, `OIWallDetector.build_signal` includes the wall strike, persistence count, and re-test reason in `reasons` and `oi_wall_context`.
- `QUALIFIED` is provisional: `alerts.send_discord` fires only after an `EMITTED` acknowledgement. A watchlist alert may be sent for `PERSISTENT`/`RETEST_READY` only when the opt-in setting is enabled.
- `alerts.py:format_signal` and `alerts.py:send_discord` render the wall context (`wall_strike`, `persistence_snapshots`, `oi_change_pct`) and structural stop distance without breaking field contracts for other detectors.
- Active trade state transitions (`T1_HIT`, `T2_HIT`, `STOPPED_OUT_AT_BE`, `SL_HIT`) continue to be dispatched via `alerts.py:send_trade_update`.

## Failure and Invalidation Rules

- **Pre-Entry Failure**: If price closes beyond the wall level during re-test, the filter invalidates the candidate and returns `EXPIRED`. No signal is built, no order is placed.
- **Post-Entry SL Hit**: If an active trade hits the 16-pt SL, `position_manager` exits at `SL_HIT`. The wall remains in `CONSUMED` state for the rest of the day.
- **No Re-Entry**: In Phase 1, `OIWallEntryFilter` strictly prevents re-entry on any consumed wall key (`wall_option_type:strike`) in the same trading session.
- **Cooldown**: Standard 15-minute cooldown activates on entry, preventing conflicting executions.
- **Discord testability**: Unit tests render both the opt-in watchlist and final signal payload through a mocked webhook/test channel, asserting no live network call and preserving existing detector alert fields.

## Acceptance evidence required from implementation/QA

- Unit tests for every state transition and both direction mirrors.
- Unit tests for the explicit `NO_WALL` decision, initial-interaction requirement, quantitative later-candle excursion, provisional qualification, and every acknowledgement outcome.
- A regression test proving detector/filter advancement during cooldown and higher-priority signal emission.
- Storage/collector tests proving identical serialized wall context and no extra API calls.
- Integration coverage proving `main.py` forwards the engine's latest wall context into the collector for non-entry observations.
- Alert tests proving the watchlist is opt-in, no signal alert is sent before final acknowledgement, and the sample Discord message renders with the agreed fields.
- Replay report for the 18 reviewed trades with baseline and Phase 1 metrics, including cases that never qualified. The report must explicitly confirm that the fixed stop policy was used.
