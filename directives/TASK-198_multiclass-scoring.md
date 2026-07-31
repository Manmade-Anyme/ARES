# TASK-198: Multi-class Trade Scoring (Entire Repository)

**Status:** TODO (Scheduled for a future session)
**Scope:** `ml_signal/labeling.py`, `storage.py`, `trade_analytics`, `PositionManager`

## Objective
Migrate the entire ARES analytics and ML pipeline from binary classification (Win=1, Loss=0) to a point-based multi-class outcome system. 

## Requirements
When a trade resolves, its outcome must be scored and recorded as follows:
- **T2_HIT**: `2` (Double Win)
- **T1_HIT**: `1` (Win)
- **STOPPED_OUT** (Trailed SL hit after T1): `0` (Break-even)
- **SL_HIT** (Original SL hit): `-1` (Loss)

## Implementation Steps
1. Update `trade_analytics` database logging to include an explicit `score` integer column.
2. Refactor `ml_signal/labeling.py` to map historical `result_state` strings to these exact integer values (2, 1, 0, -1).
3. Upgrade the offline XGBoost training pipeline (`train_offline.py`) to support multi-class classification or regression (predicting expected points) instead of binary logistic regression.
