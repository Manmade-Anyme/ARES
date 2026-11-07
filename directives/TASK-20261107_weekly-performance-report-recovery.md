---
task_id: "TASK-20261107"
date: "2026-11-07"
status: "approved for implementation"
author: "Antigravity Dev Agent"
objective: "Restore guaranteed weekly and monthly performance report delivery on session shutdown and add delivery failure alerting."
---

# Directive: Weekly & Monthly Performance Report Delivery Recovery

## 1. Context and Problem

The weekly performance digest was previously moved into a 2-minute live session window ([15:28, 15:30) IST). On Friday, 6 November 2026, the trading container stopped quote logging at 14:58 IST, causing the 15:28 window to be completely bypassed. Furthermore, the session-end shutdown trigger originally present in earlier revisions was missing, causing the report to be skipped permanently without retry or alerting.

## 2. Objective and Scope

1. **Guaranteed Session-End Fallback:** Ensure that when a market session closes at 15:30 IST or scales to zero, if `now.weekday() == 4 and not weekly_report_sent` (or month-end and `not monthly_report_sent`), delivery is attempted before exit.
2. **Shutdown Cleanup Backstop:** Include the delivery check in `main.py`'s `finally:` cleanup block.
3. **Escalate Delivery Failures:** If `send_performance_report` raises an exception during database fetch or Discord webhook delivery, alert `DISCORD_HEALTH_WEBHOOK_URL` via `send_error_alert`.
4. **Ponytail Compliance:** No new tables, columns, CLI tools, or external services. Reuses existing `send_performance_report` and `send_error_alert` primitives.

## 3. Acceptance Criteria

- When in `is_performance_report_window(now)`, in-session delivery continues as before.
- When `current_time >= time(15, 30)` triggers session close, unsent weekly/monthly digests are delivered.
- When process exits via `finally:`, unsent digests for an active session are attempted.
- Unit tests verify unsent delivery on Friday and deduplication when already sent.
- Exceptions during report delivery trigger `send_error_alert`.
