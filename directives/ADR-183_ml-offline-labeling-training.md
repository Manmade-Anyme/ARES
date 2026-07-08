# ADR-183 — Offline Labeling & XGBoost Training for `ml_collection`
**Date:** 2026-07-08
**Status:** accepted
**Related:** TASK-183, extends ADR §Layer 7 (Analysis), builds on TASK-007 (trade_analytics)

## Problem
`ml_collection` records a rich 50+ feature snapshot every ~60s cycle (~350 rows/trading day, 2,669 rows to date), and the pipeline is healthy. But the data is **inert**:
1. Its `trade_outcome`/`trade_pnl`/`trade_id` columns are **never back-filled** (0/2669 non-null) — despite the schema comment claiming otherwise.
2. The trainer (`data.py`, `source="ares"`) reads a **different** table, `trade_analytics` (38 rows, sparse features: reasons text + confidence + basic OI), **never `ml_collection`**.
3. Consequently XGBoost is **dormant** — no model artifact, `ml_predictions=0`.

Net: we collect rich features but cannot train on them, and the labeled path is both feature-poor and trade-rate-limited (~1 trade/day now → months to a usable set).

## Constraint
The system is **live on fly.io**. The user's directive: *"make sure the current implementation is untouched."* So the fix must be **additive, offline, read-only** — no change to the collection path, the engine, or deployed behavior.

## Decision

### 1. Label strategy: self-labeled forward-return (bidirectional, points, day-bounded)
Do **not** wait for trade outcomes. Label **every** `ml_collection` row directly from its own forward price path:

> From row *i*'s close, over the next `lookforward` candles (default 5), did NIFTV spot achieve a clean **±`move_points`** excursion — i.e. reach `+tp_points` before `-sl_points` (bull win) **or** `-tp_points` before `+sl_points` (bear win)?
> `label = 1` if a clean directional move completes either way, `0` if the opposing stop is hit first, `-1` (dropped) if neither resolves in the window.

- **Bidirectional** because ARES trades both CE and PE; the useful question is "is a tradeable move imminent," not "will price go up."
- **Points, not percent** — mirrors the engine's point-based targets (`target_1_pts` etc.); the existing `label_candle_forward` uses percent, so we add a new points labeler rather than bend it.
- **Day-bounded** — the forward window must never span the 15:30→09:15 overnight gap; label per trading date. (The existing `label_candle_forward` ignores this; another reason for a new function.)

**Why this over trade-outcome labels:** it turns all 2,669 rows (and +350/day) into labeled samples *today*, decoupled from the trade trickle. It answers the user's real question — *does the collected data carry predictive signal?* — now instead of in six months. Trade-outcome labels remain a valid **future** enrichment (fuse when `trade_analytics` is large), not a blocker.

**What the model means:** a "tradeable-move-imminent" probability from market microstructure features. Near-term use = an analysis/validation artifact (are the features predictive?); later = an optional pre-filter/context score for the live detectors (separate task, only if it clears a bar).

### 2. Feature assembly
Flatten the 7 JSON feature-group columns (`candle/volume/iv/oi/greek/structure/meta_features`) into one numeric matrix, one column per key, stable column order, missing keys → 0.0. Close series for labeling comes from `raw_candle.close`. Read-only SELECT, ordered by timestamp.

### 3. Training: chronological split, not 6-month walk-forward
`trainer.train_pipeline`'s walk-forward needs 6mo train + 1mo test = 7+ months span → **0 folds** on 2 weeks of data. Add a **chronological split** (first *X%* train, last *(1-X)%* test — time-ordered, no leakage). The XGBoost fit/eval/importance helpers are **inlined** in `train_offline.py` (same params from `MLConfig`, same sklearn metrics) rather than imported from `trainer.py`: `trainer.py` hard-imports `optuna` at module top (a heavy hyperparameter-tuning dep the offline path doesn't need and which isn't installed), so importing it would drag that in. Inlining keeps `trainer.py` **untouched** and the offline path dependency-light (xgboost + scikit-learn only). Small-data guard: warn below a sample threshold, still train, mark metrics provisional.

### 4. No DB writes, no new tables
The pipeline reads `ml_collection` and writes only: a model artifact (`ml_signal/models/`) and a metrics/feature-importance report (`reports/ml/`). Zero writes to Supabase → zero risk to the live system. (Deliberately not adding an `ml_collection.label` column or a new table — keeps the blast radius at "disk only.")

## Alternatives considered
- **Back-fill `ml_collection.trade_outcome` from `trade_analytics` on trade close** — requires editing the live `position_manager`/`storage` path (violates the constraint) and is trade-rate-limited. Rejected for now; viable later.
- **Percent-based `label_candle_forward` as-is** — wrong units vs. engine targets, and labels across day boundaries. Rejected.
- **Deploy live inference now** — no trained/validated model and thin data; premature. Deferred to a follow-up task gated on an AUC bar.
- **6-month walk-forward** — impossible with current data span. Deferred until enough history exists.

## Consequences
- (+) Every collected row becomes a training sample immediately; XGBoost actually runs and reports predictive value.
- (+) Live system provably untouched (diff scoped to new files + docs).
- (+) Clear growth path: rerun `train_offline` as data accumulates; graduate to walk-forward + live inference later.
- (−) The near-term label is a proxy (forward index move), not realized trade P&L — documented; fusion with trade outcomes is future work.
- (−) A model trained on ~2 weeks is provisional; metrics are directional, not production-grade.

## Component boundaries
| File | Responsibility | Touches live path? |
|------|----------------|--------------------|
| `ml_signal/dataset.py` (new) | read `ml_collection`, flatten features, forward-points day-bounded labeler | No |
| `ml_signal/train_offline.py` (new) | chronological split, inlined XGBoost train/eval/importance, save model + report, CLI | No |
| `ml_signal/trainer.py` | left untouched (optuna coupling avoided by inlining) | No (unchanged) |
| `tests/unit/test_task183_ml_offline.py` (new) | unit coverage, fully mocked | No |

## Definition of Done
Contract-level tests target the public functions (`flatten_features`, `label_forward_points`, `chronological_split`, `build_labeled_frame`) with synthetic data — no mocking of internals, no live DB. A real run on `ml_collection` produces a model + report.
