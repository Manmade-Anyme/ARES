# TASK-073 Phase 1 Implementation Specification

This specification is subordinate to `directives/adr/TASK-073_oi-rejection-entry-decoupling.md`. It is implementation guidance, not approval to modify runtime code.

## Behavioural sequence

1. On every closed-candle evaluation, `OIWallDetector` selects the nearest qualifying CE wall above spot or PE wall below spot using the existing thresholds.
2. It emits/updates an `OIWallBias` keyed by wall option side and strike. The bias is tracking-only; it does not create a trade, set cooldown, or apply SL/T1/T2.
3. Consecutive qualifying observations of the same key increment persistence. At three observations, the bias becomes `PERSISTENT`.
4. The entry filter records a favourable excursion away from the defended wall. Until that excursion exists, a return to the wall cannot qualify.
5. The next return within `oi_wall_retest_distance_pts` is accepted only when the candle closes back on the defended side: below a CE resistance wall for bearish bias, above a PE support wall for bullish bias.
6. The qualified re-test close becomes the signal trigger. Existing engine risk construction and R:R validation run afterward.
7. The filter marks the wall `CONSUMED` after a qualified signal. Invalidated or changed walls expire and reset the state.

## State transition table

| Current | Event | Next | Entry allowed |
|---|---|---|---|
| none | qualifying wall observed | `TRACKING` | no |
| `TRACKING` | same wall qualifies and count < 3 | `TRACKING` | no |
| `TRACKING` | same wall qualifies and count >= 3 | `PERSISTENT` | no |
| `PERSISTENT` | favourable excursion | `RETEST_READY` | no |
| `RETEST_READY` | valid secondary re-test rejection | `CONSUMED` after signal | yes |
| any active state | wall changes/disappears/session resets | `EXPIRED` then none | no |
| any active state | cooldown or another detector emits | same state | no; continue advancing |

## Required invariants

- `AresEngine.tick()` remains externally compatible: it still returns `Optional[AresSignal]`.
- `OIWallDetector.update()` and the entry filter are called for every candle, even when the engine is in cooldown or another detector wins priority.
- No initial touch or immediate candle-2 continuation can create an order. A valid Phase 1 entry always includes persistence, favourable excursion, and secondary wall re-test.
- `stop_loss`, `target_1`, and `target_2` are populated only by the existing central engine policy after qualification; the filter never widens SL.
- Non-OI-wall detector paths do not receive or depend on OI-wall context.
- A missing opening-range context remains `NULL`, not a sentinel numeric value.

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
- `alerts.py:format_signal` and `alerts.py:send_discord` render the wall context (`wall_strike`, `persistence_snapshots`, `oi_change_pct`) and structural stop distance without breaking field contracts for other detectors.
- Active trade state transitions (`T1_HIT`, `T2_HIT`, `STOPPED_OUT_AT_BE`, `SL_HIT`) continue to be dispatched via `alerts.py:send_trade_update`.

## Acceptance evidence required from implementation/QA

- Unit tests for every state transition and both direction mirrors.
- A regression test proving detector/filter advancement during cooldown and higher-priority signal emission.
- Storage/collector tests proving identical serialized wall context and no extra API calls.
- Replay report for the 18 reviewed trades with baseline and Phase 1 metrics, including cases that never qualified. The report must explicitly confirm that the fixed stop policy was used.
