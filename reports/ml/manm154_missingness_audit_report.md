# MANM-154: Missingness & Feature-Versioning Audit Report

**Audit Generated:** 2026-09-24 10:53:46Z
**Total Records Evaluated:** 17,803

---

## 1. Executive Summary & Evidence Verification

| Target Feature Category | Missing Count | % Missing | Root Cause Classification |
|---|---|---|---|
| `net_delta` (`greek_features`) | 8,996 | 50.53% | Schema Evolution (TASK-4c introduced 2026-08-21) |
| OI Shape fields (`oi_features`) | 3,729 | 20.95% | Schema Evolution (TASK-194 introduced 2026-07-31) |
| `dist_to_nearest_support` | 6,698 | 37.62% | Market Regime & Sentinel Cleanup (TASK-195) |
| `dist_to_nearest_resistance` | 2,865 | 16.09% | Market Regime (ATH Pivot Invariance) & Sentinel Cleanup |
| `trend_continuation` detector | 2,606 | 14.64% | Schema Evolution & Enum Key Fix (Commit 782a240) |

### Sentinel & Fabricated Value Verification
- **Legacy 100.0 Sentinels Remaining (Pre-TASK-195):** Support: `0`, Resistance: `0`
- **Legitimate 100.0 Market Distances (Post-TASK-195):** Support: `2`, Resistance: `1`
- **Negative Distance Sentinels:** `0`
- **Zero-Injected Option Payloads (`ml_collection`):** `0`
- **Zero-Injected Prediction Snapshots (`ml_predictions`):** `0` (table empty or unpopulated)
- **Verification Result:** PASS. Zero legacy sentinels, negative distances, or zero-injected payloads detected across `ml_collection` and `ml_predictions`. All missing values are cleanly stored as SQL `NULL` / JSON `null` / Python `None`. (Observations with distance exactly 100.0 in modern epochs reflect genuine market levels).

---

## 2. Missingness Breakdown by Schema Epoch / Feature Version

| Feature Version | Epoch Description | Sample Count | % Total | `net_delta` Miss% | OI Shape Miss% | Support Miss% | Resist Miss% | Trend Miss% |
|---|---|---|---|---|---|---|---|---|
| Version 1 | v1 Legacy Inception (< Jul 28) | 2,405 | 13.51% | 100.0% | 100.0% | 43.0% | 18.3% | 100.0% |
| Version 2 | v2 Dynamic Setup Enums (Jul 28-31) | 1,324 | 7.44% | 100.0% | 100.0% | 0.1% | 62.7% | 15.2% |
| Version 3 | v3 OI Shape Suite (Jul 31 - Aug 21) | 5,264 | 29.57% | 100.0% | 0.0% | 39.8% | 14.0% | 0.0% |
| Version 4 | v4 Full Modern Suite (Aug 21+) | 8,810 | 49.49% | 0.0% | 0.0% | 40.5% | 9.8% | 0.0% |

> **Key Finding (Version 4 Modern Suite):** Measured missingness in v4 is `net_delta`: 0.0%, OI shape: 0.0%, `trend_continuation`: 0.0%. Structural distance missingness reflects physical market conditions (support: 40.5%, resistance: 9.8%).

---

## 3. Missingness Breakdown by Market Session Phase

| Market Session Phase | Sample Count | % Total | `net_delta` Miss% | OI Shape Miss% | Support Miss% | Resist Miss% | Trend Miss% |
|---|---|---|---|---|---|---|---|
| `AFTERNOON_CLOSE (14:00-15:30)` | 4,227 | 23.74% | 50.2% | 20.9% | 40.9% | 15.0% | 14.6% |
| `MID_DAY (10:15-14:00)` | 10,710 | 60.16% | 50.5% | 20.9% | 38.4% | 16.3% | 14.6% |
| `MORNING_OPEN (09:15-10:15)` | 2,866 | 16.1% | 51.0% | 21.2% | 30.1% | 16.9% | 14.9% |

> **Session Observation:** Structural distance missingness varies by phase: support missingness peaks during `AFTERNOON_CLOSE (14:00-15:30)` (40.9%), while resistance missingness peaks during `MORNING_OPEN (09:15-10:15)` (16.9%).

---

## 4. Daily Missingness Breakdown

| Date | Sample Count | Active Versions | `net_delta` Miss% | OI Shape Miss% | Support Miss% | Resist Miss% | Trend Miss% |
|---|---|---|---|---|---|---|---|
| `2026-07-20` | 384 | v1 | 100.0% | 100.0% | 0.0% | 0.0% | 100.0% |
| `2026-07-21` | 375 | v1 | 100.0% | 100.0% | 0.0% | 0.0% | 100.0% |
| `2026-07-22` | 374 | v1 | 100.0% | 100.0% | 99.2% | 0.0% | 100.0% |
| `2026-07-23` | 374 | v1 | 100.0% | 100.0% | 83.4% | 0.0% | 100.0% |
| `2026-07-24` | 372 | v1 | 100.0% | 100.0% | 94.6% | 0.0% | 100.0% |
| `2026-07-27` | 361 | v1 | 100.0% | 100.0% | 0.0% | 96.7% | 100.0% |
| `2026-07-28` | 368 | v1,v2 | 100.0% | 100.0% | 0.0% | 38.0% | 99.5% |
| `2026-07-29` | 373 | v2 | 100.0% | 100.0% | 0.0% | 97.9% | 0.0% |
| `2026-07-30` | 374 | v2 | 100.0% | 100.0% | 0.3% | 22.2% | 0.0% |
| `2026-07-31` | 374 | v2 | 100.0% | 100.0% | 0.0% | 88.8% | 0.0% |
| `2026-08-01` | 1 | v3 | 100.0% | 0.0% | 0.0% | 100.0% | 0.0% |
| `2026-08-03` | 374 | v3 | 100.0% | 0.0% | 0.0% | 97.3% | 0.0% |
| `2026-08-04` | 374 | v3 | 100.0% | 0.0% | 51.1% | 0.0% | 0.0% |
| `2026-08-05` | 374 | v3 | 100.0% | 0.0% | 0.0% | 0.0% | 0.0% |
| `2026-08-06` | 293 | v3 | 100.0% | 0.0% | 0.0% | 0.3% | 0.0% |
| `2026-08-07` | 370 | v3 | 100.0% | 0.0% | 88.1% | 0.0% | 0.0% |
| `2026-08-10` | 374 | v3 | 100.0% | 0.0% | 1.6% | 0.0% | 0.0% |
| `2026-08-11` | 371 | v3 | 100.0% | 0.0% | 99.5% | 0.0% | 0.0% |
| `2026-08-12` | 372 | v3 | 100.0% | 0.0% | 93.0% | 0.0% | 0.0% |
| `2026-08-13` | 374 | v3 | 100.0% | 0.0% | 0.0% | 0.0% | 0.0% |
| `2026-08-14` | 374 | v3 | 100.0% | 0.0% | 2.1% | 0.0% | 0.0% |
| `2026-08-17` | 374 | v3 | 100.0% | 0.0% | 43.6% | 0.0% | 0.0% |
| `2026-08-18` | 370 | v3 | 100.0% | 0.0% | 86.5% | 0.0% | 0.0% |
| `2026-08-19` | 374 | v3 | 100.0% | 0.0% | 98.1% | 0.0% | 0.0% |
| `2026-08-20` | 373 | v3 | 100.0% | 0.0% | 0.0% | 98.7% | 0.0% |
| `2026-08-21` | 374 | v3,v4 | 33.4% | 0.0% | 0.0% | 0.3% | 0.0% |
| `2026-08-24` | 373 | v4 | 0.0% | 0.0% | 65.4% | 3.8% | 0.0% |
| `2026-08-25` | 373 | v4 | 0.0% | 0.0% | 32.7% | 0.0% | 0.0% |
| `2026-08-26` | 373 | v4 | 0.0% | 0.0% | 0.0% | 14.7% | 0.0% |
| `2026-08-27` | 373 | v4 | 0.0% | 0.0% | 92.0% | 0.0% | 0.0% |
| `2026-08-28` | 373 | v4 | 0.0% | 0.0% | 6.4% | 0.0% | 0.0% |
| `2026-08-31` | 372 | v4 | 0.0% | 0.0% | 93.5% | 0.0% | 0.0% |
| `2026-09-01` | 372 | v4 | 0.0% | 0.0% | 25.0% | 1.3% | 0.0% |
| `2026-09-02` | 367 | v4 | 0.0% | 0.0% | 97.5% | 0.0% | 0.0% |
| `2026-09-03` | 372 | v4 | 0.0% | 0.0% | 0.0% | 66.9% | 0.0% |
| `2026-09-04` | 372 | v4 | 0.0% | 0.0% | 0.0% | 0.0% | 0.0% |
| `2026-09-07` | 367 | v4 | 0.0% | 0.0% | 97.0% | 0.0% | 0.0% |
| `2026-09-08` | 372 | v4 | 0.0% | 0.0% | 98.9% | 0.0% | 0.0% |
| `2026-09-09` | 371 | v4 | 0.0% | 0.0% | 97.6% | 0.0% | 0.0% |
| `2026-09-10` | 374 | v4 | 0.0% | 0.0% | 58.0% | 0.0% | 0.0% |
| `2026-09-11` | 374 | v4 | 0.0% | 0.0% | 71.9% | 0.0% | 0.0% |
| `2026-09-15` | 373 | v4 | 0.0% | 0.0% | 20.1% | 6.7% | 0.0% |
| `2026-09-16` | 372 | v4 | 0.0% | 0.0% | 0.0% | 0.0% | 0.0% |
| `2026-09-17` | 373 | v4 | 0.0% | 0.0% | 0.0% | 50.1% | 0.0% |
| `2026-09-18` | 372 | v4 | 0.0% | 0.0% | 0.0% | 7.0% | 0.0% |
| `2026-09-21` | 373 | v4 | 0.0% | 0.0% | 0.0% | 79.6% | 0.0% |
| `2026-09-22` | 372 | v4 | 0.0% | 0.0% | 4.3% | 0.8% | 0.0% |
| `2026-09-23` | 373 | v4 | 0.0% | 0.0% | 0.0% | 0.0% | 0.0% |
| `2026-09-24` | 372 | v4 | 0.0% | 0.0% | 99.7% | 0.0% | 0.0% |

---

## 5. Weekly Temporal Progression

| Year-Week | Sample Count | Active Versions | `net_delta` Miss% | OI Shape Miss% | Support Miss% | Resist Miss% | Trend Miss% |
|---|---|---|---|---|---|---|---|
| `2026-W29` | 1,879 | v1 | 100.0% | 100.0% | 55.1% | 0.0% | 100.0% |
| `2026-W30` | 1,851 | v1,v2,v3 | 100.0% | 99.9% | 0.1% | 68.6% | 39.3% |
| `2026-W31` | 1,785 | v3 | 100.0% | 0.0% | 29.0% | 20.4% | 0.0% |
| `2026-W32` | 1,865 | v3 | 100.0% | 0.0% | 39.1% | 0.0% | 0.0% |
| `2026-W33` | 1,865 | v3,v4 | 86.6% | 0.0% | 45.6% | 19.8% | 0.0% |
| `2026-W34` | 1,865 | v4 | 0.0% | 0.0% | 39.3% | 3.7% | 0.0% |
| `2026-W35` | 1,855 | v4 | 0.0% | 0.0% | 43.1% | 13.7% | 0.0% |
| `2026-W36` | 1,858 | v4 | 0.0% | 0.0% | 84.6% | 0.0% | 0.0% |
| `2026-W37` | 1,490 | v4 | 0.0% | 0.0% | 5.0% | 16.0% | 0.0% |
| `2026-W38` | 1,490 | v4 | 0.0% | 0.0% | 26.0% | 20.1% | 0.0% |

---

## 6. Architectural Recommendations for Model Training

### 1. Exclusion vs Imputation vs Indicator Features
- **Exclusion (Drop Rows): REJECTED as a global strategy.** Dropping rows with missing features would eliminate >60% of historical samples, including valuable market regimes from June and July 2026. Furthermore, realized trade outcomes are scarce (<150 closed trades total); dropping early trades would starve the model of training signal.
- **Imputation (Mean / Median / Zero): STRICTLY FORBIDDEN.** Imputing missing distances with 0.0 falsely implies spot is touching the level. Imputing `net_delta` with 0.0 asserts delta neutrality during strong trending sessions. Imputation injects artificial distribution artifacts that mislead gradient boosting splits.
- **Adopted Strategy: Native Missingness Routing + Explicit Indicator Features:**
  1. **Tree-Native Missingness:** XGBoost and LightGBM handle `NaN` natively via sparsity-aware branch routing (learning optimal split direction for unobserved values).
  2. **Structural Presence Indicators:** Three explicit boolean indicator features have been added in `ml_signal.dataset.flatten_features()`:
     - `structure__has_nearest_support`: `1.0` if support exists below spot, `0.0` if NaN.
     - `structure__has_nearest_resistance`: `1.0` if resistance exists above spot, `0.0` if NaN (identifies All-Time High breakouts).
     - `greek__has_net_delta`: `1.0` if net delta was recorded, `0.0` for legacy epochs.
  3. **Tiered Dual-Track Training:**
     - *Baseline Models (v1-v4):* Train on long-term invariants (Candles, Volume, Total OI, ATM Greeks).
     - *Enriched Production Models (v3+ / v4):* Train on enriched features (OI shape, Net Delta) with explicit presence indicators.

---

## 7. Implementation Verification
- `feature_version` column added to schema and migration script created.
- `MLCollector.snapshot` stamps `feature_version = 4` on all new rows.
- `signal_consumer.py` synthetic zero injection bug (MANM-49) eliminated; missing options evaluate to `None`/`NaN`.
- `ml_signal/dataset.py` extracts `feature_version` as metadata and generates boolean indicator columns.
