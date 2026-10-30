"""Self-check for report aggregation and observed option-reference P&L."""
from datetime import date

from reports import compute_metrics, is_last_trading_day_of_month


def _trade(setup, spot_points, entry_premium=None, exit_premium=None, lot_size=65):
    entry_at = "2026-10-30T09:20:00+05:30"
    exit_at = "2026-10-30T09:40:00+05:30"
    context = {}
    row = {
        "setup_type": setup,
        "pnl_points": spot_points,
        "result_state": "SL_HIT",
        "entry_timestamp": entry_at,
        "exit_timestamp": exit_at,
        "market_context": context,
    }
    if entry_premium is not None and exit_premium is not None:
        context["option_reference"] = {
            "formula_version": "one_lot_hold_to_exit_v1",
            "status": "CLOSED",
            "reference_lots": 1,
            "contract": {
                "security_id": "51333", "exchange_segment": "NSE_FNO",
                "expiry_date": "2026-11-03", "strike": 22550,
                "option_type": "PE", "lot_size": lot_size,
            },
            "entry_event_at": entry_at,
            "entry": {"price": entry_premium, "observed_at": "2026-10-30T09:20:01+05:30",
                      "source": "dhan_last_trade", "approximate": False},
            "exit_event_at": exit_at,
            "exit_reason": "SL_HIT",
            "exit": {"price": exit_premium, "observed_at": "2026-10-30T09:40:01+05:30",
                     "source": "dhan_last_trade", "approximate": False},
            "premium_points": round(exit_premium - entry_premium, 8),
        }
    return row


def test_metrics():
    trades = [
        _trade("FAILED_BREAKOUT", 30.0, 100.0, 120.0),
        _trade("FAILED_BREAKOUT", -10.0, 100.0, 90.0),
        _trade("OI_WALL_REJECTION", 20.0),
        _trade("EXHAUSTION_REVERSAL", 0.0, 100.0, 100.0),
        _trade("EXHAUSTION_REVERSAL", 10.0),
    ]
    metrics = compute_metrics(trades)
    overall = metrics["overall"]

    assert overall["trades"] == 5
    assert overall["wins"] == 3
    assert overall["win_rate"] == 60.0
    assert overall["net_points"] == 50.0
    assert overall["best"] == 30.0 and overall["worst"] == -10.0
    reference = overall["option_reference"]
    assert reference["priced_trades"] == 3
    assert reference["eligible_trades"] == 5
    assert reference["net_premium_points"] is None
    assert reference["net_rupees"] is None
    assert reference["exact"] == {"trades": 3, "premium_points": 10.0, "rupees": 650.0}

    fb = metrics["per_setup"]["FAILED_BREAKOUT"]
    assert fb["trades"] == 2 and fb["net_points"] == 20.0
    oi = metrics["per_setup"]["OI_WALL_REJECTION"]
    assert oi["trades"] == 1 and "option_reference" not in oi
    assert metrics["per_setup"]["TREND_CONTINUATION"]["trades"] == 0
    empty = compute_metrics([])["overall"]
    assert empty["trades"] == 0 and empty["win_rate"] == 0.0
    assert empty["option_reference"]["net_rupees"] is None


def test_last_trading_day():
    assert is_last_trading_day_of_month(date(2026, 7, 31)) is True
    assert is_last_trading_day_of_month(date(2026, 7, 30)) is False
    assert is_last_trading_day_of_month(date(2026, 5, 29)) is True


def test_stopped_out_at_be_keeps_spot_outcome_separate_from_option_reference():
    trade = _trade("OI_WALL_REJECTION", 50.0, 100.0, 125.0)
    trade["result_state"] = "STOPPED_OUT_AT_BE"
    trade["market_context"]["option_reference"]["exit_reason"] = "STOPPED_OUT_AT_BE"
    metrics = compute_metrics([trade])["overall"]

    assert metrics["wins"] == 1
    assert metrics["win_rate"] == 100.0
    assert metrics["net_points"] == 50.0
    assert metrics["option_reference"]["net_premium_points"] == 25.0
    assert metrics["option_reference"]["net_rupees"] == 25.0 * 65


if __name__ == "__main__":
    test_metrics()
    test_last_trading_day()
    test_stopped_out_at_be_keeps_spot_outcome_separate_from_option_reference()
    print("reports self-check passed.")
