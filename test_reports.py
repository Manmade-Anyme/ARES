"""Self-check for reports.compute_metrics and the options-rupee math."""
from datetime import date

from reports import compute_metrics, _option_rupees, _option_1_lot_rupees, is_last_trading_day_of_month
from config import settings


def _trade(setup, pnl, delta=None, lots=None, opt_entry=None, opt_sl=None, opt_target=None):
    mc = {}
    if delta is not None and lots is not None:
        sizing = {"delta": delta, "suggested_lots": lots}
        if opt_entry is not None:
            sizing["premium"] = opt_entry
            sizing["option_sl"] = opt_sl
            sizing["option_target"] = opt_target
        mc["options_sizing"] = sizing
    return {"setup_type": setup, "pnl_points": pnl, "market_context": mc}


def test_metrics():
    lot = settings.nifty_lot_size  # 65

    trades = [
        _trade("FAILED_BREAKOUT", 30.0, delta=0.50, lots=2, opt_entry=100.0, opt_sl=90.0, opt_target=120.0),   # win, options
        _trade("FAILED_BREAKOUT", -10.0, delta=0.50, lots=2, opt_entry=100.0, opt_sl=90.0, opt_target=120.0),  # loss, options
        _trade("OI_WALL_REJECTION", 20.0),                     # win, NO sizing -> 0 rupees
        _trade("EXHAUSTION_REVERSAL", 0.0, delta=0.5, lots=1, opt_entry=100.0, opt_sl=90.0, opt_target=120.0), # breakeven, not a win
        _trade("EXHAUSTION_REVERSAL", 10.0, delta=0.5, lots=1), # missing premium -> 0 1-lot rupees
    ]

    m = compute_metrics(trades)
    o = m["overall"]

    assert o["trades"] == 5
    assert o["wins"] == 3                      # 30, 20 and 10; 0.0 excluded
    assert o["win_rate"] == 60.0
    assert o["net_points"] == 50.0             # 30 -10 +20 +0 +10
    assert o["best"] == 30.0 and o["worst"] == -10.0

    # options rupees: FAILED_BREAKOUT trades and EXHAUSTION_REVERSAL trades with sizing contribute.
    #   30*0.5*lot*2  +  -10*0.5*lot*2  +  0(no sizing)  +  0 + 10*0.5*lot*1
    assert o["option_rupees"] == 25.0 * lot

    # 1-lot options rupees: FAILED_BREAKOUT trades contribute. EXHAUSTION_REVERSAL breakeven is 0. EXHAUSTION missing is 0.
    # Win (120 - 100)*65 = 20*65. Loss (90 - 100)*65 = -10*65. Total = 10*65.
    assert o["option_1_lot_rupees"] == 10.0 * lot

    fb = m["per_setup"]["FAILED_BREAKOUT"]
    assert fb["trades"] == 2 and fb["net_points"] == 20.0
    assert fb["option_1_lot_rupees"] == 10.0 * lot

    oi = m["per_setup"]["OI_WALL_REJECTION"]
    assert oi["trades"] == 1 and oi["option_rupees"] == 0.0

    # empty setup bucket is safe
    assert m["per_setup"]["TREND_CONTINUATION"]["trades"] == 0

    # empty overall is safe (no ZeroDivision)
    empty = compute_metrics([])["overall"]
    assert empty["trades"] == 0 and empty["win_rate"] == 0.0

    # single-trade option math sanity
    assert _option_rupees(_trade("X", 30.0, delta=0.5, lots=2)) == 30.0 * 0.5 * lot * 2

    # 1-lot math sanity
    t_win = _trade("X", 30.0, delta=0.5, lots=2, opt_entry=100.0, opt_sl=90.0, opt_target=120.0)
    t_loss = _trade("X", -10.0, delta=0.5, lots=2, opt_entry=100.0, opt_sl=90.0, opt_target=120.0)
    t_be = _trade("X", 0.0, delta=0.5, lots=2, opt_entry=100.0, opt_sl=90.0, opt_target=120.0)
    t_miss = _trade("X", 30.0, delta=0.5, lots=2)
    assert _option_1_lot_rupees(t_win) == (120.0 - 100.0) * lot
    assert _option_1_lot_rupees(t_loss) == (90.0 - 100.0) * lot
    assert _option_1_lot_rupees(t_be) == 0.0
    assert _option_1_lot_rupees(t_miss) == 0.0


def test_last_trading_day():
    # 2026-07-31 is a Friday and the last weekday of July.
    assert is_last_trading_day_of_month(date(2026, 7, 31)) is True
    # 2026-07-30 (Thu) is not.
    assert is_last_trading_day_of_month(date(2026, 7, 30)) is False
    # 2026-05-29 is a Friday; 30/31 are Sat/Sun, so it's the last trading day of May.
    assert is_last_trading_day_of_month(date(2026, 5, 29)) is True


def test_stopped_out_at_be_is_a_t1_win_and_credits_option_target():
    trade = _trade(
        "OI_WALL_REJECTION",
        50.0,
        delta=0.5,
        lots=1,
        opt_entry=100.0,
        opt_sl=90.0,
        opt_target=125.0,
    )
    trade["result_state"] = "STOPPED_OUT_AT_BE"

    metrics = compute_metrics([trade])["overall"]

    assert metrics["wins"] == 1
    assert metrics["win_rate"] == 100.0
    assert metrics["option_1_lot_rupees"] == (125.0 - 100.0) * settings.nifty_lot_size


if __name__ == "__main__":
    test_metrics()
    test_last_trading_day()
    print("reports self-check passed.")
