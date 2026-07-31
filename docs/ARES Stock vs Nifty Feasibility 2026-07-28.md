# ARES Stock vs Nifty Feasibility — 2026-07-28

User asked whether an F&O **stock** could be used instead of NIFTY, motivated by a felt
problem that "there is a lot of trapping" on Nifty. Not a migration — a diversification
exploration. Nifty stays.

**Verdict: the trapping claim is real and measurable. The stock question is not currently
answerable, because every tuned threshold in config_profiles.py is an
absolute number bound to one price, one lot size and one IV regime.**

Two findings below are about **live Nifty**, not stocks, and matter more than the original
question.

---

## 1. The trapping claim — confirmed

50 liquid F&O names, 60 days of 5m bars. Every break of the running intraday high/low
after the first 30 min; "trap" = price trades back through the level within 30 min.
`edge` = median follow-through minus median adverse excursion, in ATR.

| sym | trap% | ft_atr | mae_atr | edge |
|---|---|---|---|---|
| DIVISLAB | 81.2 | 1.36 | 0.89 | +0.47 |
| ITC | 81.7 | 1.32 | 0.85 | +0.47 |
| TCS | 81.8 | 1.17 | 0.78 | +0.39 |
| DLF | 83.5 | 1.28 | 0.78 | **+0.50** |
| VEDL | 83.9 | 1.36 | 0.88 | +0.48 |
| … | | | | |
| RELIANCE | 87.5 | 1.01 | 0.97 | +0.05 |
| HDFCBANK | 86.9 | 1.10 | 0.97 | +0.13 |
| **^NSEI** | **91.3** | **0.87** | **1.15** | **−0.27** |

Nifty ranks **49th of 50** on trap rate and has the **worst edge in the entire set**. It
also generated the most breaks (607 vs ~400 typical) — it keeps making new intraday
extremes and almost none hold.

The absolute numbers are high everywhere (81–91%) because the test counts a single tick
back through the level. The **ranking** and the **edge column** are the signal.

**Structural cause.** An index is an average of 50 order books. Nothing defends a level in
an index, because no participant holds a position at "24,050 Nifty" — a break means 50
stocks briefly agreed, and carries no information. In a stock, a break through a level
means resting orders got run over. Compounding it, Nifty is the most-watched instrument in
the world by option volume, so every retail breakout trader sees the same PDH/PDL.

Note this cuts both ways: `FAILED_BREAKOUT` in Detectors is arguably *well*-matched to
Nifty, since trapping is what it trades. It's the continuation and OI-wall setups that
Nifty punishes.

### Trend efficiency — no stock is meaningfully cleaner

Separate screen, Kaufman efficiency ratio (|net move| ÷ |path travelled|) per day on 15m
bars: full range across 50 names is **0.15–0.26, with Nifty at 0.23, mid-pack**. Top names
(DLF 0.26, WIPRO 0.25, TECHM 0.25) are inside noise of each other and won't persist.

**Trendiness is a regime property, not a stock property.** "Pick a trendier stock" is not
available as a fix. What stocks actually offer is 2–3× the daily range (Nifty 0.93%,
stocks 1.7–3.1%).

Names ranking top-10 on *both* screens: **DLF, VEDL, DIVISLAB, TCS, ITC**. One 60-day
regime — a candidate list, not a stock pick.

---

## 2. Backtest — and what it actually measured

20 trading days, 1-min bars, driving the **real** detector classes
(`FailedBreakoutDetector`, `TrendContinuationDetector`, `ExhaustionDetector`) under live
`NON_EXPIRY_CONFIG`, with `apply_per_type_levels` + the R:R gate + the 15-min cooldown.
Exits modelled per the user's trading model: 40% booked at T1, SL→cost, runner to T2, 45-min
time stop.

| sym | px | SL% | T1% | n | SL% | T2% | T1BE% | TIME% | net %px |
|---|---|---|---|---|---|---|---|---|---|
| NIFTY | 24004 | 0.05 | 0.10 | 174 | **57** | 7 | 22 | 14 | −1.86 |
| DIVISLAB | 7414 | 0.16 | 0.32 | 165 | 41 | 8 | 18 | 33 | −1.14 |
| TRENT | 2941 | 0.41 | 0.82 | 164 | 17 | 0 | 6 | 77 | −2.97 |
| TCS | 2296 | 0.52 | 1.05 | 163 | 10 | 0 | 1 | 88 | −0.56 |
| RELIANCE | 1280 | 0.94 | 1.88 | 157 | 2 | 0 | 0 | 98 | +1.45 |
| DLF | 651 | 1.84 | 3.69 | 171 | 0 | 0 | 0 | **100** | +6.78 |
| ITC | 286 | 4.20 | 8.40 | 183 | 0 | 0 | 0 | **100** | −1.14 |
| VEDL | 265 | 4.53 | 9.07 | 153 | 0 | 0 | 0 | **100** | +4.14 |

> **⚠️ This table does not compare stocks to Nifty**
> It compares **one fixed 12-point stop against eight price levels**. The `SL%` column
> spans 0.05% (Nifty) to 4.53% (VEDL) — a 90× spread in actual risk.
>
> - **Nifty, DIVISLAB** — SL sits *below the noise floor*; 57% and 41% stop out before T1.
> - **DLF, ITC, VEDL** — SL so wide it is never touched; **100% time-stops**. Those
>   "profits" (DLF +6.78%) are 45 minutes of directional drift with no risk management
>   operating at all. Beta, not strategy.

**The Nifty row is the finding worth keeping: 57% of ARES's own signals stop out before T1
on the live config.** That is the trapping complaint quantified from inside the detectors,
and it is independent of any stock question. Compare TASK-185_SL-Target-Optimization,
which set 12.0 from n=13 exhaustion signals.

### Risk-equalized re-run

SL = m × median 1-min candle range per symbol (equal risk in noise units), T1/T2 preserving
the 12/24/40 ratio. Cells = total R over 20 days.

| sym | m=1 | m=2 | m=3 | m=5 | m=8 | n(m=3) | win% |
|---|---|---|---|---|---|---|---|
| NIFTY | 30.3 | −13.7 | −11.7 | −10.6 | −0.8 | 174 | 44 |
| TRENT | 24.5 | 11.6 | −0.1 | −11.7 | 7.4 | 164 | 45 |
| DLF | −8.3 | 19.1 | −0.4 | −8.7 | 5.1 | 171 | 43 |
| VEDL | 19.1 | 0.3 | 3.0 | −4.0 | 2.4 | 153 | 39 |
| DIVISLAB | 48.7 | 12.9 | 15.7 | −11.5 | 5.1 | 165 | 42 |
| TCS | 10.3 | −1.2 | −3.0 | 7.8 | −3.6 | 163 | 47 |
| ITC | −3.8 | 13.5 | −16.4 | −13.4 | −5.4 | 183 | 42 |
| RELIANCE | 23.1 | −27.7 | −24.4 | −7.2 | 0.1 | 157 | 41 |

Signs flip essentially at random across the risk parameter for every symbol (RELIANCE
+23.1 → −27.7 between m=1 and m=2). That instability is the signature of noise, not edge.
Win rates cluster **39–47% — under 50% on every instrument including Nifty**.

The m=1 column looks positive almost everywhere and should be distrusted specifically: a
stop of one candle-range is exactly where frictionless simulation flatters most.

**No stock shows an edge the current system can harvest, and neither does Nifty — on the
price-only half.**

### Rupee P&L — NIFTY, TRENT, DIVISLAB, ITC

Option P&L ≈ spot points × 0.50 delta × lot. No theta, no spread, no gamma — a **floor**
approximation; real results are worse.

| sym | n | win% | pts | avg | best | worst | lot | ₹ total | ₹/trade |
|---|---|---|---|---|---|---|---|---|---|
| NIFTY | 174 | 38 | −446.3 | −2.56 | 84.7 | −25.0 | 65 | **−14,503** | −83 |
| TRENT | 164 | 51 | −87.3 | −0.53 | 32.9 | −25.0 | 225 | −9,821 | −60 |
| DIVISLAB | 165 | 44 | −84.4 | −0.51 | 46.8 | −25.0 | 100 | −4,220 | −26 |
| ITC | 183 | 46 | −3.3 | −0.02 | 3.2 | −2.8 | 1725 | −2,803 | −15 |

**All four lose, and Nifty loses the most — on the config that is actually live.**

Exit distribution:

| sym | SL | TIME | T1+BE | T2 |
|---|---|---|---|---|
| NIFTY | **100** | 47 | 15 | 12 |
| DIVISLAB | 68 | 80 | 4 | 13 |
| TRENT | 28 | 135 | 1 | 0 |
| ITC | 0 | **183** | 0 | 0 |

ITC's −₹2,803 is not a strategy result: at ₹286 spot a 12-point stop is 4.2% away, so
**every one of its 183 trades exited on the 45-minute clock**. That number is drift.

By setup, exhaustion dominates volume everywhere (125/98/110/133 of ~170), continuation is
second, `FAILED_BREAKOUT` fires 0–6 (the OI/IV-blind artifact below).

### What the backtest was blind to

Structural, and it is a lot:

1. **`OI_WALL_REJECTION` never ran.** No historical chain data exists.
2. **`FAILED_BREAKOUT` was effectively disabled** — fired 0–2 times per symbol. Without IV
   and OI, `iv_falling` and `writers_active` are forced False, so `score` caps at 2 and
   requires *both* `weak_volume` and `deep_close` (`detectors/breakout.py:113`). An
   artifact of the data, not a result.
3. **No IV** — exhaustion's `iv_spiked` and the breakout IV condition are dead.
4. **Spot points, not option premium.** No theta, no IV crush, no spread.
5. **Nifty volume is a NIFTYBEES proxy** — Yahoo serves `^NSEI` with zero volume.
6. 20 days, one regime, no slippage.

So this indicts the *price-only* detectors, not ARES as designed. The OI/IV layer is where
the design puts the edge and this test cannot see it.

### One signal-count observation

The harness fired **~165 signals per symbol in 20 days (~8/day)** with the cooldown
applied, on the same detectors that produce a handful live. The difference is that live,
the OI and IV conditions gate most of these away — meaning **the OI/IV layer is doing
nearly all of the signal selection**, and the price patterns underneath are close to
random. Checkable against existing `ml_collection` rows without any new instrument.

---

## 3. Live Dhan findings — these are about Nifty, today

Pulled via `load_dhan_credentials_from_supabase()` (`storage.py:246`) and
`dhan.option_chain()`. See Fetchers and Ingestion.

### `oi_wall_min_oi = 4_000_000` is calibrated to expiry-day OI

| sym | expiry | dte | ATM CE OI | max CE OI | vs 4M |
|---|---|---|---|---|---|
| NIFTY | 2026-07-28 | 0 | 13,800,020 | — | PASS |
| **NIFTY** | **2026-08-04** | **7** | **3,164,915** | **3,505,840** | **FAIL** |
| RELIANCE | 2026-08-25 | 28 | 2,268,500 | 5,639,500 | PASS |
| ITC | 2026-08-25 | 28 | 3,500,025 | 8,238,600 | PASS |
| VEDL | 2026-08-25 | 28 | 1,415,650 | 2,942,850 | FAIL |
| TCS | 2026-08-25 | 28 | 741,600 | 788,850 | FAIL |
| DLF | 2026-08-25 | 28 | 500,650 | 813,200 | FAIL |
| TRENT | 2026-08-25 | 28 | 134,100 | 413,550 | FAIL |
| DIVISLAB | 2026-08-25 | 28 | 55,800 | 79,500 | FAIL |

**On a fresh weekly at 7 DTE, Nifty's own deepest CE strike is under the threshold.**
RELIANCE and ITC monthlies clear a bar that Nifty misses. Relevant to
TASK-188 OI Wall Wick Gate Postmortem 2026-07-17 and to any future investigation of
why walls fire when they do.

### The threshold is a raw share count compared across different lot sizes

| sym | max CE OI | lot | contracts |
|---|---|---|---|
| NIFTY | 3,505,840 | 65 | **53,936** |
| RELIANCE | 5,639,500 | 500 | 11,279 |
| ITC | 8,238,600 | 1725 | 4,776 |
| DIVISLAB | 79,500 | 100 | 795 |

ITC "passes" only because its lot is **26× Nifty's**. In participant terms Nifty's wall is
**11× deeper than ITC's**. The comparison silently rewards high-lot-size stocks and
punishes low-lot-size ones — a units mismatch in the config, same family as the 12-point SL
problem, one layer down.

### Other scaling mismatches exposed

- **Expiry structure** — stock chains are **monthly-only** (`SEM_EXPIRY_FLAG = M`, 3 live
  expiries) vs Nifty's 18 across `M/W`.
- **Chain depth** — strikes carrying live CE OI on next expiry: Nifty 75, stocks 21–39.
  `oi_wall_approach_distance` would often scan a window containing two or three live
  strikes.
- **IV base** — Nifty 11.5%, stocks 20.6–36.7%. `exhaustion_iv_spike_threshold = 3.0` is a
  26% relative move on Nifty's base and 8% on VEDL's. The IV conditions fire much more
  readily on Nifty, for free.

---

## 4. Corrections to claims made during the session

Recorded so the log isn't misleading.

**Capital blocker — withdrawn.** Claimed stock F&O lots are ~₹15L notional and a DLF
monthly would run ~₹43k/lot. Actual:

| sym | lot | notional | next-exp premium/lot |
|---|---|---|---|
| NIFTY (7 dte) | 65 | **15.6L** | ₹12,668 |
| VEDL | 1150 | 3.0L | ₹13,398 |
| ITC | 1725 | 4.9L | ₹14,835 |
| TCS | 225 | 5.2L | ₹15,885 |
| RELIANCE | 500 | 6.4L | ₹17,800 |
| TRENT | 225 | 6.6L | ₹22,838 |
| DLF | 950 | 6.2L | ₹24,985 |
| DIVISLAB | 100 | 7.4L | ₹27,395 |

**Nifty has the largest notional of the set.** Stock monthly lots cost 1.2–2.2× a Nifty
weekly lot, not 4×. The blocker does not exist. Worth noting separately: at ₹12,668/lot
against `default_capital 100_000` and `risk_per_trade_pct 10.0` (₹10k), one Nifty lot
already exceeds the risk budget today.

**OI wall threshold — inverted.** Claimed it would be dead on stocks. It fails on Nifty's
own fresh weekly, and RELIANCE/ITC monthlies clear it.

---

## 5. Resolved Dhan security IDs

Source: `dhanhq.fetch_security_list()` /
`https://images.dhan.co/api-data/api-scrip-master.csv`, per Searching security ID.

| sym | security_id | segment | lot |
|---|---|---|---|
| NIFTY | 13 | `IDX_I` | 65 |
| TRENT | 1964 | `NSE_EQ` | 225 |
| DLF | 14732 | `NSE_EQ` | 950 |
| VEDL | 3063 | `NSE_EQ` | 1150 |
| DIVISLAB | 10940 | `NSE_EQ` | 100 |
| TCS | 11536 | `NSE_EQ` | 225 |
| ITC | 1660 | `NSE_EQ` | 1725 |
| RELIANCE | 2885 | `NSE_EQ` | 500 |

Swapping the instrument itself is config-only — the engine was made asset-agnostic in
`directives/adr/TASK-002_asset-agnostic.md` (`security_id`, `exchange_segment`,
`instrument_type`, `yahoo_symbol`). The tuning is what isn't portable.

---

## 6. Multi-instrument feasibility — what must change

> **❓ Will the current config work on any stock added?**
> **No — and not for a tunable reason, for a units reason.** Roughly half the config is
> already instrument-agnostic; the other half is absolute numbers bound to one price, one
> lot size, one IV base.

### Per-instrument facts, pulled live (next expiry)

| sym | spot | lot | strike_int | ATR | ATR% | IV | live strikes | max CE OI | max in contracts |
|---|---|---|---|---|---|---|---|---|---|
| NIFTY | 23995.9 | 65 | 50 | 6.45 | 0.03 | 11.5 | 75 | 3,505,840 | **53,936** |
| HDFCBANK | 739.6 | 650 | 10 | 0.35 | 0.05 | 23.9 | 29 | 13,659,100 | 21,014 |
| RELIANCE | 1280.0 | 500 | 10 | 0.50 | 0.04 | 20.6 | 38 | 5,639,500 | 11,279 |
| ITC | 285.9 | 1725 | 2 | 0.20 | 0.07 | 21.4 | 31 | 8,238,600 | 4,776 |
| SBIN | 1020.6 | 750 | 10 | 0.60 | 0.06 | 24.7 | 30 | 2,669,250 | 3,559 |
| TCS | 2295.6 | 225 | 20 | 1.50 | 0.07 | 24.3 | 39 | 788,850 | 3,506 |
| ICICIBANK | 1445.7 | 700 | 10 | 0.80 | 0.06 | 12.5 | 40 | 1,950,200 | 2,786 |
| VEDL | 264.7 | 1150 | 5 | 0.15 | 0.06 | 36.7 | 23 | 2,942,850 | 2,559 |
| TRENT | 2938.3 | 225 | 50 | 1.90 | 0.06 | 28.9 | 23 | 413,550 | 1,838 |
| DLF | 650.9 | 950 | 10 | 0.45 | 0.07 | 31.2 | 23 | 813,200 | 856 |
| DIVISLAB | 7414.0 | 100 | 100 | 4.00 | 0.05 | 26.5 | 21 | 79,500 | **795** |

> **✅ The encouraging result**
> **`ATR%` is nearly uniform** — 0.03% (Nifty) to 0.07%. Per-minute noise as a fraction of
> price barely varies across instruments, so **one ATR multiple ports to all of them**.
> Normalization is a solved problem here, not a research project.

The unportable columns are `strike_int` (2 → 100, a **50× spread**) and contracts
(795 → 53,936, a **68× spread**).

### Nifty's tuning translated into portable units

```
SL 12 pts          = 0.050% of spot  = 1.86 x 1-min ATR
T1 24 pts          = 0.100%          = 3.72 x ATR
approach 80 pts    = 0.333%          = 12.40 x ATR
test 20 pts        = 0.083%          = 3.10 x ATR
strike_interval 50 = 0.208%
iv_spike 3.0       = 26.1% of ATM IV base (11.5)
oi_min 4,000,000   = 61,538 contracts = 1.14 x this chain's own max
```

> **🚨 `oi_wall_min_oi` is above Nifty's entire live chain**
> 4,000,000 is **1.14× the maximum OI on any strike** of the current weekly.
> `OI_WALL_REJECTION` cannot fire on NIFTY today and will not until OI builds toward
> expiry. Live condition, not a stock problem. Cross-ref
> TASK-188 OI Wall Wick Gate Postmortem 2026-07-17.

### Change list by category

**Cat 1 — auto-derivable, zero human input** (resolve once at startup per instrument):

| Param | Current | Source |
|---|---|---|
| `strike_interval` | 50 | median diff of live chain strikes |
| `nifty_lot_size` | 65 | `SEM_LOT_UNITS`, scrip master |
| `security_id` | 13 | scrip master by symbol |
| `exchange_segment` | `IDX_I` | `NSE_EQ` for stocks |
| `instrument_type` | `INDEX` | `EQUITY` |
| `yahoo_symbol` | `^NSEI` | `<SYM>.NS` |

**Cat 2 — price-scaled, must become ATR multiples** (13 values): `per_type_levels` ×12,
`oi_wall_approach_distance`, `oi_wall_test_distance`, `oi_wall_wick_min_range_pts`,
`breakout_deep_close_pts`, `exhaustion_level_proximity_pts`,
`structural_target_min_distance_pts`, `speed_filter_min_range_pts`,
`entry_zone_offset_pts`, `level_scan_range`, `trade_dedupe_tolerance_pts`.

**Cat 3 — OI-scaled, needs a different *kind* of threshold**: `oi_wall_min_oi`.

**Cat 4 — IV-scaled**: `exhaustion_iv_spike_threshold`, `breakout_iv_falling_threshold`.

**Cat 5 — already portable, leave alone**: all volume rules are *ratios* to a rolling
average (`exhaustion_volume_multiplier` 2.5, `breakout_weak_volume_ratio` 0.75,
`continuation_resume_volume_ratio` 1.2) — self-normalizing. Same for candle counts,
cooldowns, min-scores, `min_rr_ratio`.

### Making `oi_wall_min_oi` dynamic

Absolute share counts fail on three axes simultaneously: lot size, instrument, and
time-to-expiry. Proposed replacement — use the chain's own distribution:

> a strike qualifies as a wall if its OI is ≥ the **85th percentile of strikes with
> non-zero OI** on that chain

Self-normalizing on all three axes; no per-stock tuning. What p85 resolves to today
(contracts): NIFTY 22,006 · HDFCBANK 10,145 · RELIANCE 2,823 · VEDL 1,323 · ITC 1,299 ·
SBIN 1,246 · TCS 1,029 · TRENT 616 · DIVISLAB 558 · DLF 289.

`oi_wall_min_oi_change_pct` (5%) is already relative and can stay — but thin chains make a
5% OI move much noisier, so it likely needs an absolute contract floor so it can't fire on
a 20-lot change. That is the one place an absolute guard is still justified.

### Expiry profile for stocks — already right, one bug

`is_expiry_day_from_api()` (`detectors/expiry_detector.py:31`) queries `expiry_list()`
using the *configured* `security_id`, so it is **already per-instrument**. Point it at
TRENT and it returns True only on TRENT's monthly expiry. No change needed.

**The fallback is the bug.** `is_expiry_day_simple()` hardcodes `weekday() == 1` (Tuesday).
On a stock that fires *every* Tuesday, so any API hiccup flips a stock into
`EXPIRY_CONFIG` 4–5× a month instead of once. For stocks it should be "last Tuesday of the
month", or better, hold the previous profile rather than guess.

---

## 7. The 10-stock layer — architecture constraints

Intent: NIFTY keeps running unchanged; stocks are a separate layered entity pulling data
for ~10 instruments with per-stock dynamic config.

> **⚠️ Constraint 1 — the config singleton blocks concurrent instruments**
> `settings` is a module-level singleton (`config.py:59`) and detectors read `settings.*`
> **inside** `update()` (`detectors/oi_wall.py:100`), while `ExhaustionDetector.__init__`
> reads it at **construction** (`detectors/exhaustion.py:27`).
> **One process cannot hold 10 different configs concurrently as the code stands.**

Three ways out:

| Option | Diff | Cost |
|---|---|---|
| One process per instrument | zero code change | 10× Fly footprint — price it, given the Kronos memory history |
| Sequential loop, `apply_profile()` before each instrument's cycle | small | works *only* because the poll loop is sequential; needs one `AresEngine` per instrument (detector state must not be shared), constructed after its profile is applied |
| Thread config through detector calls | largest | the correct fix |

> **⚠️ Constraint 2 — Dhan rate limit caps the instrument count**
> One unique option-chain request per **3 seconds**. 10 stocks + NIFTY = 11 chains ×
> 3.3s ≈ **36s per cycle** against a 60s poll. It fits — but `oi_fetcher` retries up to 3×
> with backoff (`fetchers/oi_fetcher.py:127`), and a couple of retries blows the budget.
> **10 instruments is close to the practical ceiling at 60s**; the stock layer wants a
> slower poll (120s) or a smaller set.

**Suggested shape given both constraints:** one process, sequential over instruments, on a
slower poll than NIFTY, **collect-only** — no alerts, no trades — writing `ml_collection`
rows per instrument. That yields the OI/IV data no backtest can provide, leaves live NIFTY
untouched, and defers the singleton refactor until the data says a stock is worth trading.

---

## 8. Open items — recorded, not actioned

- Converting `config_profiles.py` thresholds from absolute points to %/ATR is the
  prerequisite for any real stock comparison. Not proposed as work.
- `oi_wall_min_oi` as a raw share count is a live Nifty issue worth its own look,
  independent of stocks.
- The OI/IV half is untestable historically; only a forward shadow run gets it.
- Whether live OI walls only fire near expiry is checkable against existing
  `ml_collection` rows.

## Related

- Configuration — the threshold table under discussion
- Detectors — the four setups
- Fetchers and Ingestion — Dhan chain retrieval
- TASK-185_SL-Target-Optimization — where 12/24/40 came from
- TASK-188 OI Wall Wick Gate Postmortem 2026-07-17 — prior OI-wall investigation
- Ares Build Log
