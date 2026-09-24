# Data Retention and Privacy Policy — Machine Learning System

**Document Version:** 1.0.0  
**Effective Date:** 2026-09-12  
**Task Reference:** TASK-153 / ADR-153  
**Target Entities:** `ml_predictions`, `ml_collection`, `trade_analytics`

---

## 1. Overview & Purpose

The ARES Machine Learning Layer records market microstructure features, raw model predictions, and inference snapshots to support continuous auditing, probability calibration (Brier score tracking, reliability diagrams), and concept drift monitoring.

This document defines the storage footprint, retention windows, automated purge schedules, archival protocols, and information security/privacy controls for all machine learning data assets.

---

## 2. Storage Growth Projections

The system operates across two distinct operational inference modes:

| Metric | Event-Triggered Mode (`source = 'event_triggered'`) | Continuous Mode (`source = 'continuous'`) |
|---|---|---|
| **Trigger Mechanism** | Dispatched when a deterministic trading detector fires a valid trade setup | Evaluated every 60 seconds across the live trading session (09:15 - 15:30) |
| **Inference Frequency** | 2 to 10 predictions per trading day | 375 predictions per trading day |
| **Estimated Record Size** | ~2.5 KB per row (50+ sanitized JSONB features) | ~2.5 KB per row |
| **Daily Storage** | ~5 KB – 25 KB / day | ~937.5 KB / day (~0.92 MB / day) |
| **Monthly Storage** | ~100 KB – 500 KB / month | ~19 MB / month (~20 trading days) |
| **Annual Storage** | **1.25 MB – 6.25 MB / year** | **~230 MB / year** |

---

## 3. Data Retention Policies

### 3.1. Event-Triggered Predictions (`source = 'event_triggered'`)

- **Retention Window:** **Permanent / Indefinite**
- **Rationale:** Event-triggered predictions represent exact model evaluations that influenced real capital allocations. Retaining these records indefinitely is critical for:
  - Regulatory and compliance audit trails.
  - Multi-year trade attribution and post-trade performance analytics.
  - Backtesting and model retraining across historical market regimes (bull, bear, high IV, low IV).
  - Tracking calibration drift across distinct model versions (`v1`, `v2`, `v9`, etc.).
- **Storage Impact:** Minimal (< 10 MB after multiple years of continuous operation).

### 3.2. Continuous Predictions (`source = 'continuous'`)

- **Retention Window:** **90-Day Rolling Window** in active Supabase storage.
- **Rationale:** Continuous 1-minute inferences generate higher volume (~93,000 rows/year). High-frequency operational inferences are primarily valuable for near-term drift monitoring and model warmup.
- **Purge Schedule:** Executed on the first calendar day of every month via maintenance cron or migration script:
  ```sql
  DELETE FROM ml_predictions
  WHERE source = 'continuous'
    AND timestamp < NOW() - INTERVAL '90 days';
  ```
- **Cold Storage Archival Protocol:**
  Prior to purging continuous records from Supabase, rows older than 90 days are exported to compressed Apache Parquet files in `reports/ml/archive/` partitioned by year and month (`year=YYYY/month=MM/continuous_predictions.parquet`). This keeps primary database operational latency low while preserving all data for offline quant research.

---

## 4. Privacy & Information Security

### 4.1. Zero Personally Identifiable Information (PII)

The machine learning tables store strictly quantitative market data and algorithmic state. The following invariants are strictly enforced:

- **Prohibited Data:** No broker account IDs, Dhan client IDs, access tokens, API secrets, IP addresses, names, or user identities are ever serialized into `feature_snapshot` or any database column.
- **Microstructure Features Only:** Input features are derived purely from public exchange data (spot prices, candlestick OHLCV, VWAP, option chain open interest, implied volatility, Greeks, and structural support/resistance levels).
- **Sanitization Verification:** The `sanitize_feature_snapshot()` routine strips prohibited and sensitive keys (including `access_token`, `client_id`, API secrets, passwords, account identifiers, and IP addresses) both at the root level and within nested structures, guaranteeing valid JSONB serialization without credential or PII leakage.

### 4.2. Row Level Security (RLS) & Access Control

Access to `ml_predictions` is strictly restricted at the database level:

1. **RLS Enabled:** Row Level Security is explicitly activated on `ml_predictions`.
2. **Access Revocation:** All permissions are revoked from `PUBLIC`, anonymous users (`anon`), and standard authenticated users (`authenticated`).
3. **Backend Service Role Only:**
   - Only the backend process using `SUPABASE_SERVICE_ROLE_KEY` is authorized to `SELECT` and `INSERT` rows.
   - `PredictionLogger` enforces this contract at initialization and fails closed if `supabase_service_role_key` is not configured.
   - The service role key is stored exclusively in secure deployment environment variables (e.g. Fly.io secrets) and is never committed to source control or exposed to clients.
4. **Append-Only Immutability:**
   - Operational application code never issues `UPDATE` or `DELETE` queries against `ml_predictions` during runtime.
   - Entity linkage between signal, trade, and prediction is performed in a single atomic `INSERT` at trade entry time, preserving immutability.
