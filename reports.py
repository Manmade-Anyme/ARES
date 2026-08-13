"""
Discord weekly / monthly performance reports for ARES.

Posts a dashboard-style embed summarizing how the system performed: trade count,
win rate, net spot points, and an estimated options rupee P&L, both overall and
broken down per setup type. Reads closed trades from the `trade_analytics` table
(see storage.py) — spot `pnl_points` are recorded there; options P&L is *derived*
from the delta/lots stored in `market_context.options_sizing`, so it is an estimate.

Triggered in-process from main.py's session-end branch:
  - "weekly"  every Friday
  - "monthly" on the last trading day of the month
"""

import httpx
from datetime import datetime, timezone, timedelta, date
from typing import Any

from config import settings

IST = timezone(timedelta(hours=5, minutes=30))

# Setups reported on (models.SetupType), in display order.
SETUP_TYPES = [
    "FAILED_BREAKOUT",
    "OI_WALL_REJECTION",
    "EXHAUSTION_REVERSAL",
    "TREND_CONTINUATION",
]
SETUP_LABELS = {
    "FAILED_BREAKOUT": "Failed Breakout",
    "OI_WALL_REJECTION": "OI Wall Rejection",
    "EXHAUSTION_REVERSAL": "Exhaustion Reversal",
    "TREND_CONTINUATION": "Trend Continuation",
}


def is_last_trading_day_of_month(d: date) -> bool:
    """
    True if `d` is the last Mon-Fri weekday of its month.

    ponytail: ignores exchange holidays — a holiday on the true last weekday can
    shift the real last trading day. Acceptable ceiling; the machine runs Mon-Fri
    via cron regardless, so the worst case is the monthly report firing on a day
    the market was shut (which simply has no trades to add).
    """
    nxt = d + timedelta(days=1)
    while nxt.weekday() >= 5:  # skip Sat(5)/Sun(6)
        nxt += timedelta(days=1)
    return nxt.month != d.month


def _window_bounds(now_ist: datetime, period: str) -> tuple[str, str, str]:
    """
    Compute the [start, end] query window (UTC ISO strings) and a human date-range
    label for a report period.

    weekly  -> Monday 00:00 IST of the current week .. now
    monthly -> 1st    00:00 IST of the current month .. now

    A naive datetime is assumed IST (main.py's clock runs on the container's
    Asia/Kolkata TZ), so conversion to UTC is correct regardless of host TZ.
    """
    if now_ist.tzinfo is None:
        now_ist = now_ist.replace(tzinfo=IST)

    if period == "weekly":
        start_ist = (now_ist - timedelta(days=now_ist.weekday())).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
    elif period == "monthly":
        start_ist = now_ist.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    else:
        raise ValueError(f"Unknown period: {period}")

    start_utc = start_ist.astimezone(timezone.utc).isoformat()
    end_utc = now_ist.astimezone(timezone.utc).isoformat()
    date_range = f"{start_ist.strftime('%d %b')} – {now_ist.strftime('%d %b %Y')}"
    return start_utc, end_utc, date_range


def fetch_closed_trades(supabase: Any, start_utc: str, end_utc: str) -> list[dict]:
    """
    Fetch trades that CLOSED within [start_utc, end_utc]. Open trades (pnl_points
    NULL) are excluded — they ride multi-day and get counted the week they close.
    """
    response = (
        supabase.table("trade_analytics")
        .select("setup_type, direction, pnl_points, result_state, market_context")
        .gte("exit_timestamp", start_utc)
        .lte("exit_timestamp", end_utc)
        .execute()
    )
    rows = getattr(response, "data", None) or []
    return [r for r in rows if r.get("pnl_points") is not None]


def _option_rupees(trade: dict) -> float:
    """
    Estimated options rupee P&L for one trade, from stored sizing:
        spot_points × |delta| × lot_size × suggested_lots

    ponytail: linear-delta model (same as options_math.translate_to_premium),
    ignores premium floor / theta / convexity. Trades without options_sizing
    contribute 0. Upgrade to real option fills once those are recorded.
    """
    sizing = (trade.get("market_context") or {}).get("options_sizing")
    if not sizing:
        return 0.0
    delta = sizing.get("delta")
    lots = sizing.get("suggested_lots")
    if delta is None or not lots:
        return 0.0
    pnl_points = float(trade["pnl_points"])
    lot_size = int(settings.nifty_lot_size)
    return pnl_points * abs(float(delta)) * lot_size * int(lots)


def _bucket(trades: list[dict]) -> dict:
    """Aggregate one list of trades into a metrics dict."""
    n = len(trades)
    if n == 0:
        return {
            "trades": 0, "wins": 0, "win_rate": 0.0,
            "net_points": 0.0, "avg_points": 0.0,
            "best": 0.0, "worst": 0.0, "option_rupees": 0.0,
        }
    points = [float(t["pnl_points"]) for t in trades]
    wins = sum(1 for p in points if p > 0)
    net = sum(points)
    return {
        "trades": n,
        "wins": wins,
        "win_rate": wins / n * 100.0,
        "net_points": net,
        "avg_points": net / n,
        "best": max(points),
        "worst": min(points),
        "option_rupees": sum(_option_rupees(t) for t in trades),
    }


def compute_metrics(trades: list[dict]) -> dict:
    """Overall + per-setup aggregation. Pure — unit-tested in test_reports.py."""
    per_setup = {
        s: _bucket([t for t in trades if t.get("setup_type") == s])
        for s in SETUP_TYPES
    }
    return {"overall": _bucket(trades), "per_setup": per_setup}


def _fmt_rupees(v: float) -> str:
    sign = "+" if v >= 0 else "-"
    return f"{sign}₹{abs(v):,.0f}"


def build_report_embed(period_label: str, date_range: str, metrics: dict) -> dict:
    """Build the Discord embed payload dict."""
    o = metrics["overall"]
    net = o["net_points"]
    # Green up / red down / neutral grey.
    color = 3066993 if net > 0 else 15158332 if net < 0 else 9807270

    summary = (
        f"📈 **Trades:** {o['trades']}   "
        f"🎯 **Win Rate:** {o['win_rate']:.0f}%\n"
        f"📊 **Net Spot Points:** {net:+.1f}   "
        f"🕒 **Avg/Trade:** {o['avg_points']:+.1f}\n"
        f"💰 **Est. Spot movement P&L:** {_fmt_rupees(o['option_rupees'])}\n"
        f"🟢 **Best:** {o['best']:+.1f}   🔴 **Worst:** {o['worst']:+.1f}"
    )
    fields = [{"name": "📋 Overall", "value": summary, "inline": False}]

    for s in SETUP_TYPES:
        b = metrics["per_setup"][s]
        if b["trades"] == 0:
            value = "_no trades_"
        else:
            value = (
                f"{b['trades']} trades · {b['win_rate']:.0f}% win\n"
                f"Net {b['net_points']:+.1f} pts · {_fmt_rupees(b['option_rupees'])}"
            )
        fields.append({"name": SETUP_LABELS[s], "value": value, "inline": True})

    return {
        "title": f"📊 ARES {period_label} PERFORMANCE",
        "description": f"🗓️ {date_range}",
        "color": color,
        "fields": fields,
        "footer": {"text": "Spot movement P&L is a delta-based estimate (spot pts × |Δ| × lots × lot size)."},
    }


async def send_performance_report(supabase: Any, now_ist: datetime, period: str) -> bool:
    """
    Build and post a weekly/monthly performance report to the main Discord webhook.
    Guarded end-to-end so a failure never blocks session shutdown.

    Returns True only on successful delivery, so the caller can leave a failed
    report eligible for retry instead of marking it done.
    """
    webhook_url = settings.discord_webhook_url
    if not webhook_url:
        return False

    period_label = "WEEKLY" if period == "weekly" else "MONTHLY"
    try:
        start_utc, end_utc, date_range = _window_bounds(now_ist, period)
        trades = fetch_closed_trades(supabase, start_utc, end_utc)
        metrics = compute_metrics(trades)
        payload = {"embeds": [build_report_embed(period_label, date_range, metrics)]}

        async with httpx.AsyncClient() as client:
            response = await client.post(webhook_url, json=payload)
            response.raise_for_status()
        print(f"[+] {period_label} performance report sent ({metrics['overall']['trades']} trades).")
        return True
    except Exception as e:
        # Log only the exception type — the message can embed the webhook URL
        # (and its secret token) via httpx's raise_for_status().
        print(f"[-] {period_label} performance report failed: {type(e).__name__}")
        return False
