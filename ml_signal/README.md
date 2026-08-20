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
| `trainer.py` | Walk-forward training, Optuna tuning, SHAP analysis |
| `train_offline.py` | Offline dataset export & XGBoost model training |
| `train_continuous.py` | Offline continuous market regime training |
| `predictor.py` | In-process runtime inference wrapper (`SignalPredictor`) |
| `collector.py` | In-process feature logger (`MLCollector`) writing to `ml_collection` |
| `live.py` | Optional standalone continuous prediction loop (loads from `models_continuous/`) |
| `signal_consumer.py` | Optional standalone event-triggered prediction on ARES signals |
| `schema.sql` | Supabase `ml_collection` and `ml_predictions` table definitions |

## Feature Categories

- **Candle**: body%, wick%, range%, VWAP distance, body-to-range ratio
- **Volume**: volume ratio vs rolling avg, volume slope
- **IV**: IV level, change(1/5), acceleration, percentile, CE-PE spread
- **OI**: PCR, OI bias, concentration, change%
- **Greeks**: gamma/theta ratio, total vega
- **Structure**: distance to nearest resistance/support, PDH/PDL
- **Context**: `detector_scores` matrix, `signal_direction`
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

## Requirements

- Python 3.10+
- Dhan API credentials
- Supabase access
- See `requirements.txt`
