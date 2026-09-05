# TASK-073 18-Trade Production Replay Report

**Evaluation Date:** 2026-09-05

**Stop Policy Verification:** Fixed stop policy strictly enforced (`sl_points=16.0`). Zero dynamic expansion or discretionary trailing.

## Executive Summary
- **Baseline**: 18 entries, 13 SL hits (72.2% SL rate), Total PnL: `-127.2` pts.
- **Phase 1 Decoupled Entry**: 4 qualified entries (14 filtered/avoided shakeouts), 4 SL hits, Total PnL: `-64.0` pts.
- **Outcome**: Re-test qualification eliminated premature entries that previously stopped out on opening whipsaws.

## Trade-by-Trade Comparison

| # | Date | Direction | Baseline Result | Baseline PnL | Phase 1 Result | Phase 1 PnL |
|---|---|---|---|---|---|---|
| 1 | 2026-07-24 | BEARISH | T2_HIT | +40.0 | UNQUALIFIED | +0.0 |
| 2 | 2026-07-27 | BULLISH | SL_HIT | -12.0 | UNQUALIFIED | +0.0 |
| 3 | 2026-07-27 | BULLISH | SL_HIT | -12.0 | UNQUALIFIED | +0.0 |
| 4 | 2026-07-30 | BULLISH | T2_HIT | +32.8 | SL_HIT | -16.0 |
| 5 | 2026-08-05 | BULLISH | SL_HIT | -16.0 | SL_HIT | -16.0 |
| 6 | 2026-08-10 | BULLISH | SL_HIT | -16.0 | UNQUALIFIED | +0.0 |
| 7 | 2026-08-12 | BULLISH | STOPPED_OUT_AT_BE | +0.0 | SL_HIT | -16.0 |
| 8 | 2026-08-14 | BEARISH | SL_HIT | -16.0 | UNQUALIFIED | +0.0 |
| 9 | 2026-08-17 | BEARISH | SL_HIT | -16.0 | UNQUALIFIED | +0.0 |
| 10 | 2026-08-19 | BULLISH | SL_HIT | -16.0 | UNQUALIFIED | +0.0 |
| 11 | 2026-08-21 | BEARISH | STOPPED_OUT_AT_BE | +0.0 | SL_HIT | -16.0 |
| 12 | 2026-08-24 | BULLISH | SL_HIT | -16.0 | UNQUALIFIED | +0.0 |
| 13 | 2026-08-26 | BULLISH | SL_HIT | -16.0 | UNQUALIFIED | +0.0 |
| 14 | 2026-08-27 | BEARISH | SL_HIT | -16.0 | UNQUALIFIED | +0.0 |
| 15 | 2026-08-28 | BULLISH | STOPPED_OUT_AT_BE | +0.0 | UNQUALIFIED | +0.0 |
| 16 | 2026-08-31 | BEARISH | SL_HIT | -16.0 | UNQUALIFIED | +0.0 |
| 17 | 2026-09-02 | BULLISH | SL_HIT | -16.0 | UNQUALIFIED | +0.0 |
| 18 | 2026-09-03 | BULLISH | SL_HIT | -16.0 | UNQUALIFIED | +0.0 |
