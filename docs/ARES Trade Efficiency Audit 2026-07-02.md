# ARES Trade-Efficiency Audit — 2026-07-02

Full audit of the ARES repo + live Supabase trade data (Jun 24 – Jul 2, 29 trades, last 10 days). Goal: improve trade efficiency. TASK-169 (OI wall 2-candle confirmation) merged but not yet live during the data window.

---

## Verdict

Engineering solid (clean 5-layer architecture, 141 tests, real ops story). Trading results negative. Analytics layer was lying (orphaned trades, mislabeled wins) — measurement fixed first, strategy tuning second.

## Live Performance (Supabase, old config)

| Metric | Value |
|---|---|
| Signals | 31 (~5/day) |
| Trades logged | 29 |
| Closed | 17 → **-219 pts net, -12.9 avg** |
| Stuck OPEN (orphaned) | 12 (41%) |
| Exit split | 12 SL / 3 T2 / 2 T1 |

Per setup (closed): **Exhaustion -211 pts (7/8 stopped out)** · OI Wall +30 pts (only profitable) · Failed Breakout -38 (1 trade). Both directions lose. 1 of 6 days green.

## Under trader's real model (40% booked at T1, SL→cost, runner)

Simulated vs ml_collection minute data (Jun 29 – Jul 2, 22 trades): **-163 pts, 27% win rate**.

Key insight: **T1 reached only 3/22 (14%)**. Exit style can't rescue entries that die ~27 pts against before T1 prints. Bottleneck = entry quality + target geometry, not exit management.

New findings from data:
1. **Hidden winners in orphans**: +85.6, +45.7, +37.9 EOD marks on "OPEN" trades — direction right, momentum died before 35pt T1. Time-stop / momentum target would harvest ~+169 pts.
2. **Stop slippage from 60s close-only exit detection: avg +6.8 pts/stop** (worst +34.5). 12 stops ≈ 82 pts burned on detection lag.
3. **R:R inverted on OI wall**: Jul 2 stops risked 46.4 / 39.8 pts for 35pt T1. No R:R gate exists.
4. Duplicate trade logged (06-29 14:12 OI wall, twice) — double add_trade.

## Audit Findings (ranked)

### A. Exit management
1. Breakeven-only trail (position_manager.py): T1 hit → SL to entry, no further trail. T1 winners round-trip to 0.
2. Breakeven exits logged as "T1_HIT" wins → win-rate inflated. Data proof: Jun 30 "T1_HIT" with **pnl -4.9**.
3. Exits on 60s close-only spot polls → intrabar SL/T1/T2 touches missed/late.
4. ~~No EOD close~~ → **RESOLVED 2026-07-02**: multi-day carry is intended behavior; `_initialize_db` was deleting previous-day trades at startup — fixed to load all non-closed trades regardless of date. Backfill script: `scratch/restore_orphan_trades.py`.
5. P&L tracked in index points, not option premium — theta/IV-crush invisible.

### B. Entry quality / R:R
6. No R:R gate: OI wall worst case risks 45-75 pts for 35pt T1 (entry drifts past wall, SL at strike±buffer).
7. Breakout min_score=2 includes required `closed_back` → effectively 1 extra coin-flip condition = signal.
8. OI wall 2-candle confirm + 60s poll → up to ~3min entry lag on 1-min scalps.
9. No time-of-day gates (fires 09:15 chop, 15:00+ gamma; one trade opened 15:28:25).
10. No trend-regime filter — all three detectors are mean-reversion fades; trend days bleed (Jun 30: -80, Jul 1: -71).
11. IV-crush filter blunt: kills ALL bullish incl HIGH conf; 20-sample CE-IV percentile; asymmetric.
12. Global cooldown 15-20min blocks all detectors even after instant stop-out.

### C. Data quality
13. No candle timestamp dedup (price_fetcher grabs `[-1]` every poll) → duplicate/skipped candles, VWAP volume double-counts.
14. All OI/IV deltas poll-to-poll 60s → `writers_holding` ≈ coin flip; noise feeds score matrices.
15. avg_volume includes current candle → dilutes weak-volume test.
16. PDH/PDL fallback hardcoded 24100/23900 → whole day mislevelled if fetch fails. Should alert + halt.
17. `signal_id` NULL in all trade_analytics rows — join to ares_signals broken.
18. Timestamp bug: entry_ts = IST-labeled-UTC, exit_ts = real UTC → hold-time analysis impossible.
19. Confidence bars inconsistent: breakout HIGH=4/6, OI wall/exhaustion HIGH=2/4; speed-filter threshold hardcoded.

## Recommended Improvements (prioritized)

### P0 — small, high impact
1. **R:R gate**: reject signal if SL dist > T1 dist (config `min_rr_ratio ≥ 1.0`). Sim: ~+120 pts / 4 days. ✅ SHIPPED (TASK-171)
2. **Time-stop / momentum target**: exit or tighten after N candles without progress — harvests orphan winners. ✅ SHIPPED (TASK-171 — SL tightens to entry after `time_stop_minutes`, exit type `TIME_STOP`)
3. **Gate/kill exhaustion entries** (trend filter or alert-only) — 7 full stops in 4 days. ✅ SHIPPED (TASK-171 — `exhaustion_alert_only`, observation mode)
4. **Distinct BREAKEVEN exit type** in analytics — honest win rate. 
5. **Candle timestamp dedup** in main loop (also fixes VWAP double-count). 
6. **Cooldown reset (or halve) after SL_HIT** — missed re-entry is pure opportunity cost. ✅ SHIPPED (TASK-171 — cooldown cleared on SL_HIT)


### P1 — tuning (validate against live Discord output)
8. Raise `breakout_failure_min_score` 2→3; exclude `closed_back` from score. ✅ SHIPPED (TASK-172 — closed_back is the gate, not scored; min 3 of 5 conditions)
9. Time-of-day gates: no entries before 09:30 / after 15:00 (config). [SKIP]
10. IV-crush filter: exempt HIGH confidence, lengthen lookback (~60 samples), consider symmetric. ✅ SHIPPED (TASK-172 — HIGH exempt, 60-sample CE+PE lookbacks, symmetric; alert-only signals bypass)
11. Intrabar high/low exit checks in update_trades (avg 6.8 pts slippage/stop recovered). ✅ SHIPPED (TASK-172 — candle high/low checks, fill-at-level exits, pessimistic same-candle resolution)
12. Speed-filter threshold into config_profiles; standardize confidence bars (~≥60% of matrix). ✅ SHIPPED (TASK-172 — `speed_filter_*` config; shared HIGH bar ≥60%: breakout 3/5, wall/exhaustion 3/4)
13. Fix signal_id logging + timestamp timezone consistency; dedupe add_trade. ✅ SHIPPED (TASK-172 — trade_analytics.signal_id ← ares_signals.id; entry timestamps IST→UTC; 1-pt dedupe guard)

### P2 — structural
14. Track option premium at entry/exit (chain already fetched every cycle) → true P&L incl theta. [SKIP]
15. Per-detector cooldowns; signal cache keyed (setup, direction, level) with TTL. [SKIP]
16. Trend-regime flag (VWAP/PDH-PDL position) → block or size-down counter-trend.
17. Backtest thresholds against ml_collection (50+ features/min accumulating — unused asset). [TODO] ◐ First pass done in TASK-171 (gate validation: -162.8 → -36.7 pts); full threshold sweep open.
18. WebSocket ticks for exit monitoring instead of 60s polls.

## Status Log
- 2026-07-03: TASK-172 shipped the P1 block (items 8, 10–13; 9 stays [SKIP]): breakout gate 3-of-5 with closed_back excluded, IV-crush v2 (HIGH exempt / symmetric / 60-sample), intrabar fill-at-level exits, config-driven speed filter + standardized ≥60% confidence bars, signal_id join fix, IST→UTC entry timestamps, add_trade dedupe. 181 tests green.
- 2026-07-02: TASK-171 shipped the four P0 gates (R:R, time-stop, exhaustion observation mode, cooldown reset). Backtest on last 10 days: -162.8 → -36.7 pts under the 40/60 execution model.
- 2026-07-02: Multi-day trade carry fixed (`_initialize_db` no longer wipes previous-day rows). 141 tests green. Backfill script created (dry-run default, `--apply` to write).
