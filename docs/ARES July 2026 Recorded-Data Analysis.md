# ARES Recorded-Data Analysis — July 2026

Full-month pass over live Supabase records: `ares_signals`, `trade_analytics`, `active_trades`, `ml_collection`. Window 2026-07-01 → 2026-07-30 (22 sessions), with Jun 24–30 as the only available baseline.

Scripts: `scratch/audit-july2026/{pull,analyze,deep}.py`. Raw pull: `july.json`, `all.json`.

---

## 0. Read this first — the data needed cleaning before it meant anything

**The `2026-07-17-task188-fixture-cleanup-and-ist.sql` migration was authored but never executed against production.** Its own preview counts match prod exactly today:

| Migration expected | Found in prod now |
|---|---|
| 9 fixture trades | **9** |
| 4 fixture signals (ids 167–170) | **4** |
| 30 trades with naive-IST `entry_timestamp` | **30** |
| 31 signals with naive-IST `timestamp` | **31** |

The 9 fixtures are `OI_WALL_REJECTION`, `entry_price = 24001.0`, `reasons[0] = "Reason 1"` — including the fabricated `+99.0 T2_HIT` the migration explicitly calls out. Six were inserted in a single second on 2026-07-06 at 10:10 IST.

Every number below applies that migration **in memory**. Uncleaned, July reads 99 trades; the real count is **92**.

> Action: run the migration. It is idempotent and guarded — if the signature has drifted it matches zero rows.

---

## 1. Volume — the signal engine did not slow down

| | trades | sessions | per day |
|---|---|---|---|
| Jun 24–30 | 17 | 4 | **4.2** |
| July | 92 | 22 | **4.2** |

Signals per ISO week: W27 11 · W28 14 · W29 19 · W30 27 · W31 21 — *rising* through the month. Zero weekdays produced no signal.

**So the perceived volume drop is not fewer signals.** It is this:

> **69 of 92 July trades (75%) were sized to `suggested_lots = 0`.**

`options_math.fetch_dhan_capital()` reads the live Dhan available balance. Recorded `capital` for July trades: 13 trades at **₹3**, 24 at **₹53**, and a long tail — median ₹155. A NIFTY lot at the median premium of ₹106 with `nifty_lot_size = 65` costs ~₹6,890, so `affordable_lots = floor(capital / (premium × 65)) = 0`.

Lots distribution: `{0: 69, 1: 9, 2: 4, 3: 4, 4: 5, 9: 1}`.

The alerts fired. They were unfundable. That is the volume you noticed.

Thin days were also real but distinct: 2026-07-15 has only **201** `ml_collection` bars vs ~375 for a full session (the known morning-autostart gap), and 07-03 / 07-07 / 07-08 / 07-28 produced 1 trade each.

*Secondary:* `nifty_lot_size = 65` is worth re-verifying against the current NSE contract spec — if it is stale, every lot calculation this month is off.

---

## 2. Outcomes — sitting exactly on the breakeven line

92 closed trades, **+0.40 points net**. Not a typo.

| Metric | Value |
|---|---|
| Win / flat / loss | 12 / 30 / 50 |
| Win rate (excl. flats) | **19.4%** |
| Avg win / avg loss | +68.1 / −16.3 → payoff **4.17 : 1** |
| Breakeven win rate at that payoff | **19.3%** |
| Median trade | **−1.80** |
| Max drawdown | **−268.2 pts** |
| Longest losing streak | **21 trades** |
| Green days | 7 / 21 |

The system is a lottery-ticket profile: it survives on a handful of large wins and is one bad tail away from negative. A 21-trade losing streak is not survivable psychologically at 4 trades/day — that is a full week of red.

### Exit mix — only one exit type makes money

| Exit | n | share | what it means |
|---|---|---|---|
| `SL_HIT` | 50 | 54% | full stop |
| `TIME_STOP` | 19 | 21% | 45-min trail to cost, then stopped at cost → **0.00** |
| `T1_HIT` | 11 | 12% | reached T1, trailed to cost, stopped at cost → **0.00** |
| `T2_HIT` | 12 | 13% | the only winners |

**All 11 `T1_HIT` rows booked exactly 0.00 points. Every single one.** This is not a bug — `position_manager.py:253` only writes to `trade_analytics` when the trade fully closes, so a T1 touch that later trails out at breakeven is correctly recorded as zero. But it means `T1_HIT` in this database has never once meant "a T1 win", and any report that counts it as one is wrong.

**33% of all July trades closed at dead breakeven.**

### By setup type

| setup | SL/T1/T2 | n | SL | T1 | T2 | TIME | median hold | total |
|---|---|---|---|---|---|---|---|---|
| EXHAUSTION_REVERSAL | 12/24/40 | 42 | 28 | 4 | 5 | 5 | 12 min | **+36.3** |
| TREND_CONTINUATION | 25/40/80 | 33 | 14 | 3 | 4 | 12 | 46 min | **+8.4** |
| FAILED_BREAKOUT | 15/30/55 | 10 | 4 | 3 | 1 | 2 | 57 min | **−5.0** |
| OI_WALL_REJECTION | 12/25/40 | 7 | 4 | 1 | 2 | 0 | 14 min | **−39.3** |

Exhaustion's 12-minute median hold against a 12-point stop means it is stopping out almost immediately — 28 of 42 (67%).

### Confidence is inverted

| confidence | n | total | avg |
|---|---|---|---|
| MEDIUM | 70 | **+142.2** | +2.03 |
| HIGH | 22 | **−141.8** | −6.45 |

Not an outlier artefact — dropping the best *and* worst HIGH trade makes it **worse** (−181.4). 16 of the 22 HIGH trades are `TREND_CONTINUATION`, and 13 of those stopped out. The confidence score is currently anti-predictive.

### Direction is one-sided

| direction | n | total |
|---|---|---|
| BULLISH | 52 | **+219.0** |
| BEARISH | 40 | **−218.7** |

NIFTY ran 23,926 → 24,294 (**+369 pts, +1.54%**) with 13 of 22 sessions closing up. The short side paid for the long side almost exactly. The mean-reversion detectors are fading a trend.

### Time and day concentration

| hour IST | n | total | | weekday | n | total |
|---|---|---|---|---|---|---|
| 15:00 | 18 | **+182.6** | | Fri | 16 | **+187.8** |
| 12:00 | 10 | **+141.8** | | Wed | 19 | +55.5 |
| 09:00 | 10 | +56.0 | | Mon | 21 | +50.2 |
| 11:00 | 9 | +3.2 | | Tue | 9 | **−130.0** (0% win) |
| 14:00 | 16 | −85.8 | | Thu | 27 | **−163.0** |
| 10:00 | 15 | −136.8 | | | | |
| 13:00 | 14 | **−160.6** | | | | |

Thursday is expiry day and the single worst bucket by both count and loss. The 10:00–14:00 block gives back −383 pts; the 15:00 hour alone returns +182.6.

---

## 3. Did the config changes help?

| phase (merge date) | n | win% | total | avg |
|---|---|---|---|---|
| P0 baseline, no gates (Jun 24) | 23 | 35% | **+740.9** | +32.2 |
| T169/171 OI-wall confirm, R:R gate, time-stop (Jul 02) | 4 | 0% | −111.7 | −27.9 |
| T172/173 breakout 3-of-5, IV-crush v2, trend filter ON (Jul 03) | 1 | 0% | 0.0 | 0.0 |
| T177/180/181 continuation live, trend filter OFF (Jul 06) | 0 | — | — | — |
| T182 observation + speed + IV-crush gates REMOVED (Jul 07) | 2 | 0% | −25.4 | −12.7 |
| T184/185 MEDIUM breakout back, per-type SL/T1/T2 (Jul 09) | 28 | 7% | −37.0 | −1.3 |
| T188 OI-wall wick gate fixed (Jul 17) | 51 | 18% | **+175.6** | +3.4 |

Two honest readings, and they point opposite ways:

**The baseline looks unbeatable — but most of it was the uncapped right tail.** Re-running those same pre-TASK-185 trades under the per-type geometry that shipped Jul 9:

```
pre-T185  actual   : +603.9 pts over 30 trades
pre-T185  re-capped: +148.2 pts   <- same trades, T185 geometry
post-T185 actual   : +138.6 pts over 79 trades
```

Two trades carried it: `+309.7` (FAILED_BREAKOUT, 06-29) and `+284.6` (EXHAUSTION, 06-30). Under a 55/40-point T2 they become `+55.0` and `+40.0`. **TASK-185 amputated the right tail — and the right tail was the entire edge.** On a per-trade basis, post-T185 (+1.75/trade over 79) is actually *better* than re-capped pre-T185 (+4.94/trade over 30 — still ahead, but no longer a different universe).

**The Jul 17 phase is the only positive modern one**, and it is the largest sample (51 trades). Whatever combination is live since TASK-188 is the best the system has performed under capped geometry.

*Caveat on all phase rows:* the middle four phases have n = 4, 1, 0, 2. They are unreadable. The comparison that carries weight is baseline vs T184/185 vs T188.

### The R:R gate cannot be firing

| setup | R:R at T1 | R:R at T2 |
|---|---|---|
| EXHAUSTION_REVERSAL | 2.00 | 3.33 |
| TREND_CONTINUATION | 1.60 | 3.20 |
| OI_WALL_REJECTION | 2.08 | 3.33 |
| FAILED_BREAKOUT | 2.00 | 3.67 |

`min_rr_ratio = 1.0`. Every configured pair clears it by 60–267%. Since TASK-185 made `per_type_levels` the single source of SL/T1/T2, the gate can only ever trigger on a dynamically-placed level. Suppressions are not persisted anywhere, so the exact count is unrecoverable — but structurally this filter is now a no-op.

---

## 4. Your trading model, applied to the recorded tape

Book 40% at T1, stop to cost, let the runner go:

```
raw recorded (all-out at exit) :    +0.4 pts   (+0.00/trade)
managed  (40/60 + SL-to-cost)  :   -50.3 pts   (-0.55/trade)
```

The model *underperforms* here, and the reason is specific: 11 trades reached T1 and would bank only `0.4 × T1` before the runner returned to cost, versus the recorded 0.00 — but the 12 `T2_HIT` trades give up 40% of their run to the early booking. July converted **12 of 23** T1-reachers to T2 (52%). Partial booking pays when that conversion is low and you are protecting gains; at a 52% conversion the runner is worth more than the insurance.

**Caveat that limits this whole section:** `trade_analytics` never records maximum favourable excursion. For the 50 `SL_HIT` trades we cannot know whether price went +20 in your favour before reversing. Any partial-booking or SL-tuning study built on this table is inferring T1 from config, not measuring it. This is the same ceiling TASK-185 hit.

---

## 5. Open defects this pass surfaced

1. **Migration never ran** — 9 fixtures and 30/31 corrupted timestamps still in prod (§0).

2. **2026-07-06: 8 signals fired, 0 trades recorded.** `ares_signals` ids 189–196 (09:23 → 13:50, one FAILED_BREAKOUT + seven EXHAUSTION_REVERSAL) have no corresponding `trade_analytics` row. The six fixture inserts landed at 10:10 IST the same morning — the same session in which the engine stopped persisting real trades. 07-03 has a 1-signal gap too. Total: **9 signals lost between the engine and the position manager.**

3. **20% of trades are held overnight.** 18 of 92 exited on a later calendar day — 13 held 1 day, 3 held 2, 2 held 3, the longest 2,517 minutes. `_apply_time_stop` only *tightens* the stop to entry; nothing closes the position at session end. Worse, `pnl_points` is measured in **NIFTY spot points** while the instrument is a weekly option — a 3-day hold that ends +83 spot points can easily be a premium loss after theta. Recorded P&L overstates reality on every multi-day trade. There is no end-of-day square-off.

4. **`T1_HIT` is a misleading label** — it has never once carried non-zero P&L (§2). Any digest or breakdown that reads it as a win is wrong, including the weekly/monthly Discord report shipped Jul 24.

5. **`ml_collection` is not yet a usable training set.**
   - 8,002 rows / 22 days.
   - `detector_scores` non-zero: **0 of 6,887** rows before Jul 28, **13 of 1,115** after the TASK-192 enum fix. **86% of the month is structurally zero.**
   - **0 rows carry a `trade_outcome` label.** There is no supervised target in the table at all.
   - 10 of 92 July trades have `signal_id = NULL`, so they cannot be joined back to a signal.

---

## 6. What the data actually argues for

Ranked by expected effect, not by effort:

1. **Fund the account or cut `nifty_lot_size` exposure.** 75% of signals were unfundable. Nothing else on this list matters until a signal can be taken.
2. **Run the TASK-188 migration.** Every future analysis inherits the same 7-row error otherwise.
3. **Fix the signal→trade persistence gap** (defect 2). Losing a full session of signals silently is worse than any tuning question.
4. **Add an end-of-day square-off** and price P&L in premium, not spot points (defect 3). Right now the recorded edge is partly an accounting artefact.
5. **Stop trading Thursday and the 10:00–14:00 block, or find out why they bleed.** −163 and −383 points respectively, on decent samples.
6. **Treat HIGH confidence as a bug, not a feature.** It is anti-predictive at n=22, driven by TREND_CONTINUATION.
7. **Revisit the T2 cap.** TASK-185's geometry removed the only two trades that ever paid for a month. A trailing/momentum target beyond T2 is worth testing against the re-capped counterfactual in §3.

---

## Appendix — reproduction

```bash
python3 scratch/audit-july2026/pull.py      # -> july.json, all.json
python3 scratch/audit-july2026/analyze.py   # volume, outcomes, phases, integrity, ML
python3 scratch/audit-july2026/deep.py      # signal gap, breakeven exits, T185 counterfactual
```

Both analysis scripts apply the TASK-188 migration in memory and are safe to re-run after the real migration lands (fixture ids simply match nothing).
