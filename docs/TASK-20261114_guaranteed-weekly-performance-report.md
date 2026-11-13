# TASK-20261114: Guaranteed Weekly & Monthly Performance Report Dispatch

**Date:** 2026-11-14  
**Scope:** Performance reporting architecture, GitHub Actions automation, Fly machine lifecycle  
**Status:** Completed & Delivered  
**Branch:** `feature/TASK-20261114-guaranteed-weekly-performance-report`  

---

## 1. Problem Context

For three consecutive weeks, weekly performance reports failed to deliver on Friday:
- **Week 1 (26–30 Oct 2026):** Missed because the 15:29 live window was bypassed when candle processing completed after 15:30. Fixed in `eab96ea` by shifting window to `[15:28, 15:30)`.
- **Week 2 (02–06 Nov 2026):** Process halted quote logging at 14:58:35 due to provider connectivity drops, never entering the 15:28 window. PR #159 added an in-process fallback in `main.py`'s `finally:` shutdown handler.
- **Week 3 (09–13 Nov 2026):** On Friday 13 Nov, the live market loop stalled at ~14:48–15:08 IST. When Fly issued `SIGINT`/`SIGTERM` to scale down the machine at 15:30:23 IST, Fly's default 5-second kill timeout (`SIGKILL`) terminated the container while shutdown drains were executing, completely cutting off the in-process report fallback before it could deliver.

### Core Architectural Flaw
Coupling batch analytics reporting to the live 1-minute trading loop within an intraday container that scales to zero at 15:30 IST is fundamentally fragile. If the engine experiences broker latency, network drops, or early container shutdown, report delivery is permanently lost.

---

## 2. Implemented Architecture

### 2.1 Dedicated In-Session Background Scheduler (`_run_in_session_report_scheduler`)
- Operates concurrently within `main.py` while the Fly machine is activated by the 09:07–15:30 IST cron job.
- Triggers at exactly 15:28:00 IST in the middle of the active session, completely decoupled from candle polling intervals, option chain REST requests, or ML snapshot latency.
- Bounded to `[15:28, 15:30)` IST. If an initial delivery attempt fails due to a transient network blip, automatically retries once after 5s before session close.
- Cleanly cancels during session scale-down or termination without blocking shutdown handlers.

### 2.2 Network Timeout Guard in `main.py` (`_fetch_candle_with_protection`)
- Replaced unbounded `await request` with `await asyncio.wait_for(request, timeout=20)`.
- Prevents dropped Dhan API connections from hanging the single-threaded asyncio event loop for 42 minutes (the root cause of the 13 Nov stall at 14:48 IST).

### 2.3 Fly Machine Lifecycle Hardening (`fly.toml`)
- Configured `kill_timeout = '30s'` under root configuration.
- Fly now grants up to 30 seconds for graceful shutdown drains before issuing `SIGKILL`, allowing `main.py` to complete trade flushes and shutdown fallback hooks safely.

### 2.4 Standalone Dispatch CLI (`scripts/send_weekly_report.py`)
- Direct Python CLI with zero extra dependencies beyond existing repository packages (`reports.py`, `supabase`, `httpx`).
- Supports `--dry-run` to calculate and inspect metrics without Discord posting.
- Supports `--force` to bypass schedule guards.
- Automatically routes unhandled exceptions to `DISCORD_HEALTH_WEBHOOK_URL` (redacting secrets).

---

## 3. Immediate Delivery Verification

The missing weekly report for `09 Nov – 13 Nov 2026` was immediately generated and delivered to Discord via `scripts/send_weekly_report.py`:
- **Period:** 09 Nov – 13 Nov 2026
- **Cohort:** 3 closed trades (`#631`, `#632`, `#633` / Trend Continuation)
- **Win Rate:** 33.3% (1 win / 2 losses)
- **Net Spot Points:** -15.0 pts
- **One-Lot Option Reference P&L:** -₹1,800.50 (-27.7 premium pts)
- **Option Price Coverage:** 3/3 (100% approximate marks)
- **Discord Delivery Status:** HTTP 200 OK
