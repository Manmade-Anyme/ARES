# Research Protocol: Exhaustion Stop-Distance Audit — Three Data-Derived Wider Stops

## 1. Context and Objective
ARES trades recorded in production under the `EXHAUSTION_REVERSAL` detector have operated with tight initial stop losses (median 10.0 points, maximum 12.0 points). While tight stops limit point loss per trade, they may prematurely stop out positions that experience short-lived adverse excursion before traveling to Target 1 (T1) and Target 2 (T2).

The objective of this research is to:
1. Objectively derive exactly three wider stop distances using only training data from the Development period (2026-07-20 to 2026-08-31).
2. Measure whether wider stops rescue trades that previously stopped out, or merely increase downside exposure on persisting losers.
3. Evaluate the trade-offs using strict movement scoring, fixed original targets, engine risk-reward (`min_rr_ratio`) gates, and daily paired bootstrap confidence intervals across the entire recorded cohort (through 2026-10-12).

---

## 2. Cohort Definition, Ingestion, and Matching Hierarchy
- **Data Sources**:
  - `supabase_snapshot.json`: Contains production `signals` (166 rows), `active_trades` (165 rows), and `analytics` (165 rows) captured up to 2026-10-12. Note: the source DB snapshot includes current states after entry cutoff; no future states are used in selection or scoring.
  - `dhan_raw.json`: Fresh 1-minute historical candles for NIFTY Index (`security_id=13`, `IDX_I`) from 2026-07-20 00:00:00 to 2026-10-13 00:00:00, verified via read-only Dhan preflight.
  - `cohort.csv`: Full matched cohort export (165 trades) including matching methods, timestamps, price levels, and execution notes.
- **Matching Rules**:
  1. Primary: Exact `signal_uuid` match between `signals` and `active_trades` (42 trades).
  2. Secondary: Exact `signal_id` string match.
  3. Tertiary (Fallback): Unique `(direction, entry_timestamp)` match (123 trades).
- **Exclusion & Quarantine Criteria**:
  - Unentered signals: Signal `id=505` (`2026-09-24T13:22:00+05:30`) has no corresponding entry in `active_trades` and is explicitly excluded from the primary cohort.
  - Cardinality: Exactly 165 trades matched 1-to-1 without duplicates.
  - Signal ID 475 Note: Source signal `id=475` matches active legacy `signal_id=1892` (`entry_timestamp: 2026-09-17 13:28:00 IST`). The recorded trade SL timestamp was `13:28:48.771636 IST`, prior to the first full replay minute at `13:29:00 IST`. It is retained in all paired samples (evaluated from the first available minute 13:29:00); omission risk of pre-replay seconds is flagged, and stored outcome is for reference only.
  - Active trade stop losses may have moved to break-even (BE) in live execution. The original baseline stop loss is strictly taken from `ares_signals.stop_loss`.
  - Targets are strictly preserved from `ares_signals.target_1` and `ares_signals.target_2`.
  - Reference entry price is the actual recorded `active_trades.entry_price`.

---

## 3. Partitioning Schema
- **Development Partition (Train)**:
  - Dates: `2026-07-20` through `2026-08-31` (31 trading sessions, 82 recorded trades).
  - Used exclusively for stop-loss distance selection and MAE distribution analysis.
- **Historical Validation Partition**:
  - Dates: `2026-09-01` through `2026-10-12` (28 trading sessions, 83 recorded trades).
  - Note: This period has been previously examined in prior research; it is historical validation, not pristine out-of-sample data.
- **Total Validated Sessions**: 59 sessions, 22,125 trading-hour bars (09:15 to 15:29 IST), zero gaps, zero duplicates.

---

## 4. Development-Only Stop Selection Methodology
- **Adverse Excursion & Recovery Measurement**:
  - For each recorded entry in the Development partition, track post-entry 1-minute bars up to a maximum horizon of 30 minutes within the same session.
  - Ignore the original stop loss during measurement to observe natural price excursions.
  - Track until the first touch of the original Target 1 (`hi >= t1_dist`).
  - Calculate Maximum Adverse Excursion (MAE):
    - Adverse excursion: `-lo` (where `lo = side * (bar_low - entry)` for bullish, `side * (bar_high - entry)` for bearish).
    - If a bar opens at or above T1 (`op >= t1_dist`), open precedence applies and the bar's intrabar adverse excursion is not added.
    - If T1 is touched within the bar (`hi >= t1_dist`), MAE includes the adverse extreme of the T1 touch bar as a conservative upper bound.
- **Selection Criteria**:
  1. Identify qualifying training recoveries: Development trades that reached original T1 within 30 minutes whose MAE strictly exceeded their original SL distance ($n = 8$).
  2. Compute quantiles on the 8 qualifying recoveries:
     - $q_{50} = 20.125$
     - $q_{75} = 29.3125$
     - $q_{90} = 32.8450$
  3. Proposed Stop Formula:
     $$\text{Stop} = \operatorname{clip}(\lceil q \rceil + 1, 13, 30)$$
     - $q_{50} \to \lceil 20.125 \rceil + 1 = 22$
     - $q_{75} \to \lceil 29.3125 \rceil + 1 = 31 \to$ bounded to 30
     - $q_{90} \to \lceil 32.8450 \rceil + 1 = 34 \to$ bounded to 30
  4. Duplicate Resolution: Raw derived stops were $[22, 30, 30]$. The duplicate $q_{90}$ stop (capped at 30) was resolved to the nearest unused integer in $[13, 30]$, yielding 29. Sorted ascending: **[22, 29, 30]**. Note: 29 and 30 are nearly identical due to the pre-declared cap at 30 and small $n=8$ sample size.
  5. Fallback rule: If fewer than 5 qualifying recoveries occur, use all training T1 reachers under the same formula. Halt execution if fallback T1 reachers $< 5$.
  6. Freezing: Selection is frozen in `selection.json`. `selection.py` refuses to overwrite existing selection files to guarantee stability.

---

## 5. Execution Alignment and Replay Conventions
- **Entry Timing & Availability**:
  - First evaluated bar is `start = max(source + 1, ceil_minute(active_created_at))`.
  - The entry minute itself is excluded from post-entry price path evaluation.
  - Multi-session holding is tracked until resolution or end of dataset.
  - Bar start timestamp recorded as `exit_bar_start`. Conservative exit availability is recorded as `exit_known_at = exit_bar_start + 1 min`.
  - Excursions (`mae` and `mfe`) are full-exit-bar upper bounds, not exact pre-exit excursions.
- **Scoring Rules (Movement Score)**:
  - Initial Stop Loss hit: $-1.0$
  - Target 1 hit, then stopped at Break-Even (BE): $+1.0$ (`T1_BE`)
  - Target 2 hit: $+2.0$ total (`T2`)
  - Target 1 hit, pending before Target 2: $+1.0$ (`T1_PENDING`)
  - Trade pending before Target 1 or SL: `None` (`PENDING`)
- **Strict Convention**:
  - Open print precedence evaluated before intrabar highs and lows.
  - For ambiguous bars (where both stop and target thresholds are crossed in the same minute), conservative SL-first order is assumed.
  - If a bar crosses both T1 and entry price, the stop moves to BE and triggers at BE ($+1.0$).
- **Sensitivity Conventions**:
  1. *Favourable Order (`optimistic_target_first`)*: Target assumed first for ambiguous bars. Switches outcome and recomputes stop-loss exposure.
  2. *EOD Horizon (`eod_censored`)*: Trades evaluated same-day through 15:29 IST on entry date (`same_day=True`). Unfinished trades are censored at session end (reported as `PENDING` or `T1_PENDING`), not executed closes. Switches outcome and recomputes stop-loss exposure.
  3. *Next Available Bar Open Entry (`open_entry_price`)*: Entry at `bars[start]['open']` preserving original target and stop point distances. Switches outcome and recomputes stop-loss exposure.

---

## 6. Risk Accounting and Production Gates
- **Risk Gate**:
  - Production `config_profiles.py` enforces `min_rr_ratio = 1.0` ($R:R = \text{T1 distance} / \text{SL distance}$).
  - Trades with $\text{T1} / \text{SL} < 1.0$ fail the production risk gate and cannot be accepted by the live engine.
- **Exposure Diagnostic**:
  - Total SL Exposure = $\sum (\text{Initial SL Hits} \times \text{SL distance})$.
  - Note: Under fixed targets and fixed $-1.0$ scoring, widening stop losses makes trades mechanically non-worsening (a trade can convert from $-1$ to $+1$ or $+2$, but never from positive to negative). Consequently, a positive bootstrap delta does not establish profit, capital edge, or risk-adjusted superiority.

---

## 7. Statistical Evaluation
- **Paired Comparisons**:
  - Evaluated on identical 165 trades for baseline and all variants.
  - Daily block paired bootstrap: 2,000 resamples, seed 42, evaluated across all 59 available session dates (31 Development, 28 Historical Validation), including zero-delta no-trade days in overall and partition resamples.
  - Reporting: 95% Confidence Intervals and descriptive `fraction_bootstrap_delta_nonpositive`. No hypothesis-test or statistical significance claims.
  - Block-specific unresolved counts reported for each partition and slice.
