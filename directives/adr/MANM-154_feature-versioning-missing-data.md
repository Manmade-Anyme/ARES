# ADR MANM-154: Feature Versioning, Missing Data Remediation, and Storage Contracts

**Date:** 2026-09-12  
**Status:** Proposed  
**Author:** Software Architect Agent  
**Ticket:** [MANM-154](mention://issue/01a09105-82b1-700f-8c26-8b5e24c256a1) / Supabase Audit Item 5  

---

## 1. Problem Statement & Audit Evidence

The ARES Supabase backtesting audit (2026-09-11) revealed significant missing-data inconsistencies across the 14,823 records in `ml_collection`:

1. **`net_delta`** (`greek_features`): Missing in **8,996 of 14,823 rows** (60.69%).
2. **OI Shape Fields** (`oi_features`: `strikes_with_ce_oi`, `max_ce_oi`, `p85_ce_oi`, etc.): Missing in **3,729 of 14,823 rows** (25.16%).
3. **`dist_to_nearest_support`** (`structure_features`): Missing in **6,236 of 14,823 rows** (42.07%).
4. **`dist_to_nearest_resistance`** (`structure_features`): Missing in **2,327 of 14,823 rows** (15.70%).
5. **`trend_continuation` detector score** (`detector_scores`): Missing in **2,606 of 14,823 rows** (17.58%).
6. **Zero-Feature Injection Risk (MANM-49)**: `signal_consumer.py` and legacy ingestion paths defaulted missing options and context data to synthetic zeros (`{"iv": 0, "oi": 0, "gamma": 0, ...}`), poisoning ML feature distributions.
7. **No Explicit Schema Versioning**: The `ml_collection` table lacks an explicit `feature_version` column. Downstream training scripts (`ml_signal/train_offline.py`, `ml_signal/dataset.py`) treat all 14,823 rows as homogeneous, leaving tree-based models vulnerable to learning spurious temporal artifacts resulting from schema evolution rather than true market microstructure patterns.

---

## 2. Root-Cause Historical Audit

A comprehensive git commit and schema archaeology was conducted to identify the exact origin of each missingness pattern:

| Feature / Group | Missing Count | % Missing | Root Cause Classification | Exact Commit & Date | Description |
|---|---|---|---|---|---|
| `greek_features -> net_delta` | 8,996 | 60.69% | **Schema Evolution** (Feature Addition) | PR #98 / `a1b67ce` (2026-08-21 11:16:35 IST) | Introduced in TASK-4c. Prior to this commit, `OptionRow.delta` was not forwarded from `MLCollector`, and `compute_greek_features()` did not compute `net_delta`. |
| `oi_features -> OI Shape` | 3,729 | 25.16% | **Schema Evolution** (Feature Addition) | TASK-194 / `ec2a84f` (2026-07-31 18:44:34 IST) | Introduced in TASK-194. Previously, `compute_oi_features()` computed only scalar totals (`total_ce_oi`, `pcr_oi`). Per-strike distribution metrics (`strikes_with_*_oi`, `max_*_oi`, `p85_*_oi`) did not exist. |
| `dist_to_nearest_resistance` | 2,327 | 15.70% | **Bug Remediation & ATH Regime** | TASK-194 / `ec2a84f` & TASK-195 / `08c36e3` (2026-07-31) | (1) Prior to TASK-195, unknown levels were encoded with a literal `100.0` sentinel. TASK-195 nulled 3,353 resistance sentinels in place.<br>(2) Market regime: During July/August 2026, NIFTY frequently traded at all-time highs above all calculated pivot resistance lines, leaving `resistances` empty. |
| `dist_to_nearest_support` | 6,236 | 42.07% | **Bug Remediation & Session Boundaries** | TASK-194 / `ec2a84f` & TASK-195 / `08c36e3` (2026-07-31) | (1) TASK-195 nulled 1,873 support sentinels.<br>(2) Morning opening gap-ups and early cycles often lacked levels below spot or ran prior to CPR level hydration. |
| `detector_scores -> trend_continuation` | 2,606 | 17.58% | **Schema Evolution & Enum Iteration Fix** | `782a240` (2026-07-28 12:02:37 IST) | TASK-177 introduced the detector, but `collector.py` hardcoded 3 detectors and evaluated `str(SetupType.X)` (`"SetupType.OI_WALL_REJECTION"` != `"OI_WALL_REJECTION"`). Commit `782a240` iterated `SetupType` dynamically, adding the `trend_continuation` key. |
| **Zero-Injection (MANM-49)** | Transient | N/A | **Runtime Integration Defect** | Issue MANM-49 / `signal_consumer.py` | `signal_consumer.py` initialized missing options data as `{iv: 0, oi: 0, gamma: 0}`. In `dataset.py`, absent features previously defaulted to `0.0` before TASK-199 (`375a59b`) corrected them to `np.nan`. |

---

## 3. Architectural Decisions

### Decision 1: Explicit `feature_version` Schema & Temporal Partitioning

We will add a first-class `feature_version` column to `ml_collection` and partition the table into four distinct historical epochs:

```sql
ALTER TABLE ml_collection ADD COLUMN IF NOT EXISTS feature_version integer NOT NULL DEFAULT 4;
CREATE INDEX IF NOT EXISTS idx_ml_collection_feature_version ON ml_collection (feature_version);
```

#### Defined Schema Epochs

*   **Version 1 (`feature_version = 1`, Legacy Inception):**
    *   **Boundary:** Snapshots recorded before `2026-07-28T06:32:37Z` (commit `782a240`).
    *   **Characteristics:** 7 initial feature groups; no `trend_continuation` in `detector_scores`; no OI shape fields; no option delta (`net_delta` absent); naive IST timestamps (remediated to UTC in TASK-206); structure distance sentinels nulled in TASK-195.
    *   **Approximate Rows:** ~2,606 rows.
*   **Version 2 (`feature_version = 2`, Dynamic Setup Enums):**
    *   **Boundary:** `2026-07-28T06:32:37Z` to `2026-07-31T13:14:34Z` (commit `ec2a84f`).
    *   **Characteristics:** `trend_continuation` present in `detector_scores`; real DTE computation; still lacks OI shape fields and `net_delta`.
    *   **Approximate Rows:** ~1,123 rows.
*   **Version 3 (`feature_version = 3`, OI Chain Shape & Join Key Integrity):**
    *   **Boundary:** `2026-07-31T13:14:34Z` to `2026-08-21T05:46:35Z` (commit `a1b67ce`).
    *   **Characteristics:** Real `signal_id` (`db_id`) join keys; OI shape fields (`strikes_with_*_oi`, `max_*_oi`, `p85_*_oi`) populated; structural sentinels replaced with native `None`; lacks `net_delta`.
    *   **Approximate Rows:** ~5,267 rows.
*   **Version 4 (`feature_version = 4`, Full Modern Suite - CURRENT):**
    *   **Boundary:** `2026-08-21T05:46:35Z` to Present.
    *   **Characteristics:** All modern features active, including `net_delta` in `greek_features`, `detector_scores` fed into training, `trade_score` column present, and `oi_wall_context` telemetry logged.
    *   **Approximate Rows:** ~5,827 rows.

All new rows written by `MLCollector.snapshot()` will be stamped with `feature_version = 4`.

---

### Decision 2: Strict Invariant on Missing Data Representation (`NULL`/`NaN` vs Zero)

1.  **Database & Serialization Contract:**
    *   Any missing or uncomputed market metric MUST be serialized as `null` in Postgres JSONB blobs and SQL columns (Python `None`).
    *   Coercing missing features to `0.0` or arbitrary sentinel values (e.g. `100.0`, `-999.0`) is strictly forbidden.
    *   A value of `0.0` is reserved exclusively for genuine, measured zero values (e.g. exactly 0 OI change, 0 distance to a level currently touched by spot price, or 0 net delta in an exactly balanced delta-neutral book).
2.  **Runtime Ingestion Invariant (Eliminating MANM-49):**
    *   In `signal_consumer.py`, when option data or level context is unavailable during event-triggered inference, arguments must be passed as `None` or omitted, allowing the feature computation functions to output `None` rather than zero-filled dictionaries.
3.  **Feature Matrix Contract (`ml_signal/dataset.py`):**
    *   `dataset.flatten_features()` must ensure every `None` or missing dictionary key is flattened into `np.nan`.
    *   `_numeric_only()` must continue dropping `None` values so they arrive as unassigned columns in `flatten_features()` and receive `np.nan`.

---

### Decision 3: Model Training Policy for Historical Missing Data

We evaluate the three approaches required by the acceptance criteria:

1.  **Option A: Complete Row Deletion (Exclusion):**
    *   *Analysis:* Restricting training to rows where all features are present (`feature_version = 4`) discards 8,996 rows (60.7% of snapshots). More critically, it drops ~60% of the scarce realized trade outcomes (leaving fewer than 100 usable trades for training).
    *   *Verdict:* **Rejected as a global strategy.** Discarding early data blinds the model to the June/July market volatility regimes and cripples realized outcome training.
2.  **Option B: Feature Imputation (Mean / Median / Zero):**
    *   *Analysis:* Imputing `net_delta` with 0.0 injects a synthetic "balanced book" assumption into strong trending markets. Imputing structural distances with the median implies an arbitrary, fictitious distance to support/resistance, severely corrupting tree split boundaries.
    *   *Verdict:* **Strictly Rejected.** Imputation of market features introduces lookahead bias, distorts price relationships, and generates false model confidence.
3.  **Option C (Adopted): Native Missing Value Routing + Explicit Indicator Features + Tiered Training:**
    *   *Mechanism:*
        *   **Tree Native Routing:** XGBoost and LightGBM provide native sparsity-aware split finding, assigning `NaN` values to whichever child node maximizes split gain.
        *   **Structural Indicator Features:** Add boolean missingness indicators for physical boundary features:
            *   `has_nearest_resistance`: `1` if `dist_to_nearest_resistance` is finite, `0` if `NaN`.
            *   `has_nearest_support`: `1` if `dist_to_nearest_support` is finite, `0` if `NaN`.
            *   `has_net_delta`: `1` if `net_delta` is finite, `0` if `NaN`.
            This enables trees to isolate "trading at ATH with no resistance" or "legacy snapshot without delta" without distorting distance magnitudes.
        *   **Tiered Model Architectures:**
            *   *Baseline Invariant Model:* Trains across all versions (`v1`–`v4`, $N=14,823$) using only feature groups that have existed continuously (Candle, Volume, Total OI, ATM Greeks).
            *   *Enriched Production Model:* Trains on `feature_version >= 3` (or `feature_version == 4` when delta is required), where full OI shape and delta are active.
        *   **Cross-Validation Stratification:** Walk-forward cross-validation folds in `ml_signal/train_offline.py` must report metric stability across feature versions to guarantee model performance is not driven by schema transitions.

---

## 4. Alternatives Considered & Rejected

*   **Alternative 1: Dynamic Inferential Backfill of `net_delta` via Historical Option Pricing Models.**
    *   *Reason for Rejection:* Would require fetching complete tick-by-tick option historical books from Dhan or calculating historical implied volatility surfaces retrospectively, which is error-prone, non-deterministic, and violates the read-only audit directive.
*   **Alternative 2: Global Schema Reset (Truncating `ml_collection`).**
    *   *Reason for Rejection:* Truncating would discard 3+ months of live market feature observations, destroy valuable negative samples, and delay model retraining by months.
*   **Alternative 3: Semantic Versioning Strings (e.g. `'v4.1.0'`).**
    *   *Reason for Rejection:* Integer versions (`integer`) allow efficient indexed range queries (e.g. `WHERE feature_version >= 3`), fast sorting, and compact storage in PostgreSQL.

---

## 5. File-Level Implementation Instructions for Code Generator

Implementation of this ADR is assigned to the **Code Generator Agent** under issue MANM-154 upon human approval.

### File 1: `ml_signal/config.py`
*   Add constant:
    ```python
    CURRENT_FEATURE_VERSION: int = 4
    ```
*   Add `feature_version: int = CURRENT_FEATURE_VERSION` to `MLConfig`.

### File 2: `ml_signal/schema.sql`
*   Update `ml_collection` table definition to add:
    ```sql
    feature_version integer NOT NULL DEFAULT 4,
    ```
*   Add index:
    ```sql
    CREATE INDEX IF NOT EXISTS idx_ml_collection_feature_version ON ml_collection (feature_version);
    ```

### File 3: `migrations/2026-09-12-manm154-feature-versioning.sql`
*   Create a migration script:
    1.  Add `feature_version` column with default 4.
    2.  Idempotently backfill historical epochs based on UTC timestamp cutoffs:
        ```sql
        -- Version 1: Initial inception to commit 782a240
        UPDATE ml_collection
        SET feature_version = 1
        WHERE timestamp < '2026-07-28T06:32:37Z';

        -- Version 2: Dynamic setup enum to TASK-194
        UPDATE ml_collection
        SET feature_version = 2
        WHERE timestamp >= '2026-07-28T06:32:37Z' AND timestamp < '2026-07-31T13:14:34Z';

        -- Version 3: OI shape & join integrity to TASK-4c
        UPDATE ml_collection
        SET feature_version = 3
        WHERE timestamp >= '2026-07-31T13:14:34Z' AND timestamp < '2026-08-21T05:46:35Z';

        -- Version 4: TASK-4c onwards (net_delta active)
        UPDATE ml_collection
        SET feature_version = 4
        WHERE timestamp >= '2026-08-21T05:46:35Z';
        ```

### File 4: `ml_signal/collector.py`
*   In `MLCollector.snapshot()`:
    *   Include `"feature_version": self.config.feature_version` in the inserted `record` dictionary.

### File 5: `ml_signal/signal_consumer.py`
*   Eliminate synthetic zero dictionaries (fixing bug MANM-49).
*   Replace:
    ```python
    atm_ce={"iv": 0, "oi": 0, "oi_change_pct": 0, "gamma": 0, "theta": 0, "vega": 0},
    atm_pe={"iv": 0, "oi": 0, "oi_change_pct": 0, "gamma": 0, "theta": 0, "vega": 0},
    total_ce_oi=0,
    total_pe_oi=0,
    ```
    with:
    ```python
    atm_ce=None,
    atm_pe=None,
    total_ce_oi=None,
    total_pe_oi=None,
    ```
    ensuring `predict_from_raw()` correctly evaluates absent options as `np.nan`.

### File 6: `ml_signal/dataset.py`
*   In `flatten_features()`:
    *   Extract `feature_version = int(r.get("feature_version", 1) or 1)`.
    *   Add `feature_version` column to the returned DataFrame.
    *   Add structural presence indicator features:
        *   `structure__has_nearest_support = (~df["structure_features__dist_to_nearest_support"].isna()).astype(float)`
        *   `structure__has_nearest_resistance = (~df["structure_features__dist_to_nearest_resistance"].isna()).astype(float)`
        *   `greek__has_net_delta = (~df["greek_features__net_delta"].isna()).astype(float)`

### File 7: `ml_signal/train_offline.py`
*   Update `_fetch_ml_collection()` SELECT query to include `feature_version`.
*   In `run_training()` / evaluation:
    *   Log missingness breakdown by `feature_version` in `metrics["missingness_by_feature_version"]`.

### File 8: `scripts/audit_ml_missingness.py`
*   Create a standalone diagnostic script to audit `ml_collection` (via Supabase or exported JSON):
    *   Outputs a table of missingness percentage per feature grouped by date and `feature_version`.
    *   Verifies that no literal sentinels (`100.0`) or zero-injected options exist.

### File 9: `tests/unit/test_manm154_feature_versioning.py`
*   Comprehensive unit test suite verifying:
    1.  `MLCollector.snapshot` stamps `feature_version = 4`.
    2.  Missing options and structural levels evaluate to `np.nan` and never 0.0 or sentinels.
    3.  `dataset.flatten_features` preserves `feature_version` and generates `has_*` indicator columns.
    4.  Migration timestamp cutoffs correctly map test records to versions 1, 2, 3, and 4.
    5.  `signal_consumer.py` does not pass zero dictionaries.

---

## 6. Definition of Done & Acceptance Verification

1.  **Schema Versioning Active:**
    *   `feature_version` integer column is added to Supabase `ml_collection`.
    *   All new rows written by `MLCollector.snapshot` carry `feature_version = 4`.
2.  **Historical Rows Categorized:**
    *   Migration script executed to categorize historical rows into v1, v2, v3, and v4 based on verified commit timestamps.
3.  **Missing Value Invariant Enforced:**
    *   No synthetic zero dictionaries passed in `signal_consumer.py`.
    *   Missing features evaluate to `np.nan` in `flatten_features()`.
    *   Structural presence indicators (`has_nearest_support`, etc.) available for model training.
4.  **Audit Report Available:**
    *   `scripts/audit_ml_missingness.py` produces the missingness breakdown by date, market session, and feature version.
5.  **Test Verification:**
    *   All new unit tests in `test_manm154_feature_versioning.py` pass cleanly.
    *   Full existing ARES test suite passes with zero regressions.
