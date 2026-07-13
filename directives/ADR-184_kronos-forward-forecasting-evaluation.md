# ADR-184 — Evaluation: Can Kronos Forward-Forecasting Add Value to ARES's ML?

**Date:** 2026-07-11
**Status:** proposed (evaluation / design memo — no code)
**Related:** ADR-183 (offline labeling & XGBoost training), TASK-009 (ml_collection pipeline), TASK-007 (trade_analytics)
**Subject:** [`shiyu-coder/Kronos`](https://github.com/shiyu-coder/Kronos) — a foundation model for financial K-line (OHLCV) forecasting

---

## TL;DR

- ARES's XGBoost sees **only backward-looking, point-in-time features**. It has **no
  model of where price is likely to go next**. That is its single biggest structural
  limitation, independent of Kronos.
- **Kronos forecasts exactly that** — the forward OHLCV path — and it consumes
  **only OHLCV**. It therefore *cannot* replace ARES's real edge (options
  microstructure: OI / IV / Greeks), but it **cleanly complements** it.
- Recommended role: **Kronos as a forward-looking feature source** feeding the
  existing XGBoost, with the flagship feature being a **Monte-Carlo barrier-hit
  probability** — a Kronos-native estimate of the *exact* quantity XGBoost already
  predicts.
- Recommendation: prove it with an **offline A/B spike gated on an AUC bar** before
  any live wiring — the same discipline ADR-183 applied to XGBoost itself. There are
  also several **Kronos-independent** training wins worth doing regardless.
- Honest blocker to validate early: NIFTY is an **index** (`security_id "13"`,
  `IDX_I`) with **no real traded volume**, and Kronos's tokenizer expects volume.

---

## 1. Where ARES's ML stands today

### 1.1 What the model actually predicts
The `ml_signal/` module trains an XGBoost **binary classifier** that estimates a
*"tradeable-move-imminent"* probability: from the current bar, does NIFTY spot reach
**+`tp_points` (35)** before **−`sl_points` (25)** within **`lookforward` (5)**
candles — in either direction (`MLConfig`, `dataset.label_forward_points`). The
output is bucketed into HIGH / MEDIUM / LOW confidence (`predictor.SignalPredictor`).

### 1.2 The features are entirely backward-looking
`features.build_feature_vector` assembles ~50 features across 7 groups — candle,
volume, IV, OI, Greeks, structure, meta. **Every one is a snapshot of the present or
recent past.** The only forward-looking element in the whole pipeline is the *label*,
which peeks at future candles — and future candles are, by definition, **not
available at inference time**. So the model is asked to predict the future from a
photograph of the present.

### 1.3 There are two distinct training data paths
This matters directly for *"how do we improve training."*

| Path | Source table | Label | Data reality (per ADR-183) |
|------|--------------|-------|-----------------------------|
| `data.py` `source="ares"` → `label_from_ares_outcome` | **`trade_analytics`** (realized trades) | T1/T2 hit = 1, SL = 0 | **~38 rows**, sparse features (reasons text + confidence + basic OI). Trade-rate-limited (~1/day). |
| `train_offline.py` (ADR-183) → `build_labeled_frame` | **`ml_collection`** (rich 60s snapshots) | self-labeled forward-return, day-bounded | **~2,669 rows, +350/day**, full 50-feature vector. |

The "recorded Supabase trades" training you're describing is the **first** path. The
**second** path (ADR-183) is the higher-volume one and is where the current
provisional model comes from.

### 1.4 The current model is provisional and small-data-bound
`reports/ml/task183_offline_metrics.json`: **AUC 0.547**, 168 labeled samples,
positive rate 0.39. AUC ~0.55 is *barely above random* (0.50). Two structural causes:
- **Not enough data.** `trainer.walk_forward_split` needs 6 months train + 1 month
  test → **0 folds** on ~1 month of history; only the chronological-split offline
  path runs at all today.
- **A backward-only feature set** may simply lack the signal to forecast a forward
  move — which is exactly the gap Kronos targets.

---

## 2. What Kronos is

A **decoder-only transformer foundation model** for financial candlestick
forecasting, two-stage:
1. a **tokenizer** that converts continuous OHLCV into hierarchical discrete tokens, and
2. an **autoregressive transformer** pretrained on those token sequences across 45+
   global exchanges.

- **Input:** a sequence of past OHLCV candles + timestamps + the future timestamps to
  predict. **Output:** a **forecast of the forward OHLCV path**, with *probabilistic*
  sampling (temperature / nucleus) — so you can draw **many** future paths, not one.
- Pretrained checkpoints on Hugging Face: **mini 4.1M** (ctx 2048), **small 24.7M**,
  **base 102.3M** (large is access-restricted). **MIT license**, PyTorch,
  `KronosPredictor` convenience API. Fine-tuning supported via a Qlib pipeline.
- **Critical constraint:** it ingests **only OHLCV(+volume)**. It knows nothing about
  option OI, IV, or Greeks — ARES's actual differentiators.

**Conclusion:** Kronos and ARES are strong on *opposite* axes. Kronos = forward price
dynamics from raw candles; ARES = current options microstructure. This is the textbook
setup for **complement, not replace.**

---

## 3. How Kronos adds value

### 3.1 It fills the exact gap — forward-looking features
Run Kronos on the recent NIFTY 1-min OHLCV window each cycle, sample **K** forecast
paths over ARES's own horizon, and turn those paths into features the classifier has
never had:

- **`kr_barrier_prob_bull` / `kr_barrier_prob_bear` / `kr_barrier_prob_any`** — the
  fraction of sampled paths that clear **+tp before −sl** (and the bearish mirror),
  measured over the *same* `lookforward` / `tp_points` / `sl_points` the label uses.
  **This is a Kronos-native estimate of the exact target XGBoost is trained on** — the
  single most valuable derived feature, usable both as an input *and* as a standalone
  baseline / second opinion.
- **`kr_exp_return_pts`** — mean forecast return over the horizon (points).
- **`kr_fwd_vol_pts`** — dispersion across sampled paths (a forward volatility read
  that IV only proxies indirectly).
- **`kr_max_fav_exc_pts` / `kr_max_adv_exc_pts`** — expected max favorable / adverse
  excursion, directly relevant to whether T1 is reachable before SL.

Because these are genuinely orthogonal to the existing backward features, they are the
most plausible source of the lift the current AUC (0.547) is missing.

### 3.2 Three possible roles (recommended one marked)
| Role | What it means | Verdict |
|------|----------------|---------|
| **(a) Forward features → XGBoost** | Kronos outputs become new columns in the existing classifier | **Recommended.** Lowest risk, directly attacks the gap, reuses the whole pipeline. |
| (b) Standalone ensemble / gate | Kronos's barrier-hit probability confirms or vetoes ARES detector signals independently of XGBoost | Useful as a *second opinion*; keep for Phase 2. |
| (c) Both | The spike produces both for free (the barrier-prob is a feature *and* a baseline) | Natural outcome of evaluating (a). |

---

## 4. Improving training & prediction — the Kronos-independent wins

These are worth doing **regardless** of Kronos and will compound with it:

1. **Fuse the two label sources.** Today the rich features (`ml_collection`) and the
   *real* outcomes (`trade_analytics`) live in separate tables and separate training
   paths. The rich snapshots are self-labeled by a *proxy* (forward index move), while
   the realized trades carry the *true* P&L outcome but almost no features. Joining
   snapshot features to realized trade outcomes (by signal_id / timestamp) gives you
   feature-rich, ground-truth-labeled rows — the highest-quality training data you can
   produce. (ADR-183 explicitly flagged this as future work; it remains the biggest
   single lever.)
2. **Grow data before trusting walk-forward.** With ~1 month of history, keep the
   chronological split; graduate to `walk_forward_split` only once ≥7 months exist.
   Report **`precision@20%`** as the North-Star metric (already implemented) — for a
   scalping filter, precision on the highest-confidence slice matters more than AUC.
3. **Calibrate probabilities.** `CalibratedClassifierCV` is already imported in
   `trainer.py`; turning it on makes the HIGH/MEDIUM/LOW thresholds mean what they say.
4. **Back-fill `ml_collection` trade outcomes** on trade close (a later, live-path
   change) so every collected row eventually carries a real label — this is what turns
   the trickle of trades into a compounding, ground-truth dataset over time.

---

## 5. Recommended path (offline-first, gated)

Mirrors ADR-183's "additive, offline, read-only, gate on an AUC bar" discipline.

- **Phase 0 — Offline proof-of-value spike.** A read-only script computes the Kronos
  feature block per `ml_collection` row (strictly no look-ahead: only candles at/before
  row *i*; day-bounded like `build_labeled_frame`), joins it to the existing labeled
  frame, and retrains XGBoost **twice** — baseline vs. +Kronos — reporting the
  AUC / precision@20% delta and SHAP. Disk-only outputs; **zero** live-path impact.
- **Phase 1 — Promote to an 8th feature group** in `features.build_feature_vector`
  and `collector.snapshot` (flag-gated, default off) *iff* Phase 0 clears the bar.
- **Phase 2 — Optional live inference.** Load Kronos-small/mini once in `live.py` /
  `signal_consumer.py`, CPU inference at the 60s cadence, behind the flag and a latency
  budget; or run the barrier-hit probability as an independent ensemble gate.
- Fine-tuning Kronos on NIFTY history (Qlib) is a later enrichment, only if zero-shot
  underperforms.

**Gate:** proceed past Phase 0 only if the augmented model beats baseline by a
meaningful, stable margin *and* Kronos features rank in SHAP. A negative result is
still a valuable, cheap answer.

---

## 6. Risks / caveats (validate early)

- **NIFTY index has no real volume.** `security_id "13"` / `IDX_I` is the index, not a
  traded instrument; `intraday_minute_data` volume is absent/synthetic, yet Kronos's
  tokenizer expects volume. Mitigation: test a volume-optional mode, substitute NIFTY
  **futures** volume as a proxy, or zero it — decide empirically in Phase 0. *This is
  the first thing to check.*
- **Domain shift / weak zero-shot.** Kronos is pretrained mostly on crypto/equity
  K-lines, likely little NIFTY 1-min index. Zero-shot may underperform; measure before
  investing in fine-tuning.
- **Infra weight.** Adds PyTorch + Hugging Face transformers (large deps + model
  download) to a deliberately lightweight asyncio/Fly.io stack. Contain it in
  `ml_signal/` with its own optional extra (e.g. `ml_signal/requirements-kronos.txt`);
  Phase 0 touches nothing deployed.
- **Latency.** Sampling K transformer paths every 60s — benchmark small/mini on CPU
  before any live wiring (the Phase 2 gate).
- **Value is unproven until measured.** The thesis is strong in principle, but the same
  skepticism ADR-183 applied to XGBoost applies here: no live wiring before the offline
  A/B clears its bar.

---

## 7. Recommendation

**Yes — Kronos can add real value, specifically as a forward-looking feature source
that fills ARES's structural blind spot.** It is not a replacement for the
options-microstructure model and should never be treated as one.

Concrete next step: approve a **Phase 0 offline spike** (separate implementation task)
that quantifies the lift, plus the Kronos-independent training wins in §4 (fuse label
sources, calibrate, keep chronological split). Keep the live path untouched until the
numbers justify it.
