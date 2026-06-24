# ML Signal Prediction Module

Standalone XGBoost-based prediction module for ARES. Predicts the probability
that a trade setup will hit Target 1 (T1) before Stop Loss (SL), using
live market data from Dhan API. Zero coupling to ARES — runs as an
independent process.

## Architecture

```
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

## Modules

| File | Purpose |
|------|---------|
| `config.py` | All configurable parameters |
| `features.py` | 50+ feature engineering functions (candle, IV, OI, Greeks, structure, meta) |
| `labeling.py` | Label generation (ARES outcomes + self-labeled candles) |
| `data.py` | Load training data from Supabase / Dhan API |
| `trainer.py` | Walk-forward training, Optuna tuning, SHAP analysis |
| `predictor.py` | Runtime inference wrapper |
| `live.py` | Standalone continuous prediction loop |
| `signal_consumer.py` | Event-triggered prediction on ARES signals |
| `schema.sql` | Supabase `ml_predictions` table |

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

## Requirements

- Python 3.10+
- Dhan API credentials
- Supabase access
- See `requirements.txt`
