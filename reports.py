"""
Discord weekly / monthly performance reports for ARES.

Posts a dashboard-style embed summarizing how the system performed: trade count,
win rate, net spot points, and one-lot option-reference P&L when the pinned
contract has valid observed entry and final-exit prices. Spot `pnl_points` remain
the independent spot outcome. Missing option-price evidence stays unavailable.

Triggered in-process from main.py's session-end branch:
  - "weekly"  every Friday, eligible from 15:28 until 15:30 IST (exclusive)
  - "monthly" on the last trading day of the month, in the same window
"""

import httpx
import math
from datetime import datetime, timezone, timedelta, date, time
from typing import Any

from config import settings
from oi_watch_context import parse_candle_timestamp

IST = timezone(timedelta(hours=5, minutes=30))
REPORT_QUERY_PAGE_SIZE = 1000


def is_performance_report_window(now_ist: datetime) -> bool:
    """Return whether an IST time is in the final report window, [15:28, 15:30)."""
    if now_ist.tzinfo is None:
        now_ist = now_ist.replace(tzinfo=IST)
    current_time_ist = now_ist.astimezone(IST).time().replace(tzinfo=None)
    return time(15, 28) <= current_time_ist < time(15, 30)

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
    rows = []
    start = 0
    while True:
        response = (
            supabase.table("trade_analytics")
            .select("id, setup_type, direction, pnl_points, score, result_state, market_context, entry_timestamp, exit_timestamp, time_metrics_excluded")
            .gte("exit_timestamp", start_utc)
            .lte("exit_timestamp", end_utc)
            .eq("time_metrics_excluded", False)
            .order("exit_timestamp")
            .order("id")
            .range(start, start + REPORT_QUERY_PAGE_SIZE - 1)
            .execute()
        )
        page = getattr(response, "data", None) or []
        rows.extend(page)
        if len(page) < REPORT_QUERY_PAGE_SIZE:
            break
        start += REPORT_QUERY_PAGE_SIZE
    
    valid_rows = []
    for r in rows:
        if r.get("time_metrics_excluded") is True:
            continue
        if r.get("pnl_points") is None:
            continue
        entry = r.get("entry_timestamp")
        exit_ts = r.get("exit_timestamp")
        if entry and exit_ts and exit_ts < entry:
            continue
        valid_rows.append(r)
    return valid_rows


def _reference_price(leg: dict, event_at: Any) -> tuple[float, bool] | None:
    """Validate captured premium evidence against its source event."""
    if not isinstance(leg, dict):
        return None
    try:
        price = float(leg.get("price"))
        observed_at = parse_candle_timestamp(leg.get("observed_at"))
        event_at = parse_candle_timestamp(event_at)
    except (TypeError, ValueError, OverflowError, OSError):
        return None
    source = leg.get("source")
    approximate = leg.get("approximate")
    if (isinstance(leg.get("price"), bool) or not math.isfinite(price) or price <= 0
            or not isinstance(source, str) or not source.strip() or type(approximate) is not bool):
        return None
    if approximate:
        expected_close = event_at.replace(second=0, microsecond=0) + timedelta(minutes=1)
        if source != "completed_1m_close" or observed_at != expected_close:
            return None
    elif source != "dhan_last_trade" or abs((observed_at - event_at).total_seconds()) > 5:
        return None
    return price, approximate


def _option_reference_result(trade: dict) -> tuple[float, float, bool] | None:
    """Return observed exit-minus-entry premium points, rupees and approximation."""
    context = trade.get("market_context") or {}
    reference = context.get("option_reference") if isinstance(context, dict) else None
    if not isinstance(reference, dict) or reference.get("formula_version") != "one_lot_hold_to_exit_v1":
        return None
    if (reference.get("status") != "CLOSED" or type(reference.get("reference_lots")) is not int
            or reference.get("reference_lots") != 1):
        return None
    contract = reference.get("contract")
    if not isinstance(contract, dict):
        return None
    security_id = str(contract.get("security_id") or "")
    exchange = contract.get("exchange_segment")
    option_type = contract.get("option_type")
    try:
        expiry = datetime.strptime(str(contract.get("expiry_date") or ""), "%Y-%m-%d").date()
        strike = float(contract.get("strike"))
    except (TypeError, ValueError, OverflowError):
        return None
    if (not security_id.isdigit() or int(security_id) <= 0 or exchange != "NSE_FNO"
            or option_type not in {"CE", "PE"} or isinstance(contract.get("strike"), bool)
            or not math.isfinite(strike) or strike <= 0 or not strike.is_integer()):
        return None
    lot_size = contract.get("lot_size")
    if isinstance(lot_size, bool):
        return None
    try:
        lot_size = float(lot_size)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(lot_size) or lot_size <= 0 or not lot_size.is_integer():
        return None
    entry = _reference_price(reference.get("entry"), reference.get("entry_event_at"))
    exit_ = _reference_price(reference.get("exit"), reference.get("exit_event_at"))
    if entry is None or exit_ is None:
        return None
    try:
        entry_event = parse_candle_timestamp(reference.get("entry_event_at"))
        exit_event = parse_candle_timestamp(reference.get("exit_event_at"))
        entry_observed = parse_candle_timestamp(reference["entry"]["observed_at"])
        exit_observed = parse_candle_timestamp(reference["exit"]["observed_at"])
        row_exit = parse_candle_timestamp(trade.get("exit_timestamp"))
    except (KeyError, TypeError, ValueError, OverflowError, OSError):
        return None
    row_entry = trade.get("entry_timestamp")
    row_result = trade.get("result_state")
    try:
        row_entry = parse_candle_timestamp(row_entry) if row_entry is not None else entry_event
    except (TypeError, ValueError, OverflowError, OSError):
        return None
    if (entry_event > exit_event or entry_observed > exit_observed or exit_event != row_exit
            or entry_event.date() > expiry or exit_event.date() > expiry
            or entry_event != row_entry or row_result in (None, "OPEN")
            or reference.get("exit_reason") != row_result):
        return None
    raw_points = exit_[0] - entry[0]
    if not math.isfinite(raw_points):
        return None
    points = round(raw_points, 8)
    stored_points = reference.get("premium_points")
    if stored_points is not None:
        try:
            if isinstance(stored_points, bool) or round(float(stored_points), 8) != points:
                return None
        except (TypeError, ValueError, OverflowError):
            return None
    rupees = points * int(lot_size)
    if not math.isfinite(rupees):
        return None
    return points, rupees, entry[1] or exit_[1]


def _option_reference_metrics(trades: list[dict]) -> dict:
    """Aggregate only validated option marks; never turn missing evidence into zero."""
    exact_points, exact_rupees, exact_count = 0.0, 0.0, 0
    approximate_points, approximate_rupees, approximate_count = 0.0, 0.0, 0
    for trade in trades:
        result = _option_reference_result(trade)
        if result is None:
            continue
        points, rupees, approximate = result
        if approximate:
            approximate_points += points
            approximate_rupees += rupees
            approximate_count += 1
        else:
            exact_points += points
            exact_rupees += rupees
            exact_count += 1
    priced = exact_count + approximate_count
    eligible = len(trades)
    complete = eligible > 0 and priced == eligible
    return {
        "eligible_trades": eligible,
        "priced_trades": priced,
        "coverage_pct": priced / eligible * 100.0 if eligible else 0.0,
        "net_premium_points": round(exact_points + approximate_points, 8) if complete else None,
        "net_rupees": round(exact_rupees + approximate_rupees, 8) if complete else None,
        "exact": {"trades": exact_count, "premium_points": round(exact_points, 8),
                  "rupees": round(exact_rupees, 8)},
        "approximate": {"trades": approximate_count,
                        "premium_points": round(approximate_points, 8),
                        "rupees": round(approximate_rupees, 8)},
    }


def _bucket(trades: list[dict]) -> dict:
    """Aggregate one list of trades into a metrics dict."""
    n = len(trades)

    def _reached(trade, milestone):
        score = trade.get("score")
        if isinstance(score, (int, float)) and not isinstance(score, bool) and score >= milestone:
            return True
        context = trade.get("market_context") or {}
        if not isinstance(context, dict):
            return False
        reference = context.get("option_reference") or {}
        milestones = reference.get("milestones") if isinstance(reference, dict) else None
        marker = milestones.get(f"T{milestone}") if isinstance(milestones, dict) else None
        if isinstance(marker, dict):
            return True
        spot_milestones = context.get("target_milestones")
        if (milestone == 2 and isinstance(spot_milestones, dict)
                and isinstance(spot_milestones.get("t2"), dict)):
            return True
        trailing = context.get("option_trailing") or {}
        if isinstance(trailing, dict):
            highest = trailing.get("highest_milestone", 0)
            highest_reached = (isinstance(highest, (int, float)) and not isinstance(highest, bool)
                              and highest >= milestone)
            return (highest_reached
                    or bool(trailing.get("t1_at" if milestone == 1 else "t2_at")))
        return False

    if n == 0:
        return {
            "trades": 0, "wins": 0, "win_rate": 0.0,
            "net_points": 0.0, "avg_points": 0.0,
            "best": 0.0, "worst": 0.0,
            "t1_trades": 0, "t2_trades": 0,
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
        "t1_trades": sum(1 for t in trades if _reached(t, 1)),
        "t2_trades": sum(1 for t in trades if _reached(t, 2)),
    }


def compute_metrics(trades: list[dict]) -> dict:
    """Overall + per-setup aggregation. Pure — unit-tested in test_reports.py."""
    per_setup = {
        s: _bucket([t for t in trades if t.get("setup_type") == s])
        for s in SETUP_TYPES
    }
    overall = _bucket(trades)
    overall["option_reference"] = _option_reference_metrics(trades)
    return {"overall": overall, "per_setup": per_setup}


def _fmt_rupees(v: float) -> str:
    sign = "+" if v >= 0 else "-"
    return f"{sign}₹{abs(v):,.0f}"


def build_report_embed(period_label: str, date_range: str, metrics: dict) -> dict:
    """Build the Discord embed payload dict."""
    o = metrics["overall"]
    net = o["net_points"]
    reference = o["option_reference"]
    # Green up / red down / neutral grey.
    color = 3066993 if net > 0 else 15158332 if net < 0 else 9807270

    option_points = reference["net_premium_points"]
    option_rupees = reference["net_rupees"]
    option_totals = "Unavailable" if option_points is None else f"{option_points:+.1f} pts"
    rupee_total = "Unavailable" if option_rupees is None else _fmt_rupees(option_rupees)
    coverage = (f"{reference['priced_trades']}/{reference['eligible_trades']}"
                f" ({reference['coverage_pct']:.0f}%) · "
                f"Exact {reference['exact']['trades']} · Approximate {reference['approximate']['trades']} · "
                f"Missing {reference['eligible_trades'] - reference['priced_trades']}")
    exact = reference["exact"]
    approximate = reference["approximate"]
    complete_coverage = (reference["eligible_trades"] > 0
                         and reference["priced_trades"] == reference["eligible_trades"])
    if not complete_coverage and reference["priced_trades"] < reference["eligible_trades"]:
        exact_subtotal = approximate_subtotal = "unavailable (incomplete coverage)"
    else:
        exact_subtotal = (
            f"{exact['premium_points']:+.1f} pts / {_fmt_rupees(exact['rupees'])}"
            if complete_coverage and exact["trades"] else "unavailable (0 exact-price pairs)"
        )
        approximate_subtotal = (
            f"{approximate['premium_points']:+.1f} pts / {_fmt_rupees(approximate['rupees'])}"
            if complete_coverage and approximate["trades"]
            else "unavailable (0 approximate-price pairs)"
        )
    quality = (
        f"Exact subtotal: {exact_subtotal} · Approximate subtotal: {approximate_subtotal}"
    )
    summary = (
        f"📈 **Trades:** {o['trades']}   "
        f"🎯 **Win Rate:** {o['win_rate']:.0f}%\n"
        f"📊 **Net Spot Points:** {net:+.1f}   "
        f"🕒 **Avg/Trade:** {o['avg_points']:+.1f}\n"
        f"📊 **Net Option Premium Points:** {option_totals}\n"
        f"💰 **One-Lot Option P&L:** {rupee_total}\n"
        f"🗂️ **Option Price Coverage:** {coverage}\n"
        f"{quality}\n"
        f"🎯 **Spot Milestones:** T1 {o['t1_trades']} · T2 {o['t2_trades']}\n"
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
                f"Net {b['net_points']:+.1f} spot pts"
            )
        fields.append({"name": SETUP_LABELS[s], "value": value, "inline": True})

    return {
        "title": f"📊 ARES {period_label} PERFORMANCE",
        "description": f"🗓️ {date_range}",
        "color": color,
        "fields": fields,
        "footer": {"text": "Option figures use one reference lot from observed entry to final-exit premium; these are marks, not broker fills or realized account P&L. Exact and approximate subtotals are separated."},
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
