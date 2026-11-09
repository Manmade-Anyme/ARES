# Deep Audit: Weekly Discord Performance Report Delivery Failure

**Audit Date:** 2026-11-07  
**Scope:** Friday session performance reporting (`reports.py`, `main.py`, Fly.io deployment, Discord delivery)  
**Status:** Root causes identified; data intact; remediation outlined.

---

## 1. Executive Summary

A deep audit was conducted to investigate why the weekly performance report (`📊 ARES WEEKLY PERFORMANCE`) was not received on Discord on Friday, 6 November 2026.

1. **Data & Metrics Integrity:** Intact. The database holds 9 recorded closed trades for the week of `02 Nov – 06 Nov 2026`. Querying, metrics aggregation, and Discord embed formatting were verified end-to-end against live Supabase data and Discord webhook configurations.
2. **Primary Root Cause:**
   - **Premature Machine Shutdown / Process Inactivity:** On Friday, November 6, trading loop activity and quote persistence stopped at **14:58:35 IST** (`09:28:35 UTC` recorded in `latest_quotes`). The engine was not actively iterating when the scheduled 15:28–15:30 IST reporting window opened.
   - **Ultra-Narrow In-Memory Window Without Fallback:** `main.py` gates the weekly report to `is_performance_report_window(now)` (`[15:28, 15:30) IST`). With a 60-second poll interval and ~15s API/network cycle latency, the engine only gets at most **one** loop iteration to satisfy the window.
   - **Missing Shutdown Fallback:** In commit `eab96ea` (TASK-20261030), the report trigger was moved from the session-end shutdown block to the 15:28 window, but the shutdown fallback was removed. If 15:28–15:30 is missed or the session terminates early/at 15:30, the report is permanently skipped.
   - **Silent Exception Swallowing:** `send_performance_report` catches exceptions, prints to local `stdout`, and returns `False` without routing failure notifications to `send_error_alert` / `DISCORD_HEALTH_WEBHOOK_URL`. Because Fly machines scale to zero on exit, container logs are discarded.

---

## 2. Evidence & Timeline Analysis

### Friday, 6 November 2026 Timeline (IST)
| Time (IST) | Event / Observed State | Source / Evidence |
|---|---|---|
| **09:52–11:56** | 4 trade signals generated and executed (`ares_signals` IDs 619–622) | Supabase `ares_signals`, `active_trades` |
| **12:48:42** | Last ML snapshot recorded (`ml_collection`) | Supabase `ml_collection` |
| **14:58:35** | Last spot quote persisted in `latest_quotes` (NIFTY 24878.55) | Supabase `latest_quotes` (`09:28:35 UTC`) |
| **14:58–15:30** | **No further records or ticks logged.** Engine halted / machine stopped before 15:28 window | Supabase audit across all tables |
| **15:28:00** | Weekly report window opened; engine was inactive / shut down | `main.py:1142` |
| **15:30:00** | Session gate close threshold | `main.py:1148` |

---

## 3. Weekly Data & Embed Audit (02 Nov – 06 Nov 2026)

Executing the query logic from `reports.py` on live data verifies that the week's trades and metrics are intact:

- **Cohort:** 9 trades closed between `2026-11-01T18:30:00Z` and `2026-11-06T09:58:00Z`.
- **Win Rate:** 11% (1 win / 8 losses).
- **Net Spot Points:** -107.4 points.
- **One-Lot Option Reference P&L:** -123.6 premium points / -₹8,034.
- **Coverage:** 9/9 trades (100% approximate marks).
- **Setups:**
  - Failed Breakout: 3 trades (Net -32.4 pts)
  - OI Wall Rejection: 0 trades
  - Exhaustion Reversal: 1 trade (Net -10.0 pts)
  - Trend Continuation: 5 trades (Net -65.0 pts)
- **Discord Webhook (`ARES Hook`):** Verified active and reachable (HTTP 200).

---

## 4. Architectural Weaknesses

1. **Tight Timing Window:** A 120-second window in a 75-second loop leaves zero fault tolerance for Dhan latency or clock skew.
2. **Missing Exit Drain Hook:** When `current_time >= time(15, 30)` triggers machine shutdown, there is no check for `if now.weekday() == 4 and not weekly_report_sent: await send_performance_report(...)`.
3. **No Standalone Workflow / Script:** The weekly ML report (`ml_training.yml`) runs on an independent GitHub Actions cron (Saturday 05:30 IST) and successfully delivered. The weekly trading performance report relies entirely on the live market loop container staying alive until 15:28.

---

## 5. Remediation Plan

1. **Add Shutdown Fallback in `main.py`:** If the session closes or drains at 15:30 and `not weekly_report_sent` (on Fridays), execute `send_performance_report` during final cleanup before `tick_feed.stop()`.
2. **Widen Trigger Window:** Expand `is_performance_report_window` to `[15:25, 15:30) IST`.
3. **Escalate Webhook Failures:** Update `send_performance_report` to call `send_error_alert` if delivery fails.
4. **Standalone Dispatch Script:** Provide `scripts/send_weekly_report.py` for manual delivery or a fallback GitHub Actions cron on Friday at 15:35 IST.
