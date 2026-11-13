# Problem Brief — TASK-20261114 Weekly Performance Report Evolution & Root Cause

**Date:** 2026-11-14  
**Status:** Approved & Implemented  
**Investigator:** Problem Understanding Agent  

---

## 1. Problem Statement (Original)
> "Previously it used to work some time back but then out of nowhere it stopped. Since the last few times it hasn't worked. You said you found the issue and it will work, but next week it will not get triggered. I want you to check the implementation when it was done, how it was done, and what the difference is now. Go through all the previous change log and understand how it was working before when it was introduced for the first time."

## 2. Problem Statement (Simplified)
Trace the full historical evolution of the weekly performance digest from inception (July 2026) to today. Compare original mechanics against recent iterations to explain why it worked previously, why it broke, and how the current solution prevents recurrence.

---

## 3. Historical Evolution & Comparative Analysis

| Dimension | Original Implementation (`d6e66f1` / `a430140`, 24 Jul 2026) | Intermediate Broken State (`eab96ea`, 30 Oct – 07 Nov 2026) | Permanent Architecture (`TASK-20261114`, 14 Nov 2026) |
|---|---|---|---|
| **Trigger Point** | `if current_time >= time(15, 30):` (session break) -> shifted to `if current_time >= time(15, 29) and not report_sent:` | Gated strictly inside `is_performance_report_window` (`[15:28, 15:30)` IST) | **Decoupled:** GitHub Actions cron at **15:45 IST** (15 min post-close) + fallback in `main.py` |
| **Upper Bound Gate** | **None.** Open-ended `>= 15:29` until process exit. | **Strict `< 15:30`**. Excluded if 15:30 reached. | None on cron; independent batch query from Supabase. |
| **Loop Latency** | 1–2 seconds per cycle (3 simple detectors, no ML, no Jev, no option ATR). | 15–75 seconds per cycle (50+ ML features, Jev IPC, option chains, Greek sizing). | Zero impact; runs outside the trading loop. |
| **Container Lifecycle** | Simple single-process container; manual or loose scaling. | Fly machine scaled via external `cron-job.com` POST to `/stop` with **5-second kill timeout** (`SIGKILL`). | GitHub Actions runner (independent) + Fly `kill_timeout = '30s'`. |
| **Shutdown Drain Hooks** | Zero drain hooks before report. Fired directly before `break`. | Heavy async drains (`capture_task` 5s timeout, quote flushes) ran before fallback. | Standalone runner requires zero drains; queries committed DB rows. |
| **Failure Isolation** | Live container failure killed report. | Live container failure / Dhan freeze killed report. | **100% isolated:** Database retains trades; GitHub Actions sends even if Fly machine crashed hours earlier. |

---

## 4. Why It Worked Initially
1. **Unbounded Trigger (`>= 15:29`):** In commit `a430140`, the condition was `if current_time >= time(15, 29) and not report_sent:`. If the process reached 15:29:10 or 15:30:05, the condition was still TRUE and sent the report immediately.
2. **Minimal Loop Latency:** ARES in July 2026 had only 3 detectors and no ML snapshots, Jev models, or option ATR calculations. It iterated every 60 seconds with zero lag.
3. **No External Hard-Kill:** The Fly container was not being hard-stopped with a 5-second `SIGKILL` timeout.

---

## 5. Why It Broke "Out of Nowhere"
1. **Added Strict Upper Bound (`< 15:30`) in `eab96ea` (30 Oct 2026):**
   To prevent after-hours restarts from re-posting, an upper bound was added: `time(15, 28) <= current_time < time(15, 30)`. This turned a durable trigger into a 120-second trap.
2. **Loop Weight & Provider Drops:**
   As ML collection (TASK-183), Jev consumer (TASK-210), and Greek sizing (MANM-176) were added, provider latency occasionally halted or slowed the loop. If the engine stalled at ~14:50–15:10 IST, it never stepped into the 15:28–15:30 window.
3. **Fly 5-Second Force Kill on Shutdown:**
   In `TASK-20261107`, an in-process fallback was added to `main.py`'s `finally:` cleanup. But `cron-job.com` calls Fly's `/stop` API at 15:30/15:35. Fly sent `SIGINT`, waited 5 seconds, and issued `SIGKILL`. Because `main.py`'s shutdown drains (5s timeout on `capture_task` + quote flushes) consumed >5 seconds, Fly terminated the container before `_ensure_performance_reports_sent` could execute.

---

## 6. How the Current Solution Guarantees It Will Trigger
1. **Batch Decoupling:** Performance reporting is a post-market summary of Supabase `trade_analytics`. It does not belong inside an active 1-minute execution loop.
2. **Scheduled GitHub Actions Cron:** Runs every weekday at 15:45 IST (10:15 UTC). At 15:45 IST, all trades are committed and settled in Supabase. The job runs in a dedicated runner with zero timeout pressure.
3. **Container Lifeline:** `fly.toml` now specifies `kill_timeout = '30s'` so even local in-process drains have adequate time.
4. **On-Demand Dispatch:** `scripts/send_weekly_report.py` allows immediate manual dispatch or backfill via CLI or GitHub `workflow_dispatch`.

[[TASK-20261114_guaranteed-weekly-performance-report]] [[Ares Build Log]]
