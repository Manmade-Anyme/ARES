# ARES Project Todo & Roadmap

## Active Tasks

### 📊 Trade Efficiency Audit Backlog (see `docs/ARES Trade Efficiency Audit 2026-07-02.md`)
**Shipped in TASK-171:** R:R gate, time-stop risk-off, exhaustion observation mode, cooldown reset after stop-out.
**Shipped in TASK-172:** P1 block — breakout gate 3-of-5 (closed_back excluded), IV-crush v2 (HIGH exempt/symmetric/60-sample), intrabar fill-at-level exits, config speed filter + ≥60% confidence bars, signal_id join fix, IST→UTC entry timestamps, add_trade dedupe.
**Shipped in TASK-173:** P2 items 16 & 18 — trend-regime filter (VWAP/PDH-PDL counter-trend gate, HIGH downgraded to observation-only / MEDIUM suppressed) and WebSocket tick feed augmenting the 60s REST poll for sub-60s SL/T1/T2 exit checks (best-effort, REST-only fallback). Items 14–15 stay `[SKIP]`.
- [ ] Full threshold sweep against the ml_collection dataset (audit item 17, remaining scope).
- [ ] Untagged P0 items pending decision: distinct BREAKEVEN exit type, candle timestamp dedup.
- [ ] Validate TASK-172 P1 tuning against live Discord output over the next sessions.
- [ ] Validate TASK-173 trend-regime filter and tick feed against live Discord output over the next sessions.
- [x] Run `scratch/restore_orphan_trades.py --apply` — 10 orphans restored to `active_trades` on 2026-07-02 (1 duplicate + 1 unmatched post-market row skipped).

### 🔒 Dhan Token Automation (Programmatic Refresh) [COMPLETED]
**Description:** Migrated to centralized `dhanrenew` microservice running on Fly.io which auto-renews tokens into Supabase. ARES fetches client ID and access token from Supabase dynamically on startup and auto-recovers mid-session.

**Steps Completed:**
- [x] Integrate Supabase dynamic credentials fetcher on startup.
- [x] Implement dynamic mid-session credentials reloading on auth failure.
- [x] Remove Dhan client ID and access token from local `.env` and deployment secrets.

### 📐 Sizing Safety Gate Styling Enhancement [TODO]
**Description:** Enhance `alerts.py` to clearly label trades as "Paper Sizing Only" when capital limits calculate suggested lots to 0, ensuring alerts remain visible and active.
- [ ] Add a "Paper Sizing Only" visual indicator to Discord embed alerts.
- [ ] Highlight paper-sizing signals differently in the local console UI.

### 🧹 Scratch File Cleanup [TODO]
**Description:** `scratch/` is for local-only work (audits, debug scripts, throwaway notes) and must never be pushed. `scratch/` was added to `.gitignore` in TASK-169, but files already committed before that point remain tracked.
- [x] Audit `scratch/` for any files still tracked in git and remove them from the repo (`git rm --cached`). Done 2026-07-20: 15 tracked files untracked; local copies and the already-untracked `restore_orphan_trades.py` unaffected.
- [ ] If a new scratch file is ever accidentally pushed in a PR, remove it from that PR before merge.

---

## Completed Tasks
- [x] Dhan Token Automation (Programmatic Refresh)
- [x] Dynamic PDH/PDL fetching from Dhan API.
- [x] Target sorting logic based on proximity and momentum.
- [x] Supabase RLS policy fix for trade logging.
