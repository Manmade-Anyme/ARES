"""Regression cases for directional OI wall retest rejection (MANM-158)."""

from datetime import datetime, timedelta

from detectors.oi_wall_entry import OIWallEntryFilter
from models import Direction, OHLCVCandle, OIWallBias


def wall_bias(strike, side, timestamp, persistence):
    return OIWallBias(
        wall_key=f"{side}:{int(strike)}",
        wall_strike=strike,
        wall_option_type=side,
        direction=Direction.BULLISH if side == "PE" else Direction.BEARISH,
        trade_option_type="CE" if side == "PE" else "PE",
        wall_oi=6_299_085,
        wall_oi_change_pct=9.94,
        relative_percentile=90.0,
        first_seen=timestamp,
        last_seen=timestamp,
        persistence_snapshots=persistence,
        persistence_duration_seconds=0.0,
        state="TRACKING",
        initial_interaction_timestamp=None,
        initial_interaction_price=None,
        favourable_excursion_pts=0.0,
        reasons=("OI wall retest",),
    )


def update(entry_filter, side, strike, timestamp, persistence, prices):
    candle = OHLCVCandle(timestamp=timestamp, open=prices[0], high=prices[1], low=prices[2], close=prices[3], volume=1000)
    return entry_filter.update(wall_bias(strike, side, timestamp, persistence), candle, [])


def test_september_28_falling_pe_retest_waits_then_wall_breach_expires():
    entry_filter = OIWallEntryFilter()
    t0 = datetime(2026, 9, 28, 9, 16)
    strike = 22950.0
    candles = [
        (22974.85, 22974.85, 22946.20, 22957.35),
        (22966.65, 22970.80, 22963.95, 22967.95),
        (22970.40, 22977.45, 22966.00, 22966.20),
    ]
    for minute, prices in enumerate(candles):
        update(entry_filter, "PE", strike, t0 + timedelta(minutes=minute), minute + 1, prices)
    assert entry_filter.state == "RETEST_READY"

    falling = update(entry_filter, "PE", strike, t0 + timedelta(minutes=3), 4,
                     (22966.85, 22967.85, 22953.05, 22954.10))
    assert (falling.status, falling.telemetry.filter_state, falling.retest_timestamp) == (
        "WAITING", "RETEST_READY", None,
    )
    assert entry_filter.retest_timestamp is None

    breached = update(entry_filter, "PE", strike, t0 + timedelta(minutes=4), 5,
                      (22951.70, 22951.80, 22938.20, 22938.80))
    assert (breached.status, breached.telemetry.filter_state) == ("EXPIRED", "EXPIRED")


def test_pe_retest_can_qualify_on_later_bullish_candle():
    entry_filter = OIWallEntryFilter()
    t0 = datetime(2026, 9, 28, 9, 17)
    strike = 22950.0
    update(entry_filter, "PE", strike, t0, 2, (22966.65, 22970.80, 22963.95, 22967.95))
    update(entry_filter, "PE", strike, t0 + timedelta(minutes=1), 3,
           (22970.40, 22977.45, 22966.00, 22966.20))
    contrary = update(entry_filter, "PE", strike, t0 + timedelta(minutes=2), 4,
                      (22966.85, 22967.85, 22953.05, 22954.10))
    assert (contrary.status, entry_filter.state) == ("WAITING", "RETEST_READY")

    valid = update(entry_filter, "PE", strike, t0 + timedelta(minutes=3), 5,
                   (22954.00, 22962.00, 22951.00, 22960.00))
    assert (valid.status, valid.trigger_price, valid.retest_timestamp) == (
        "QUALIFIED", 22960.00, t0 + timedelta(minutes=3),
    )


def test_ce_retest_needs_bearish_candle_and_retains_ready_state():
    entry_filter = OIWallEntryFilter()
    t0 = datetime(2026, 9, 28, 9, 17)
    strike = 24100.0
    update(entry_filter, "CE", strike, t0, 2, (24085.0, 24085.0, 24065.0, 24075.0))
    update(entry_filter, "CE", strike, t0 + timedelta(minutes=1), 3,
           (24070.0, 24072.0, 24050.0, 24055.0))
    contrary = update(entry_filter, "CE", strike, t0 + timedelta(minutes=2), 4,
                      (24060.0, 24085.0, 24058.0, 24078.0))
    assert (contrary.status, contrary.telemetry.filter_state, entry_filter.retest_timestamp) == (
        "WAITING", "RETEST_READY", None,
    )

    valid = update(entry_filter, "CE", strike, t0 + timedelta(minutes=3), 5,
                   (24082.0, 24088.0, 24070.0, 24073.0))
    assert (valid.status, valid.trigger_price, valid.retest_timestamp) == (
        "QUALIFIED", 24073.0, t0 + timedelta(minutes=3),
    )
