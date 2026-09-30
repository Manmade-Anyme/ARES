---
adr_id: "MANM-158"
title: "OI wall retest diagnosis and validation plan"
status: "approved; implemented pending out-of-sample validation"
date: "2026-09-30"
issue: "MANM-158"
---

# MANM-158: OI wall retest diagnosis

## Decision status

Approved in the MANM-158 discussion on 2026-09-30 and implemented on this branch. The current investigation addresses OI wall only; failed breakout remains a later workstream. The change has not been deployed, and out-of-sample expectancy validation remains pending.

## Evidence and method

Read-only queries against the **Trading signal data** Supabase project (`mgenubvjbatpcpntlgav`) on 2026-09-30 joined `trade_analytics.signal_id` to `ares_signals.id::text`. The ticket baseline is `entry_timestamp < 2026-09-12`; all timestamps below are Asia/Kolkata. `pnl_points > 0` defines a profitable trade. The ticket's stated “win rate” instead counts some breakeven exits as wins; it must not be used as the positive-PnL rate. `trade_analytics` has 233 rows in this baseline. Only 19 of 20 OI wall trades link to an `ares_signals` row; geometry and confidence analyses exclude the unmatched trade. Reference levels were parsed from the first recorded signal reason because these historical signals have no populated `oi_wall_context` or typed reference-level field.

| Setup | Trades | Net points | Points/trade | Positive / zero / negative PnL |
| --- | ---: | ---: | ---: | ---: |
| Trend continuation | 90 | +372.70 | +4.14 | 16 / 42 / 32 |
| Exhaustion reversal | 106 | +92.90 | +0.88 | 18 / 22 / 66 |
| Failed breakout | 17 | -51.55 | -3.03 | 2 / 4 / 11 |
| OI wall rejection | 20 | -143.20 | -7.16 | 2 / 4 / 14 |

The two weak setups contributed -194.75 points. Excluding them from this historical ledger yields +465.60 instead of +270.85 points. That subtraction is an attribution, **not** a prospective performance estimate: removing signals can alter cooldown, priority, and subsequent trades. As of 2026-09-30, the complete ledger has 19 failed-breakout trades (-43.55) and 21 OI-wall trades (-159.20), so the weakness has not disappeared in the small later sample.

## Diagnosis

1. **Observed loss distribution.** OI wall had 14 stop losses totaling -216.00 points, four zero-PnL exits, and two positive exits totaling +72.80. Failed breakout had 11 stop losses totaling -138.00, four zero-PnL exits, and two positive exits totaling +86.45. The positive-PnL rates are 10.0% and 11.8%, respectively. OI wall's mean positive trade is +36.40 and mean negative trade is -15.43 (win/loss size ratio 2.36); the previous claim of a 0.84 ratio is unsupported. Breakeven exits and fat-tailed winners make a two-outcome breakeven-win-rate formula misleading here.
2. **Observed stop geometry.** `engine.apply_per_type_levels()` uses a fixed entry-relative stop (currently 16 points for OI wall and 12 for failed breakout). In the linked baseline, 12/19 OI wall stops and 4/17 failed-breakout stops were on the entry side of, or exactly at, their parsed wall/breakout level. Eight and three of those trades, respectively, stopped out. Median entry-to-level distance was 19.45 and 10.80 points. This establishes a structural mismatch, but does **not** establish that moving the stop would have rescued those trades; that requires candle-path replay with the actual exit rules.
3. **Breakout score hypothesis.** The current four-factor detector accepts score 2. Fifteen of 17 linked failed-breakout signals were `MEDIUM` and lost -36.55 points together. The two `HIGH` signals lost -15.00 points together. Raising the score to 3 would remove most historical signals, but the remaining sample has no demonstrated edge. TASK-184 deliberately retained score 2 for manual-trader information; do not silently remove that tier or claim score 3 is validated.
4. **Regime hypothesis.** A retrospective 15-point VWAP-distance plus five-minute slope rule identified four of 14 failed-breakout trades with matched minute candles; those four lost -39.00 points. It identified only one of 13 matched OI-wall trades (-16.00). Three failed-breakout and six linked OI-wall trades lacked both matched readings. This is exploratory, not an out-of-sample filter result. The rule appears unlikely to solve OI wall alone.
5. **Wall freshness remains unproven.** `OIWallDetector` requires OI >4M and an OI-change percentage >5 at selection. The historical signal reasons show positive changes (mean 11.87% across 19 linked trades), but there is no pre-September-7 `ml_collection.oi_wall_context` series and the stored OI percentage does not establish a sustained defense or exclude strike migration. Calling the losing walls “stale” or “zombie” is a hypothesis, not a finding.

PR #107 is the merge of `feature/MANM-110-fix-oi-wall-tracked-wall-priority` (`c3142af`), not MANM-148. It retains a reachable interacted wall during retest. PR #119 (`9d8ad8f`, merged September 22) removed the later directional confirmation candle to allow immediate qualification at the defended retest close. The historical loss period largely predates those changes, so it cannot establish either PR's expectancy effect.

### First trade under the immediate-retest rule

The one OI wall signal emitted after PR #119 is Supabase signal `518`, joined to `trade_analytics.id = ff6d24bc-a2e6-4704-a3dc-3c0fce708a98`, on September 28. It was a bullish PE 22950 wall, recorded at 09:19 IST at 22954.10, with SL 22938.10 and T1 22979.10; the recorded exit was `SL_HIT` at 09:21 for -16.00 points. Suggested option sizing was zero lots, so the record establishes a signal and analytics trade, not a funded execution.

The wall legitimately met the implementation's numeric test: PE OI rose from 4.39M at 09:16 to 6.30M at 09:19, the last snapshot reported +9.94% OI change and persistence 4, and the 09:17 candle made the initial bullish interaction. The 09:18 candle armed the retest after a 27.45-point favourable excursion. These observations show that the size/change/persistence checks operated; OI growth by itself does not prove that support was being defended by writers.

The 09:19 *retest* candle opened 22966.85, reached 22967.85, fell to 22953.05, and closed 22954.10. It closed 3.05 points above its low and 4.10 above the wall, but its body fell 12.75 points. Its low was still 3.05 points above the strike; the configured 20-point retest band counted this near approach as a test. The next minute fell through the wall. The price evidence therefore shows no bullish rejection on the entry candle, despite a defended-side close.

A read-only filter replay of the stored 09:16–09:20 candles and wall bias state matched production telemetry. PR #119's filter returned `QUALIFIED` at 09:19; PR #103/#107's prior filter waited for later directional confirmation and did not emit before the wall broke. This is a filter-level counterfactual, not a full-engine PnL replay. The current test `test_pe_wall_full_lifecycle_to_qualified` also expects a red PE retest candle to qualify. The observed trade is thus consistent with PR #119's implementation and tests; the acceptance rule is too weak to identify a rejection.

## OI wall decision

- **Keep the two-phase wall flow and immediate entry on a valid retest.** In `OIWallEntryFilter.update()`, require the retest candle to move in the intended trade direction as well as enter the existing distance band and close on the defended side: `close > open` for a bullish PE wall, `close < open` for a bearish CE wall. This applies the same directional evidence already required of the first wall interaction. A red PE or green CE candle remains `RETEST_READY`; it is not an accepted rejection. A later candle may qualify while the same wall remains valid. This is a structural definition, with no new fitted numeric threshold and no added confirmation candle.
- **Keep existing stop and target geometry for this isolated change.** The September 28 entry failed because price crossed the wall and stop on the next minute; simply widening its 16-point stop would not establish a valid rejection. A hypothetical 6-point wall buffer would raise mean risk to 27.51 points in the historical linked sample and fail the existing 1:1 gate for 10/19 signals. Stop changes need separate full-path replay.
- **Observe OI and regime rather than assert causality.** The wall met the numeric OI threshold yet price fell through it. This supports a price-confirmation fix; it does not prove stale OI, writer unwinding, or a regime threshold. Those remain separate hypotheses.
- **Defer failed breakout.** The historical audit above is retained for traceability, but no breakout behavior change is proposed in this OI-wall decision.

## Validation requirement and data gap

The earlier proposed walk-forward windows (June 24–July 20 calibration, July 20–August 15 validation, August 15–September 11 holdout) cannot be presented as a completed out-of-sample replay:

- The current Supabase `ml_collection` begins July 20. `scratch/audit-july2026/july.json` preserves July 1–30 minute candles, but there are still no June 24–30 candles in these sources.
- `options_snapshots` for July 20–24 cluster around 09:36–09:38 each day; they do not provide the tick-by-tick option chain needed to rerun `AresEngine` for a full session. `ml_collection.raw_atm_oi` contains ATM data, not the full chain or level snapshots.
- The August 15–September 11 interval contains only 8 failed-breakout trades (-40.00) and 11 OI-wall trades (-144.00). These dates and thresholds were chosen after inspecting the full 233-trade sample; it is a temporal slice, not a blind holdout.
- After the September 11 baseline, only two failed-breakout trades (+8.00) and one OI-wall trade (-16.00) exist. They provide no useful adjusted-policy validation.
- Fixed historical trades cannot replay changed signal selection, priority, cooldown, entry, and exit paths. The current engine also uses `datetime.now()` for cooldown, so deterministic replay needs an injected replay clock or equivalent isolated control.

An additional read-only first-barrier check used the July scratch export. Its `ml_collection.timestamp` values are shifted +05:30 relative to the current database for the same row IDs (for example, row 6510 is 09:15 UTC in the export and 03:45 UTC in current Supabase). After correcting that export offset and starting with the next minute candle, eight linked July 20–30 trades (five OI wall, three failed breakout) had the same first stop-versus-T1 direction as their recorded exit categories. Applying the proposed structural buffers to those fixed entries rescued **zero** first-stop trades; one OI-wall first-target case became a first-stop case, and three of five wall entries would fail the current 1:1 gate. This check does not model T2, breakeven trailing, reselected trades, or the full engine. The old `scratch/audit-july2026/sl_tuning.py` prints misleading zero adverse excursions because it does not correct the export offset; its suggested stop values were not used.

Before declaring that the new rule improves expectancy, capture or obtain complete timestamped minute candles, full option chains, structural levels, and the code/config version for each session. Freeze the directional rule and metric definitions **before** an untouched future period. Replay baseline and candidate through the same engine and exit rules, with chronological windows, warm-up handling, identical fill assumptions, and per-setup metrics including trade count, net/mean PnL, positive/zero/negative outcomes, profit factor, drawdown, and trade-level paired differences. Label July–September 2026 analysis as exploratory. If complete historical inputs cannot be recovered, record both baseline and candidate decisions prospectively and evaluate the future sample without changing either rule. The September 28 case validates rejection of a false positive; one trade cannot establish positive expectancy.

### Reproduction queries

Run read-only on project `mgenubvjbatpcpntlgav`. The September 11 cutoff is exclusive of September 12 UTC; the baseline dates here are unaffected by the timezone boundary.

```sql
SELECT setup_type, COUNT(*) AS n,
       ROUND(SUM(pnl_points), 2) AS net_points,
       ROUND(AVG(pnl_points), 2) AS points_per_trade,
       COUNT(*) FILTER (WHERE pnl_points > 0) AS positive,
       COUNT(*) FILTER (WHERE pnl_points = 0) AS zero,
       COUNT(*) FILTER (WHERE pnl_points < 0) AS negative
FROM public.trade_analytics
WHERE entry_timestamp < '2026-09-12'
GROUP BY setup_type ORDER BY setup_type;
```

```sql
WITH linked AS (
  SELECT t.setup_type, t.result_state, s.direction,
         s.trigger_price, s.stop_loss,
         CASE WHEN t.setup_type = 'OI_WALL_REJECTION'
              THEN substring(s.reasons->>0 FROM 'wall at ([0-9.]+)')::numeric
              ELSE substring(s.reasons->>0 FROM 'level ([0-9.]+)')::numeric
         END AS reference_level
  FROM public.trade_analytics t
  JOIN public.ares_signals s ON t.signal_id = s.id::text
  WHERE t.entry_timestamp < '2026-09-12'
    AND t.setup_type IN ('OI_WALL_REJECTION', 'FAILED_BREAKOUT')
)
SELECT setup_type, COUNT(*) AS linked_trades,
       COUNT(*) FILTER (WHERE
         (direction = 'BEARISH' AND stop_loss <= reference_level) OR
         (direction = 'BULLISH' AND stop_loss >= reference_level)
       ) AS stop_on_entry_side_or_level
FROM linked GROUP BY setup_type ORDER BY setup_type;
```

## Implementation and verification

The implementation adds one symmetric directional check in the secondary-retest branch of `detectors/oi_wall_entry.py`. The existing lifecycle fixtures that expected contrary candles to qualify now use directional retest candles. New regression tests cover the September 28 sequence, a valid later green PE retest, and the CE mirror with continued `RETEST_READY` state after a non-rejection candle. Wall identity, persistence, excursion, priority, acknowledgement, SL/T1/T2, and telemetry behavior were not changed. After merging the latest mainline changes, the full test suite passed with `python -m pytest tests/ -q --tb=short --import-mode=importlib` (830 passed). This verifies behavior and regression compatibility; it is not an out-of-sample performance result.
