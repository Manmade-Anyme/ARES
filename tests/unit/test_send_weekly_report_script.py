"""
Unit tests for scripts/send_weekly_report.py
"""

import asyncio
from datetime import datetime, date, time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import reports
from scripts import send_weekly_report


def test_parse_args_defaults():
    args = send_weekly_report.parse_args([])
    assert args.period == "weekly"
    assert args.target_date is None
    assert not args.dry_run
    assert not args.force
    assert not args.check_day


def test_parse_args_custom():
    args = send_weekly_report.parse_args([
        "--period", "monthly",
        "--date", "2026-11-13",
        "--dry-run",
        "--force",
    ])
    assert args.period == "monthly"
    assert args.target_date == "2026-11-13"
    assert args.dry_run is True
    assert args.force is True


def test_get_target_datetime_with_string():
    dt = send_weekly_report.get_target_datetime("2026-11-13")
    assert dt.date() == date(2026, 11, 13)
    assert dt.time() == time(23, 59, 59)
    assert dt.tzinfo == reports.IST


def test_is_eligible_day():
    friday = datetime(2026, 11, 13, 15, 30, tzinfo=reports.IST)
    saturday = datetime(2026, 11, 14, 15, 30, tzinfo=reports.IST)
    wednesday = datetime(2026, 11, 11, 15, 30, tzinfo=reports.IST)

    assert send_weekly_report.is_eligible_day(friday, "weekly") is True
    assert send_weekly_report.is_eligible_day(saturday, "weekly") is True
    assert send_weekly_report.is_eligible_day(wednesday, "weekly") is False


@pytest.mark.asyncio
async def test_dispatch_report_dry_run(monkeypatch):
    mock_supabase = MagicMock()
    mock_fetch = MagicMock(return_value=[])
    monkeypatch.setattr(send_weekly_report, "fetch_closed_trades", mock_fetch)

    dt = datetime(2026, 11, 13, 15, 30, tzinfo=reports.IST)
    success = await send_weekly_report.dispatch_report(
        supabase_client=mock_supabase,
        webhook_url="http://discord.invalid",
        health_webhook_url=None,
        now_ist=dt,
        period="weekly",
        dry_run=True,
    )
    assert success is True
    mock_fetch.assert_called_once()


@pytest.mark.asyncio
async def test_dispatch_report_success(monkeypatch):
    mock_supabase = MagicMock()
    monkeypatch.setattr(send_weekly_report, "fetch_closed_trades", MagicMock(return_value=[]))

    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()

    mock_client = AsyncMock()
    mock_client.post.return_value = mock_response

    mock_client_cls = MagicMock()
    mock_client_cls.return_value.__aenter__.return_value = mock_client
    mock_client_cls.return_value.__aexit__.return_value = None

    monkeypatch.setattr(send_weekly_report.httpx, "AsyncClient", mock_client_cls)

    dt = datetime(2026, 11, 13, 15, 30, tzinfo=reports.IST)
    success = await send_weekly_report.dispatch_report(
        supabase_client=mock_supabase,
        webhook_url="http://discord.invalid/webhook",
        health_webhook_url=None,
        now_ist=dt,
        period="weekly",
        dry_run=False,
    )
    assert success is True
    mock_client.post.assert_awaited_once()


@pytest.mark.asyncio
async def test_dispatch_report_failure_alerts_health_webhook(monkeypatch):
    mock_supabase = MagicMock()
    monkeypatch.setattr(send_weekly_report, "fetch_closed_trades", MagicMock(side_effect=RuntimeError("DB query failed")))

    mock_client = AsyncMock()
    mock_client_cls = MagicMock()
    mock_client_cls.return_value.__aenter__.return_value = mock_client
    mock_client_cls.return_value.__aexit__.return_value = None

    monkeypatch.setattr(send_weekly_report.httpx, "AsyncClient", mock_client_cls)

    dt = datetime(2026, 11, 13, 15, 30, tzinfo=reports.IST)
    success = await send_weekly_report.dispatch_report(
        supabase_client=mock_supabase,
        webhook_url="http://discord.invalid/webhook",
        health_webhook_url="http://discord.invalid/health",
        now_ist=dt,
        period="weekly",
        dry_run=False,
    )
    assert success is False
    mock_client.post.assert_awaited_once()
    assert "DB query failed" in str(mock_client.post.call_args) or "RuntimeError" in str(mock_client.post.call_args)


@pytest.mark.asyncio
async def test_main_cli_skips_on_ineligible_day(monkeypatch):
    # Wednesday with --check-day should cleanly return 0 without doing work
    exit_code = await send_weekly_report.main(["--period", "weekly", "--date", "2026-11-11", "--check-day"])
    assert exit_code == 0
