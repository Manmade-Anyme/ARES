# TASK-011 — OI-wall SL/configuration assessment

Date: 8 October 2026. Read-only analysis. No application code, configuration, database or order changes. Uses the 20 retained OI_WALL_REJECTION signal records, their original levels and previously retrieved Dhan one-minute candles. Nineteen signals predate September's lifecycle redesign; only September 28 is a retained confirmed entry under the later lifecycle. Seven watch-ready episodes are analyzed separately and are not additional confirmed trades.

## Recommendation

No configuration has been established as reliably profitable. The narrow candidate supported by this retrospective sensitivity is **SL 20 spot points, T1 25 spot points, existing T2 policy unchanged**. Compare prospectively against current SL16/T125 after investigating the entry-candle tracking issue below; do not label this an optimized or validated live configuration.

| Parameter | Current | Candidate for comparison | Reason |
|---|---:|---:|---|
| OI_WALL_REJECTION.stop_pts |16 |20 |Raises T1-first count from7/20 to10/20 at recorded reference;9/20 with next-minute entry. |
| target_1_pts |25 |25 |Preserves existing reward distance; lowering to20 did not establish improved full-strategy profitability. |
| target_2_fallback_pts |40 |40 |No justification to change simultaneously. Structural T2 may override this fallback. |
| min_rr_ratio |1.0 |1.0 |SL20/T125 passes with1.25 reward/risk. Do not weaken global gate to accommodate larger stops. |
| OI size/growth, persistence, retest distances |Existing profile values |Unchanged for this comparison |The path study does not re-run selection across all alternative detector settings. |

This is a proposed controlled comparison, not permission to implement. Independent break-even timing, wall/swing-based SL, option-premium targets and partial-position allocation are not available through these existing numeric knobs and would require separately evaluated behavior changes.

## The premise: T1 has occurred

Fresh original signal levels plus previously reconciled active/analytics records show **14 SL_HIT,4 STOPPED_OUT_AT_BE,2 T2_HIT**. The four breakeven outcomes imply earlier T1 progression under recorded position-manager behavior. The two T2 exits show movement beyond T1, although T2-first branch ordering can bypass a distinct T1 notification. Therefore the retained history is not zero-T1.

Since September's redesign, only one confirmed signal is retained (September28), and it stopped out. Recent profitable-direction watch messages are not recorded trade entries. Enlarging SL cannot convert a watch with no qualifying retest into a confirmed trade.

Five earlier signals used12-point stops; the other15 used16. Uniform16/25 and20/25 replays are counterfactual comparisons, not the actual historical mix.

## Configuration mechanics

- `config_profiles.py:42` sets per-type `SetupLevels(16.0,25.0,40.0)`, in spot points, for both profiles.
- `engine.py:19–46` places SL/T1 at fixed trigger-price distances. T2 is nearest favorable structural level beyond T1, with40 only a fallback. Raising fallback does not necessarily change a structural T2.
- `engine.py:293–304` requires reward-to-T1/risk >= min_rr_ratio1.0. **SL30/T125 or SL40/T125 suppresses signals**, regardless of seemingly attractive price-path results.
- `position_manager.py:333–360` moves tracked stop to entry upon T1. No independent configurable break-even threshold or verified partial execution is represented by that transition.
- `position_manager.py:375–380` can credit a T1-sized P&L override for a terminal BE event. That is a system accounting convention, not proof of executed partial profit. The economic full-position diagnostic below values BE at zero.

## First-target sensitivity

Measure first post-entry touch of T1 versus SL, holding original signal direction and date fixed. Recorded-reference scenario uses stored entry price; sensitivity uses next whole-minute open. Both begin price-path evaluation on first whole minute after active-record creation, excluding the signal candle's pre-entry extremes. This also omits the remaining seconds of entry minute; tick-perfect execution is unavailable.

No simultaneous stop/T1 candles occur in the tested comparisons; adverse-first and favorable-first counts agree. All prices and scores are spot points. A first-T1 score below assumes the full unit exits at T1; it isolates target reachability and is **not ARES's actual T2/BE strategy P&L**.

| SL | T1 | T1 first /20 recorded reference | T1 first /20 next-minute open | First-T1 gross point score: reference / next open | Passes current RR gate |
|---:|---:|---:|---:|---:|---|
|12|25|6|6|-18.00 / -18.00|Yes|
|16|25|7|7|-33.00 / -33.00|Yes|
|20|25|10|9|+50.00 / +5.00|Yes|
|25|25|10|9|+0.00 / -50.00|Yes|
|30|25|10|9|-50.00 / -105.00|No|
|30|30|9|8|-60.00 / -120.00|Yes|
|40|25|11|10|-85.00 / -150.00|No|
|40|40|7|7|-219.75 / -240.00|Yes|
|16|20|8|8|-32.00 / -32.00|Yes|
|20|20|11|10|+40.00 / +0.00|Yes|

SL16→20 changes three reference-price outcomes from SL-first to T1-first: August17, August31 and September2. Moving20→25 produces no further T1 successes. Consequently larger losing distances worsen this sample despite identical hit count. Entry-price sensitivity reduces the20/25 first-target gross score from+50 to+5 over20 trades before any costs. This is fragile in-sample evidence, not a profitability claim.

The seven hypothetical immediate-watch entries remain4/7 T1-first at SL16,20 and25 with T125. Thus the SL20 candidate's improvement in legacy confirmed entries does not establish that it reproduces the user's pullback-based watch method.

## Full-position exit diagnostic

Keep each original stored T2, T1=25, and move remaining full-position stop to entry after T1. No partial booking is assumed because no actual allocation rule is encoded in these records. SL/target ordering is adverse-first if both touched, though no such unresolved bars arise in these comparisons; newly reached T1 is not retroactively assigned a same-bar BE exit. EOD closes any remaining position. Original structural levels are held fixed; the detector and future eligibility/cooldowns are not rerun.

| SL | SL exits | BE exits | T2 exits | Gross equal-unit spot-point sum |
|---:|---:|---:|---:|---:|
|16|13|4|3|−88.80|
|20|10|6|4|−36.80|
|25|10|6|4|−86.80|

All three remain negative before costs. This diagnostic is not live P&L or risk-normalized option performance, but it prevents treating better T1 frequency as proof the whole strategy is fixed. With fixed rupee risk, wider-stop position sizes would differ; equal-unit sums do not represent that sizing scheme.

## Entry-candle tracking defect needs investigation before tuning

Confirmed source-code mechanism: main adds a new trade, then later in the same loop passes that signal candle's high/low into position updates (`main.py:405`, `main.py:468`). The manager accepts those extremes (`position_manager.py:295`) and can apply them to the new trade. Its timestamp clamp does not remove price history preceding the entry. This mechanism existed in August21 commit5055675 as well as current code.

**August27 evidence, signal397:**

- Snapshot15743 at09:17:21.764 reports signal spot24,254.15 and already-observed candle high24,277.35.
- Bearish SL is24,270.15, so this earlier high is already above the newly calculated stop.
- Active trade creation09:17:22.377; recorded SL exit09:17:23.012, about0.64 seconds later.
- Feeding this existing candle to manager deterministically yields SL even without a new adverse move; candle low does not hit T1.
- In the clean next-whole-minute path,16/25 reaches T1 first, while stored record says SL. This is the only first-target mismatch among20 original-geometry replays.

The code flaw is confirmed; this historical event is strongly consistent with it. Exact attribution is not provable without tick logs and deployed-version evidence, and other SL records must not be labeled false on this basis. A post-entry price-eligibility check is the needed behavior correction; its concrete implementation requires separate approval/work. Merely widening SL may conceal this defect.

## Why other apparent fixes are not established

- Raising SL to30/40 with unchangedT125 fails engineRR gate. Raising T1 alongside it reduces target frequency in these samples.
- Delaying BE or capping structural T2 may change outcomes but is not isolated by this experiment. Several old T2 distances are large (e.g. July27 first signal316.3 points, August28 172.85); editing fallback40 cannot cap them.
- Better manual pullback timing and larger tolerance of initial movement are separate dimensions. The user’s successful watch entries are not the same historical events/entry prices as the20 confirmed signals.
- No grid-search maximum is accepted as an optimized setting. Candidate20/25 is a modest, gate-compatible sensitivity with a clear comparison baseline. There is no out-of-sample test here; one current-lifecycle confirmed entry is insufficient for a reliable current-strategy success estimate.

## Sources and checks

- Supabase `ares_signals` original levels, `active_trades` and `trade_analytics`, plus the August27 ML snapshot cited above.
- Previously captured Dhan minute candles; read-only historical API semantics checked against [Dhan documentation](https://dhanhq.co/docs/v2/historical-data/).
- Local code and Git sources named above. Independent review reproduced16/25,20/25 and25/25 counts from raw candle data, verified RR incompatibility and the historical/current entry-candle mechanism.
- Simulated outputs are explicitly distinguished from actual outcomes, consistent with the [CFTC's explanation of hypothetical-performance limitations](https://www.cftc.gov/LearnAndProtect/AdvisoriesAndArticles/fraudadv_tradingsystem.html).
- Scratch scripts/results under `/private/tmp/ares_sl_sensitivity*` and `/private/tmp/ares_full_exit_sensitivity.py`. No changes applied beyond this research document.
