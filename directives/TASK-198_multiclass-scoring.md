# TASK-198: Multi-class Trade Scoring (Entire Repository)

**Status:** PARTIALLY IMPLEMENTED (steps 1-2 shipped in PR #61; step 3 outstanding)
**Scope:** `ml_signal/labeling.py`, `storage.py`, `trade_analytics`, `PositionManager`

## Objective
Migrate the entire ARES analytics and ML pipeline from binary classification (Win=1, Loss=0) to a point-based multi-class outcome system. 

## Requirements — as originally specified
When a trade resolves, its outcome must be scored and recorded as follows:
- **T2_HIT**: `2` (Double Win)
- **T1_HIT**: `1` (Win)
- **STOPPED_OUT** (Trailed SL hit after T1): `0` (Break-even)
- **SL_HIT** (Original SL hit): `-1` (Loss)

## As implemented — and why it differs

`PositionManager` now emits `STOPPED_OUT_AT_BE` (not `STOPPED_OUT`) when a
trailed stop is hit, and `storage.log_exit` records:

| result_state        | score | rationale |
|---------------------|-------|-----------|
| `T2_HIT`            | `2`   | ran to the second target |
| `T1_HIT`            | `1`   | first target booked |
| `STOPPED_OUT_AT_BE` | `1`   | T1 was touched, so 40% is already booked — a win |
| `SL_HIT`            | `0`   | original stop |
| `TIME_STOP`         | `0`   | forced to break-even *without* ever touching T1 |
| `STOPPED_OUT`       | `0`   | legacy pre-TASK-198 state |

Two deliberate deviations from the spec above:

1. **`STOPPED_OUT_AT_BE` scores 1, not 0.** It can only be reached after T1 is
   touched (the `_time_stopped` branch in `PositionManager` catches the other
   route to a break-even exit first), and 40% of the position is booked at T1.
   Scoring it 0 would discard a real, banked win.
2. **`SL_HIT` scores 0, not -1.** Keeping every score non-negative lets the
   column map straight onto the ML label with no offset. The loss is already
   carried, with magnitude, in `pnl_points`.

Caveat worth remembering: `STOPPED_OUT_AT_BE` books `pnl_points` ≈ 0.00 because
P&L is measured in spot points and the exit is at entry. `reports.py` counts
wins as `pnl_points > 0`, so the weekly report calls these losses while `score`
calls them wins. The two metrics answer different questions; do not "fix" one to
match the other without deciding which is wanted.

## Implementation Steps
1. ✅ Update `trade_analytics` database logging to include an explicit `score` integer column.
   Applied to production by hand on 2026-08-01; recorded in
   `migrations/2026-08-01-task198-score-columns.sql` (TASK-199).
2. ✅ Refactor `ml_signal/labeling.py` to map historical `result_state` strings to
   integer values — see `classify_ares_outcome`.
3. ⬜ Upgrade the offline XGBoost training pipeline (`train_offline.py`) to support
   multi-class classification or regression (predicting expected points) instead
   of binary logistic regression. **Still binary.** Blocked on data, not code:
   only 110 rows carry a real outcome (29 positives), which is too few to fit
   three classes without overfitting.
