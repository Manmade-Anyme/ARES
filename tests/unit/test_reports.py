from datetime import datetime, timezone

import pytest

from reports import build_report_embed, compute_metrics, is_performance_report_window


@pytest.mark.parametrize(
    ("now_ist", "expected"),
    [
        (datetime(2026, 10, 30, 15, 27, 59), False),
        (datetime(2026, 10, 30, 15, 28), True),
        (datetime(2026, 10, 30, 15, 29, 59), True),
        (datetime(2026, 10, 30, 15, 30), False),
        (datetime(2026, 10, 30, 9, 58, tzinfo=timezone.utc), True),
    ],
)
def test_performance_report_window_starts_at_1528_ist_and_ends_before_1530(now_ist, expected):
    assert is_performance_report_window(now_ist) is expected


def _closed_trade(
    *,
    setup_type="FAILED_BREAKOUT",
    spot_points=12.0,
    entry_premium=100.0,
    exit_premium=115.0,
    contract_lot_size=65,
    approximate_entry=False,
    approximate_exit=False,
    milestones=None,
    include_reference=True,
):
    market_context = {
        "options_sizing": {
            "option_delta": 0.5,
            "suggested_lots": 9,
            "final_lots": 7,
            "lot_size": 77,
            "entry_limit": 101.0,
            "option_sl": 90.0,
            "option_target": 120.0,
        }
    }
    row = {
        "id": "trade-1",
        "setup_type": setup_type,
        "pnl_points": spot_points,
        "result_state": "SL_HIT",
        "exit_timestamp": "2026-10-30T04:10:00Z",
        "market_context": market_context,
    }
    if include_reference:
        premium_points = exit_premium - entry_premium
        market_context["option_reference"] = {
            "formula_version": "one_lot_hold_to_exit_v1",
            "status": "CLOSED",
            "reference_lots": 1,
            "contract": {
                "security_id": "51333",
                "exchange_segment": "NSE_FNO",
                "expiry_date": "2026-11-03",
                "strike": 22550,
                "option_type": "PE",
                "lot_size": contract_lot_size,
            },
            "entry_event_at": "2026-10-30T09:20:00+05:30",
            "entry": {
                "price": entry_premium,
                "observed_at": (
                    "2026-10-30T09:21:00+05:30" if approximate_entry
                    else "2026-10-30T09:20:01+05:30"
                ),
                "source": "completed_1m_close" if approximate_entry else "dhan_last_trade",
                "approximate": approximate_entry,
            },
            "milestones": milestones or {},
            "exit_event_at": "2026-10-30T09:40:00+05:30",
            "exit_reason": "SL_HIT",
            "exit": {
                "price": exit_premium,
                "observed_at": (
                    "2026-10-30T09:41:00+05:30" if approximate_exit
                    else "2026-10-30T09:40:01+05:30"
                ),
                "source": "completed_1m_close" if approximate_exit else "dhan_last_trade",
                "approximate": approximate_exit,
            },
            "premium_points": premium_points,
        }
        row["option_reference_pnl_points"] = premium_points
    return row


def test_compute_metrics_calculates_full_one_lot_option_move_from_captured_contract():
    metrics = compute_metrics([_closed_trade()])
    overall = metrics["overall"]
    reference = overall["option_reference"]

    assert overall["net_points"] == 12.0
    assert reference["net_premium_points"] == 15.0
    assert reference["net_rupees"] == 975.0
    assert reference["priced_trades"] == reference["eligible_trades"] == 1
    assert reference["coverage_pct"] == 100.0
    assert reference["exact"] == {"trades": 1, "premium_points": 15.0, "rupees": 975.0}
    # The contract lot-size snapshot (65) applies; planned lots and sizing lot size do not.


def test_compute_metrics_keeps_exact_and_approximate_subtotals_labeled():
    approximate = _closed_trade(
        setup_type="TREND_CONTINUATION",
        entry_premium=80.0,
        exit_premium=72.0,
        contract_lot_size=50,
        approximate_exit=True,
    )
    metrics = compute_metrics([_closed_trade(), approximate])
    reference = metrics["overall"]["option_reference"]

    assert reference["net_premium_points"] == 7.0
    assert reference["net_rupees"] == 575.0
    assert reference["exact"] == {"trades": 1, "premium_points": 15.0, "rupees": 975.0}
    assert reference["approximate"] == {"trades": 1, "premium_points": -8.0, "rupees": -400.0}
    assert reference["priced_trades"] == reference["eligible_trades"] == 2

    embed = build_report_embed("WEEKLY", "26 Oct – 30 Oct 2026", metrics)
    overall = next(field["value"] for field in embed["fields"] if field["name"] == "📋 Overall")
    assert "Exact" in overall
    assert "Approximate" in overall
    assert "Exact subtotal: +15.0 pts / +₹975" in overall
    assert "Approximate subtotal: -8.0 pts / -₹400" in overall


def test_compute_metrics_marks_period_option_totals_unavailable_when_any_trade_lacks_prices():
    metrics = compute_metrics([_closed_trade(), _closed_trade(include_reference=False)])
    reference = metrics["overall"]["option_reference"]

    assert reference["priced_trades"] == 1
    assert reference["eligible_trades"] == 2
    assert reference["coverage_pct"] == 50.0
    assert reference["net_premium_points"] is None
    assert reference["net_rupees"] is None

    embed = build_report_embed("WEEKLY", "26 Oct – 30 Oct 2026", metrics)
    overall = next(field["value"] for field in embed["fields"] if field["name"] == "📋 Overall")
    assert "Unavailable" in overall
    assert "Exact subtotal: unavailable (incomplete coverage)" in overall
    assert "Approximate subtotal: unavailable (incomplete coverage)" in overall
    assert "15.0 pts / +₹975" not in overall
    assert "1/2" in overall
    assert "50%" in overall


def test_compute_metrics_reports_zero_priced_and_unavailable_when_no_option_prices_exist():
    metrics = compute_metrics([_closed_trade(include_reference=False)])
    reference = metrics["overall"]["option_reference"]

    assert reference["priced_trades"] == 0
    assert reference["eligible_trades"] == 1
    assert reference["coverage_pct"] == 0.0
    assert reference["net_premium_points"] is None
    assert reference["net_rupees"] is None

    embed = build_report_embed("WEEKLY", "26 Oct – 30 Oct 2026", metrics)
    overall = next(field["value"] for field in embed["fields"] if field["name"] == "📋 Overall")
    assert "Unavailable" in overall
    assert "0/1" in overall
    assert "0%" in overall


def test_spot_t1_t2_milestones_do_not_change_hold_to_final_exit_option_pnl():
    without_milestones = compute_metrics([_closed_trade()])["overall"]["option_reference"]
    with_milestones = compute_metrics(
        [
            _closed_trade(
                milestones={
                    "T1": {
                        "event_at": "2026-10-30T09:30:00+05:30",
                        "spot_target": 22870.0,
                        "spot_price": 22871.2,
                        "source": "completed_candle_touch",
                    },
                    "T2": {
                        "event_at": "2026-10-30T09:35:00+05:30",
                        "spot_target": 22900.0,
                        "spot_price": 22901.0,
                        "source": "completed_candle_touch",
                    },
                }
            )
        ]
    )["overall"]["option_reference"]

    assert with_milestones["net_premium_points"] == without_milestones["net_premium_points"] == 15.0
    assert with_milestones["net_rupees"] == without_milestones["net_rupees"] == 975.0


def test_report_embed_shows_spot_and_option_points_without_old_or_setup_rupee_estimates():
    metrics = compute_metrics([_closed_trade()])
    embed = build_report_embed("WEEKLY", "26 Oct – 30 Oct 2026", metrics)
    all_text = "\n".join(
        [embed["title"], embed["description"]]
        + [field["name"] + "\n" + field["value"] for field in embed["fields"]]
        + [embed["footer"]["text"]]
    )

    assert "Net Spot Points" in all_text
    assert "Net Option Premium Points" in all_text
    assert "One-Lot Option P&L" in all_text
    assert "Est. Spot movement P&L" not in all_text
    assert "Est. Bracket 1-Lot P&L" not in all_text
    assert "delta-based estimate" not in all_text
    setup_value = next(field["value"] for field in embed["fields"] if field["name"] == "Failed Breakout")
    assert "Net +12.0 spot pts" in setup_value
    assert "₹" not in setup_value
