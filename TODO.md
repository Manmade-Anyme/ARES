# ARES Project Todo & Roadmap

> **Status 2026-07-31**: system stable, data clean, no code work pending.
> Deliberate decision to stop changing code. One active item, below.

---

## Active

### 🎯 SL / Entry / Target recalibration — per detector
**Owner: user.** Not started, not scoped. To be discussed before any code.

The data is finally ready for it. As of 2026-07-31, after TASK-194/195 and the
TASK-188 migration: orphans 0, labels written, timestamps correct, fixtures gone.
`trade_analytics` joins to `ares_signals` for SL/T1/T2 on every trade.

Starting picture (118 trades, clean data):

| detector | n | SL | T1 | T2 | SL_HIT% | avg P&L |
|---|---|---|---|---|---|---|
| EXHAUSTION_REVERSAL | 56 | 12.0 | 24.0 | 81.8 | 71.4% | +5.66 |
| TREND_CONTINUATION | 34 | 25.0 | 40.0 | 80.0 | 40.0% | +0.25 |
| FAILED_BREAKOUT | 14 | 15.0 | 30.0 | 94.7 | 55.6% | +17.96 |
| OI_WALL_REJECTION | 13 | 12.0 | 25.0 | 40.0 | 40.0% | +6.07 |

Two things to carry into that discussion:

- **Multi-day carry is intentional** (TASK-170, confirmed 2026-07-31). There is no
  EOD square-off and none is wanted. T2 rides until hit or stopped.
- **`pnl_points` is NIFTY spot, and that is correct.** ARES is a spot-based system:
  detection, entry, SL, targets, ML features and P&L are all judged on the spot
  chart. The options layer is derived reference projected from spot via delta, and
  may eventually be removed. Exit premium is deliberately **not** recorded and
  pricing trades in premium is out of scope. See the design principle in
  `README.md`. Still worth carrying into the SL discussion: the 24 multi-session
  carries are **+1,011.7 pts of a +655.5 total**, so everything held intraday is
  net negative and the carries are where the entire edge sits.

---

## Open but parked — no action agreed

Kept so they are not lost. None are in progress.

- [ ] **82 of 111 trades sized to 0 lots** (empty Dhan balance). Affects the
      options sizing layer only — spot-based signal quality and recorded P&L are
      unaffected, since those never depend on lots. Not a code issue.
- [ ] **`oi_wall_min_oi = 4_000_000` is a raw share count.** Sits above the entire
      live chain for most of a weekly cycle; fires only as OI builds toward expiry.
      TASK-195 now records `max_*_oi` / `p85_*_oi`, so the proposed
      "wall = OI ≥ p85 of non-zero strikes" rule is testable once enough clean
      rows accumulate.
- [ ] **`breakout_detector` and `continuation_detector` have the stateful-skip bug**
      TASK-188 fixed for `oi_wall`. Left alone deliberately — FAILED_BREAKOUT is
      the best performer and there is no evidence its rate is wrong.
- [ ] **Nothing restarts the Fly machine at 09:15.** Cost 46% of a session on
      2026-07-15. Machine start/stop is cron-job.org → `api.machines.dev`.
- [ ] **Full threshold sweep against `ml_collection`** (audit item 17, open since
      2026-07-02). Now unblocked — the table has labels for the first time.
- [ ] "Paper Sizing Only" indicator on Discord alerts and the console UI.
      Directly relevant to the 0-lot finding above.
- [ ] Distinct BREAKEVEN exit type; candle timestamp dedup. Untagged P0s from the
      2026-07-02 audit, never decided.

---

## Resolved

- [x] **TASK-195** (2026-07-31) — orphaned trades and the `structure_features`
      sentinel repaired rather than deleted. 29 trades relinked, 5,226 rows nulled
      in place, nothing deleted.
- [x] **TASK-194** (2026-07-31) — `ml_collection` given a real join key and its
      first labels. Root cause was `signal_id` storing a random display code while
      `trade_analytics` stored `db_id`; 0 of 119 rows overlapped.
- [x] **TASK-188 migration** (2026-07-31) — 9 test fixtures and 4 fixture signals
      purged, 61 naive-IST timestamps corrected. All hold times now positive.
- [x] **Orphan rate** — not an ongoing defect. TASK-172 fixed the source on
      2026-07-03. Verified over the last 10 trading days: 57 signals / 57 trades /
      57 collected rows, zero orphans.
- [x] Trade Efficiency Audit backlog — shipped across TASK-171/172/173; the
      withholding gates were then removed entirely by TASK-182.
- [x] Dhan token automation, dynamic PDH/PDL, target sorting, Supabase RLS fix.
- [x] `scratch/` untracked (TASK-169 + 2026-07-20 cleanup).
