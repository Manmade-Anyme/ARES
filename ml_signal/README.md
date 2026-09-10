# ML Signal Prediction Module

Standalone XGBoost-based prediction module for ARES. Predicts the probability
that a trade setup will hit Target 1 (T1) before Stop Loss (SL), using
live market data from Dhan API. Zero coupling to ARES — runs as an
independent process.

## Architecture

```
┌──────────────────────────────────────────────────────────┐
│  ARES Main Loop (Production In-Process)                  │
│                                                          │
│  SignalPredictor -> Enriches Discord Alerts (TASK-196)   │
│  MLCollector      -> Logs features to ml_collection      │
└──────────────────────────┬───────────────────────────────┘
                           │
                           ▼
               ┌──────────────────────┐
               │  ml_collection       │
               │  (Supabase table)    │
               └──────────────────────┘

Optional Standalone Processes:

┌─────────────────────┐     ┌─────────────────────┐
│  live.py            │     │  signal_consumer.py │
│  (continuous)       │     │  (event-triggered)  │
│                     │     │                     │
│  Polls Dhan directly│     │  Polls ares_signals │
│  Every 60s          │     │  Every 5s           │
│  Logs to            │     │  Logs predictions   │
│  ml_predictions     │     │  linked to signal   │
└─────────────────────┘     └─────────────────────┘
         │                           │
         └───────────┬───────────────┘
                     ▼
          ┌─────────────────────┐
          │  ml_predictions     │
          │  (Supabase table)   │
          └─────────────────────┘
```

> **Production Note**:
> Production ARES uses the **in-process `SignalPredictor`** (TASK-196) inside `main.py`, which enriches live Discord alerts with forward probabilities and persists nothing. Data collection is handled in-process by `MLCollector` writing to `ml_collection`. `live.py` and `signal_consumer.py` are optional standalone processes; the `ml_predictions` table is only written when running those optional scripts.

## Modules

| File | Purpose |
|------|---------|
| `config.py` | All configurable parameters |
| `features.py` | 50+ feature engineering functions (candle, IV, OI, Greeks, structure, meta) |
| `labeling.py` | Label generation (ARES outcomes + self-labeled candles) |
| `data.py` | Load training data from Supabase / Dhan API |
| `trainer.py` | Walk-forward training and Optuna tuning |
| `train_offline.py` | Offline dataset export & XGBoost model training |
| `backfill_labels.py` | Dry-run-first repair and catch-up script for `ml_collection` join keys, outcomes, historical BE-after-T1 P&L, orphan trade links, and legacy structure sentinels |
| `predictor.py` | In-process runtime inference wrapper (`SignalPredictor`) |
| `collector.py` | In-process feature logger (`MLCollector`) writing to `ml_collection` |
| `live.py` | Optional standalone continuous prediction loop |
| `signal_consumer.py` | Optional standalone event-triggered prediction on ARES signals |
| `schema.sql` | Supabase `ml_collection` and `ml_predictions` table definitions |

## Feature Categories

- **Candle**: body%, wick%, range%, VWAP distance, body-to-range ratio
- **Volume**: volume ratio vs rolling avg, volume slope
- **IV**: IV level, change(1/5), acceleration, percentile, CE-PE spread
- **OI**: PCR, OI bias, concentration, change%
- **Greeks**: gamma/theta ratio, total vega
- **Structure**: distance to nearest resistance/support, PDH/PDL
- **Meta**: DTE, expiry day flag, session phase

## Usage

### Continuous mode
```bash
DHAN_CLIENT_ID=... DHAN_ACCESS_TOKEN=... \
SUPABASE_URL=... SUPABASE_KEY=... \
python -m ml_signal.live
```

### Event-triggered mode
```bash
SUPABASE_URL=... SUPABASE_KEY=... \
python -m ml_signal.signal_consumer
```

### Training
```python
from ml_signal.trainer import train_pipeline

df = load_training_data(...)
model, metrics = train_pipeline(df, feature_cols, run_optuna=True)
```

### Offline training and SHAP reporting

Run the offline trainer (it reads source data without changing live systems) with:

```bash
python -m ml_signal.train_offline
```

The chronological held-out test split is explained with `shap.TreeExplainer`,
using the fitted model's `best_iteration`. On Python 3.10 SHAP/XGBoost
compatibility failures, the trainer falls back to XGBoost's native exact
TreeSHAP contributions and checks additivity in raw-margin units. If the SHAP
package is genuinely missing, explanation is skipped as a nonfatal condition.

Training writes the model under `ml_signal/models/`, a canonical report at
`reports/ml/task183_offline_metrics.json`, a versioned report at
`reports/ml/v{n}_offline_metrics.json`, and (when SHAP is available) a
headless 150-DPI bar chart at `reports/ml/v{n}_shap_summary.png`. Reports
include the model version, stable SHAP status/backend/output-unit fields, and
an annualized Sharpe diagnostic computed from daily realized NIFTY spot P&L at
a zero risk-free rate. The Sharpe diagnostic reports its date window and trade
count; it is not a capital-return or after-cost Sharpe.
SHAP values are mean absolute contributions in raw-margin/log-odds units, with
the top 15 features retained.

This is offline analysis only: it does not change training predictions or live
inference. SHAP describes association and model behavior, not causation, and
does not automatically improve a model metric.

### Label and P&L repair
```bash
SUPABASE_URL=... SUPABASE_KEY=... \
python -m ml_signal.backfill_labels
```

The backfill command is report-only by default. Re-run with `--apply` after
reviewing the dry-run output to write repairs, including historical
`STOPPED_OUT_AT_BE` rows whose stored P&L must be recalculated from exact
`active_trades` or `ares_signals` target sources.

## Requirements

- Python 3.10+
- Dhan API credentials
- Supabase access
- See `requirements.txt`
