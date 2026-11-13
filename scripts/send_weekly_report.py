#!/usr/bin/env python3
"""
scripts/send_weekly_report.py

Standalone dispatch CLI for ARES weekly and monthly performance reports.
Decouples batch reporting from the live market trading engine and Fly machine lifecycle.

Features:
- Can run locally, in CI/CD, or via scheduled GitHub Actions.
- Supports both "weekly" and "monthly" report periods.
- Supports custom target dates (--date YYYY-MM-DD).
- Supports --dry-run to inspect computed metrics and payload without sending.
- Supports --check-day for automated cron runs (only sends weekly on Friday/weekends,
  and monthly on the last trading day of the month).
- Automatic error escalation to DISCORD_HEALTH_WEBHOOK_URL on failure.
"""

import argparse
import asyncio
import os
import sys
from datetime import datetime, date, time
from pathlib import Path
from typing import Optional

# Ensure repository root is on sys.path for direct execution
repo_root = Path(__file__).resolve().parent.parent
if str(repo_root) not in sys.path:
    sys.path.insert(0, str(repo_root))

import httpx
from supabase import create_client

from config import settings
from reports import (
    IST,
    _window_bounds,
    fetch_closed_trades,
    compute_metrics,
    build_report_embed,
    is_last_trading_day_of_month,
)


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Dispatch ARES weekly or monthly performance report to Discord."
    )
    parser.add_argument(
        "--period",
        choices=["weekly", "monthly", "both"],
        default="weekly",
        help="Report period to generate: 'weekly', 'monthly', or 'both' (default: weekly).",
    )
    parser.add_argument(
        "--date",
        dest="target_date",
        default=None,
        help="Target date in YYYY-MM-DD format (defaults to current date in IST).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Compute metrics and print report embed JSON without posting to Discord.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Bypass schedule day guards and force report delivery.",
    )
    parser.add_argument(
        "--check-day",
        action="store_true",
        help="Enforce schedule guards: send weekly only on Friday/Sat/Sun, monthly only on last trading day.",
    )
    return parser.parse_args(argv)


def get_target_datetime(target_date_str: Optional[str]) -> datetime:
    """Return an IST-aware datetime for the target date or current time."""
    if target_date_str:
        d = date.fromisoformat(target_date_str)
        # End of day on target date (23:59:59 IST) so any post-15:30 drain trades are safely captured
        return datetime.combine(d, time(23, 59, 59), tzinfo=IST)
    return datetime.now(IST)


def is_eligible_day(now_ist: datetime, period: str) -> bool:
    """Check whether now_ist is an eligible day for the requested period under schedule guards."""
    target_date = now_ist.date()
    if period == "weekly":
        # Friday (4), Saturday (5), or Sunday (6)
        return now_ist.weekday() in (4, 5, 6)
    if period == "monthly":
        return is_last_trading_day_of_month(target_date)
    return False


async def dispatch_report(
    supabase_client,
    webhook_url: str,
    health_webhook_url: Optional[str],
    now_ist: datetime,
    period: str,
    dry_run: bool = False,
) -> bool:
    """Generate and deliver a performance report for the given period."""
    period_label = "WEEKLY" if period == "weekly" else "MONTHLY"
    print(f"[*] Generating {period_label} performance report for reference time {now_ist.isoformat()}...")

    try:
        start_utc, end_utc, date_range = _window_bounds(now_ist, period)
        print(f"[*] Query window (UTC): {start_utc} -> {end_utc} ({date_range})")

        trades = fetch_closed_trades(supabase_client, start_utc, end_utc)
        print(f"[*] Retrieved {len(trades)} closed trade(s) from Supabase.")

        metrics = compute_metrics(trades)
        embed = build_report_embed(period_label, date_range, metrics)
        payload = {"embeds": [embed]}

        if dry_run:
            import json
            print(f"[DRY-RUN] Embed payload for {period_label}:")
            print(json.dumps(payload, indent=2))
            return True

        if not webhook_url:
            raise ValueError("DISCORD_WEBHOOK_URL is not configured.")

        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.post(webhook_url, json=payload)
            response.raise_for_status()

        print(f"[+] Successfully posted {period_label} performance report to Discord ({metrics['overall']['trades']} trades).")
        return True

    except Exception as exc:
        exc_name = type(exc).__name__
        # Redact exc string to prevent leaking webhook tokens via httpx HTTPStatusError
        print(f"[-] {period_label} performance report delivery failed: {exc_name}")

        if health_webhook_url and not dry_run:
            try:
                alert_payload = {
                    "content": f"🚨 **ARES Performance Report Failure**\n`{period_label}` report delivery failed: `{exc_name}`\nTime: `{now_ist.isoformat()}`"
                }
                async with httpx.AsyncClient(timeout=10.0) as client:
                    await client.post(health_webhook_url, json=alert_payload)
            except Exception as alert_err:
                print(f"[-] Health webhook alert failed: {type(alert_err).__name__}")

        return False


async def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    now_ist = get_target_datetime(args.target_date)

    periods_to_run = []
    if args.period in ("weekly", "both"):
        if args.force or not args.check_day or is_eligible_day(now_ist, "weekly"):
            periods_to_run.append("weekly")
        else:
            print(f"[info] Skipping weekly report: {now_ist.strftime('%A')} is not Friday or weekend (use --force to override).")

    if args.period in ("monthly", "both"):
        if args.force or not args.check_day or is_eligible_day(now_ist, "monthly"):
            periods_to_run.append("monthly")
        else:
            print(f"[info] Skipping monthly report: {now_ist.date()} is not the last trading day of the month (use --force to override).")

    if not periods_to_run:
        print("[*] No reports eligible for delivery under current schedule guards. Exiting cleanly.")
        return 0

    supabase_url = settings.supabase_url or os.environ.get("SUPABASE_URL")
    supabase_key = (
        settings.supabase_service_role_key
        or settings.supabase_key
        or os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
        or os.environ.get("SUPABASE_KEY")
    )
    webhook_url = settings.discord_webhook_url or os.environ.get("DISCORD_WEBHOOK_URL", "")
    health_webhook_url = (
        settings.discord_health_webhook_url
        or os.environ.get("DISCORD_HEALTH_WEBHOOK_URL")
    )

    if not supabase_url or not supabase_key:
        print("[-] FATAL: Supabase URL or Key is missing. Check .env or environment variables.")
        return 1

    supabase_client = create_client(supabase_url, supabase_key)

    overall_success = True
    for period in periods_to_run:
        success = await dispatch_report(
            supabase_client=supabase_client,
            webhook_url=webhook_url,
            health_webhook_url=health_webhook_url,
            now_ist=now_ist,
            period=period,
            dry_run=args.dry_run,
        )
        if not success:
            overall_success = False

    return 0 if overall_success else 1


if __name__ == "__main__":
    exit_code = asyncio.run(main())
    sys.exit(exit_code)
