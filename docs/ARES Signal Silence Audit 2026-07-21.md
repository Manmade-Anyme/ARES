# ARES Signal Silence Audit — 2026-07-21

User reported no Discord trade alerts. Full audit of the signal → alert chain.
**Verdict: the system was working. The silence was correct.**

---

## Why there were no alerts

Today was a **genuine expiry day** — `dhan.expiry_list()` returns `2026-07-21` as the
nearest expiry. ARES correctly loaded `EXPIRY_CONFIG`, which raises every detector bar
above the `NON_EXPIRY_CONFIG` that produced yesterday's 6 signals. On a 95-point grind,
nothing cleared them.

Confirming which profile is live: today's boot banner printed `Cooldown : 20 minutes`.
20 exists only in `EXPIRY_CONFIG` (`config_profiles.py:205`) — NON_EXPIRY *and* the
`TuningConfig` class default are both 15, so a printed 20 can only come from
`apply_profile(EXPIRY_CONFIG)`. All 111 `ml_collection` rows carry `is_expiry_day=1`,
sourced from the same runtime variable that selects the profile. ATM theta -42.7..-58.5
and gamma 0.0029..0.0045 are 0-DTE magnitudes.

### The one near-miss

| | |
|---|---|
| Time | 10:08:28 IST (`ml_collection` id 5814) |
| Candle | O 24227.90 / H 24228.30 / L 24224.15 / C 24227.65, vol 914,587 |
| vol_ratio | **2.65** (20-bar mean 345,108) |
| body/range | 0.060 |
| NON_EXPIRY gate (2.5x) | passes → EXHAUSTION_REVERSAL BULLISH fires |
| EXPIRY gate (3.5x) | fails — threshold 1,207,877 |

Verified by replaying the real `ExhaustionDetector` against the real tape: 1 signal
under NON_EXPIRY, 0 under EXPIRY. So the expiry profile rejected the day's only
qualifying setup — correct as configured, but the open tuning question.

Other detectors were legitimately starved: strict inside day (high 24262.20 vs PDH
24266.10, missed by 3.9 pts; low 24163.75 vs PDL 24135.85), and OI wall needs a >15%
single-poll build on expiry vs ~5% observed.

---

## Confirmed working

Machine booted 09:07 IST pre-open · heartbeats every 15 min · buffers 30/30 ·
111 full snapshots · **both Discord webhooks HTTP 200** · `send_discord` has one call
site inside `if signal:` so nothing was swallowed · deploy is post-TASK-190.

---

## Bugs found and fixed — PR #46 (merged)

Both ML-training-data only. The live detectors read the fetcher output directly in
`engine.tick` (`main.py:211`) *before* `ml_collector.snapshot` (`main.py:214`), so
neither could suppress a signal.

1. **`pcr_oi` pinned to exactly 1.0 on every row ever collected.**
   `_compute_totals_from_chain` read nested `{"ce": {"oi": ...}}` keys but
   `OIFetcher.fetch_chain` emits flat `ce_oi`/`pe_oi`. Totals stayed 0 → divide-guard
   fired. Live chain check: true PCR **0.7323**, matching the user's independent ~0.74.
   `oi_concentration` was likewise pinned to 0. Guard now returns `None`, not `1.0`.

2. **`iv_change_1` structurally 0.0 forever.** `snapshot()` appended the current bar to
   `iv_history`/`volume_history` *before* the compute calls, so `features.py:74` diffed
   `current_iv` against itself. Also capped `iv_percentile` at 95.0, duplicated the last
   volume in `vol_slope_5`, biased `vol_ratio` toward 1.0. `iv_acceleration` was
   differencing history against itself independently.

3. **The fix was one-sided** (caught by Codex review). `ml_signal/live.py` is an
   independent caller with the *same* append-before-compute defect. Fixed in `19c674c`;
   both callers now honour one contract — **history holds prior bars only** — asserted
   for both by `TestHistoryContractHoldsForEveryCaller`.

**Why it shipped green:** the pre-existing collector test mocked the chain in the nested
shape the fetcher never emits, so it asserted the bug was correct. Same failure mode as
the Kronos validation gap: the suite mocked the boundary the bug lived at.

**Not recoverable:** `pcr_oi` / `oi_concentration` are lost for all pre-fix rows —
`ml_collection` stores `raw_candle` and `raw_atm_oi`, never the chain.

**Feature-distribution note:** `vol_ratio`, `vol_slope_5`, `iv_percentile` and
`iv_acceleration` now have different semantics than historical rows. Training across the
boundary mixes two definitions — use a cutoff date or recompute the recoverable ones.

---

## Three false-evidence traps (do not diagnose from these)

- **`detector_scores` is always all-zeros, even when a signal fires.**
  `collector.py:192-196` one-hots `str(signal.setup_type)`, which yields
  `"SetupType.EXHAUSTION_REVERSAL"` and never matches the bare literal. No
  `trend_continuation` key at all. Verified against 07-20 rows that did fire.
- **`meta_features.dte` is a hardcoded 7.0 placeholder** (`main.py` passes `None`,
  `features.py:182` substitutes). `dte=7.0` beside `is_expiry_day=1` is not a contradiction.
- **`flyctl logs` retains ~100 lines spanning multiple days.** The "Non-expiry day" line
  matched during this audit was *yesterday's*. Always check the timestamp on the line.

Also: a Discord webhook GET returns **403 without a User-Agent** (Cloudflare) — set one
or you will misread a healthy webhook as dead.

---

## Open backlog

| Item | Type | Note |
|---|---|---|
| Expiry `exhaustion_volume_multiplier` 3.5 → ~3.0 | tuning | The thing that cost today's 10:08 setup. Needs a backtest and a chosen number. |
| `exhaustion.py:42-48` includes current candle in its own baseline | live path | ~5% conservative bias on every ratio. Behaviour change. |
| Expiry from `oi_fetcher.get_nearest_expiry()` | correctness | Decided once pre-open today with a hardcoded `weekday()==1` fallback; locks the wrong profile on a Tuesday holiday. |
| VWAP survives restart | live path | Dhan returns the **whole session** as arrays on every poll (149 candles at 11:43); `price_fetcher.py:173-177` uses `[-1]` and discards the rest. ~5 lines, no new API call. Deferred by user. |
| Discord non-2xx swallowed | ops | `alerts.py:96-101` prints to stdout; `main.py:263-264` is dead code. |
| Nothing starts the Fly machine before 09:15 | ops | Today's 09:07 boot was luck. Cost 46% of the 07-15 session. |
