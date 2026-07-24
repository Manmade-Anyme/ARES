"""Self-check for reports.compute_metrics and the options-rupee math."""
from datetime import date

from reports import compute_metrics, _option_rupees, is_last_trading_day_of_month
from config import settings


def _trade(setup, pnl, delta=None, lots=None):
    mc = {}
    if delta is not None and lots is not None:
        mc["options_sizing"] = {"delta": delta, "suggested_lots": lots}
    return {"setup_type": setup, "pnl_points": pnl, "market_context": mc}


def test_metrics():
    lot = settings.nifty_lot_size  # 65

    trades = [
        _trade("FAILED_BREAKOUT", 30.0, delta=0.50, lots=2),   # win, options
        _trade("FAILED_BREAKOUT", -10.0, delta=0.50, lots=2),  # loss, options
        _trade("OI_WALL_REJECTION", 20.0),                     # win, NO sizing -> 0 rupees
        _trade("EXHAUSTION_REVERSAL", 0.0, delta=0.5, lots=1), # breakeven, not a win
    ]

    m = compute_metrics(trades)
    o = m["overall"]

    assert o["trades"] == 4
    assert o["wins"] == 2                      # 30 and 20; 0.0 excluded
    assert o["win_rate"] == 50.0
    assert o["net_points"] == 40.0             # 30 -10 +20 +0
    assert o["best"] == 30.0 and o["worst"] == -10.0

    # options rupees: only the two FAILED_BREAKOUT trades contribute.
    #   30*0.5*lot*2  +  -10*0.5*lot*2  +  0(no sizing)  +  0*... = 20*lot
    assert o["option_rupees"] == 20.0 * lot

    fb = m["per_setup"]["FAILED_BREAKOUT"]
    assert fb["trades"] == 2 and fb["net_points"] == 20.0

    oi = m["per_setup"]["OI_WALL_REJECTION"]
    assert oi["trades"] == 1 and oi["option_rupees"] == 0.0

    # empty setup bucket is safe
    assert m["per_setup"]["TREND_CONTINUATION"]["trades"] == 0

    # empty overall is safe (no ZeroDivision)
    empty = compute_metrics([])["overall"]
    assert empty["trades"] == 0 and empty["win_rate"] == 0.0

    # single-trade option math sanity
    assert _option_rupees(_trade("X", 30.0, delta=0.5, lots=2)) == 30.0 * 0.5 * lot * 2


def test_last_trading_day():
    # 2026-07-31 is a Friday and the last weekday of July.
    assert is_last_trading_day_of_month(date(2026, 7, 31)) is True
    # 2026-07-30 (Thu) is not.
    assert is_last_trading_day_of_month(date(2026, 7, 30)) is False
    # 2026-05-29 is a Friday; 30/31 are Sat/Sun, so it's the last trading day of May.
    assert is_last_trading_day_of_month(date(2026, 5, 29)) is True


if __name__ == "__main__":
    test_metrics()
    test_last_trading_day()
    print("reports self-check passed.")
