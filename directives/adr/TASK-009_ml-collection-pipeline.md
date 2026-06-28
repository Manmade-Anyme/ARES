# ADR TASK-009: ML Data Collection Pipeline

**Date:** 2026-06-25
**Status:** Approved

## Context

The XGBoost ML module (`ml_signal/`) was implemented for live prediction but requires a trained model (`v1.joblib`) before inference can begin. The existing buggy historical data in `trade_analytics` was cleaned up (TASK-008), leaving only 3 labeled training samples. The live ARES system fetches rich market data every cycle (candle OI IV Greeks option chain levels) but this data was not being recorded for ML training.

We need a data collection pipeline that:
- Records all 50+ ML features from every ARES evaluation cycle without duplicate API calls
- Preserves the full market context and detector decision paths for post-hoc analysis
- Links collected snapshots to trade outcomes when trades eventually close
- Enables model training after sufficient data has accumulated (~1 month)

## Decision

Embed an `MLCollector` hook directly into the ARES main loop (`main.py`) that takes the same data ARES has already fetched, computes features using the existing `features.py` functions, and logs categorized feature snapshots to a new `ml_collection` Supabase table.

## Architecture

```
ARES Main Loop (main.py):
  fetch candle ──► fetch OI/chain ──► build levels
         │
         ▼
    engine.tick()        ←─ existing ARES detection
         │
         ▼
    ml_collector.snapshot()  ←─ NEW: uses same data, no API calls
         │
         ├── compute_candle_features()
         ├── compute_volume_features()
         ├── compute_iv_features()
         ├── compute_oi_features()
         ├── compute_greek_features()
         ├── compute_structure_features()
         └── compute_meta_features()
         │
         ▼
    ml_collection table (Supabase)
         │
    [later: linked to trade_analytics via trade_id]
         │
    trainer.py → v1.joblib → live inference
```

## New Table: `ml_collection`

Stores every ARES evaluation cycle as a row with categorized feature JSON blobs:

- `candle_features` -- body%, wick%, range%, VWAP distance
- `volume_features` -- vol_ratio, vol_slope, vol_above_avg
- `iv_features` -- iv_level, iv_change, iv_acceleration, iv_percentile
- `oi_features` -- pcr_oi, oi_bias, concentration
- `greek_features` -- gamma/theta ratio, total vega
- `structure_features` -- dist to support/resistance, PDH/PDL distance
- `meta_features` -- dte, session_phase, expiry flag

Also stores:
- Signal reference (if a signal fired in this cycle)
- Detector scores (which detector fired and its confidence)
- Raw candle / OI snapshots for repro
- Trade outcome (filled retroactively when trade closes)

## Files Changed

| File | Change |
|------|--------|
| `ml_signal/collector.py` | **New** -- MLCollector class |
| `ml_signal/__init__.py` | Export MLCollector |
| `ml_signal/schema.sql` | Add ml_collection table DDL |
| `ml_signal/setup_db.py` | **New** -- helper to create table via psql |
| `main.py` | +5 lines: init collector + call snapshot() every cycle + startup msg |

## Files NOT Changed

`engine.py`, `detectors/`, `fetchers/`, `position_manager.py`, `storage.py`, `models.py`, `config.py`, `config_profiles.py` — zero changes.

## Data Flow

1. **Startup:** ARES initializes MLCollector. Prints `[+] ML Data Collection Logger: ACTIVE` if table exists.
2. **Every cycle (~60s):** After `engine.tick()` returns, `snapshot()` computes all features from the already-fetched candle/ATM/chain/levels data.
3. **Signal events:** If a signal fired, the row is tagged with `signal_generated=true` + signal metadata + detector scores.
4. **Async insert:** DB write is offloaded to `run_in_executor()` to avoid blocking the event loop.
5. **Heartbeat:** Every 15 minutes, the stats display includes ML snapshot count and signal count.
6. **Training later:** After ~1 month, join `ml_collection` with `trade_analytics` on timestamp window. Use trade outcomes (T2_HIT=1, SL_HIT=0) as labels. Train XGBoost. Deploy model for live inference.

## Zero API Overhead

The MLCollector does **zero** Dhan API calls. It computes features from data already fetched by the PriceFetcher and OIFetcher. The only new operation is one Supabase insert per cycle.

## Getting Started

To enable data collection:
1. Open Supabase Dashboard → SQL Editor
2. Paste contents of `ml_signal/schema.sql`
3. Run it
4. Restart ARES — the collector activates automatically

To verify: check the heartbeat message shows `ML Snapshots: N (Signals: M)`.
