# ARES Project Todo & Roadmap

> **Status 2026-08-01**: running again. The 2026-07-31 note below said "no code
> work pending" — at that moment ARES could not boot at all, and nobody knew,
> because the Fly machine was already stopped for the weekend and a deploy to a
> stopped machine only stages the image. TASK-199 fixed that and three further
> defects; TASK-200 corrected the startup banner. Verified on the deployed image,
> not just locally. One active item, below, plus three parked.
>
> Standing lesson: a green test suite and a successful deploy proved nothing here.
> Both were green through a total startup crash.

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
- [ ] **`superfly/flyctl-actions/setup-flyctl@master` is pinned to a moving branch.**
      `.github/workflows/deploy.yml:57`, in the step that has `FLY_API_TOKEN` in
      scope — a token that can deploy to *and destroy* the production app.
      Whatever sits on Superfly's `master` when the job runs executes on the
      runner, with no review and no control over when it changes. They publish
      tags (`v1.6`, `v1.5`, `v1.4`); pinning to a tag or a commit SHA costs
      nothing. **The only parked item with a security dimension.**
- [ ] **No trading-day or holiday gate.** The session check at `main.py:188` is
      time-of-day only (09:15–15:30); `weekday()` is used solely to pick the
      Friday report. Weekends are safe only because the cron is Mon–Fri — on a
      **weekday NSE holiday** the machine still starts and ARES runs its full
      loop. That matters more than "stale data": Dhan serves *false* spot prices
      during off-hours maintenance (observed 2026-08-01: `Spot=25965.35` against a
      Friday range of 24350–24430, ~+6.4%). A manual start that day loaded an open
      position and booked its exit off that print. Everything downstream of spot —
      structure distances, detectors, SL/target checks — is wrong in that window.
- [ ] **Workflow actions still declare Node 20.** `actions/checkout@v4` and
      `actions/setup-python@v5` (`.github/workflows/deploy.yml:18,20,56`) target a
      runtime GitHub has removed and now force onto Node 24, which is what the
      recurring annotation reports. Harmless today; when the shim goes the job
      fails to start. Failure mode is the concerning part: CI red does not block a
      merge here, so this surfaces as **merges succeeding while the image silently
      stops updating**. Latest majors are `checkout@v7` / `setup-python@v7`, both
      `node24`. Must be proven by a real CI run, not a diff review.
- [ ] ~~**Nothing restarts the Fly machine at 09:15.**~~ **Resolved — do not
      re-raise.** An external cron-job.org job POSTs
      `api.machines.dev/v1/apps/<app>/machines/<id>/start` Mon–Fri before open; it
      lives outside the repo, so "no cron/workflow in the codebase" is expected,
      not a gap. See `Tools/Cron-job.org Fly API Setup` in Obsidian. Residual risk
      is the holiday gate above, plus the fact that the **machine ID is
      hard-coded in the cron URL** (`84e671b2119168` as of 2026-08-01) — destroy
      or recreate that machine and the cron 404s every morning, silently.
- [ ] **Full threshold sweep against `ml_collection`** (audit item 17, open since
      2026-07-02). Now unblocked — the table has labels for the first time.
- [ ] "Paper Sizing Only" indicator on Discord alerts and the console UI.
      Directly relevant to the 0-lot finding above.
- [ ] Distinct BREAKEVEN exit type; candle timestamp dedup. Untagged P0s from the
      2026-07-02 audit, never decided.

---

## Resolved

- [x] **TASK-200** (2026-08-01) — startup banner corrected. The banner is rendered
      twice (console + Discord) and the copies had drifted: a doubled `+`, a
      hardcoded detector list missing Trend Continuation, a `09:15 to 23:30 IST`
      session string eight hours longer than the real gate, and no ML Predictor
      line at all. `SESSION_DISPLAY` / `detector_names()` now live once in
      `config.py` and both renderers read them.
- [x] **TASK-199** (2026-08-01) — post-merge audit of PRs #57–#61. Four defects
      that all survived a green 330-test suite. **ARES could not start at all**:
      `main.py:17` imports `ml_signal.predictor` at module scope, but `joblib` and
      `xgboost` were absent from `requirements.txt` and the Dockerfile installs
      only that file. Confirmed in production — v94 crash-looped on
      `ModuleNotFoundError`. Also: live prediction raised on ~2 of 3 signals
      (`None` structure features made a single-row frame object-dtype); break-even
      exits posted a blank Discord Action line; and `flatten_features` filled
      unknown distances with `0.0`, i.e. "spot is exactly at support/resistance" —
      a worse sentinel than the `100.0` TASK-194/195 removed to get there.
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
