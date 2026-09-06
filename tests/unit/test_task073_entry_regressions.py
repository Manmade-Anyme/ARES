"""Public detector/filter and engine regressions for the TASK-073 audit."""
from datetime import datetime, timedelta

import pytest

from config import settings
from config_profiles import EXPIRY_CONFIG, NON_EXPIRY_CONFIG
from detectors.oi_wall import OIWallDetector
from detectors.oi_wall_entry import OIWallEntryFilter
from engine import AresEngine
from models import ATMStrikes, OHLCVCandle, OptionRow, SetupType


@pytest.fixture(params=[NON_EXPIRY_CONFIG, EXPIRY_CONFIG], ids=["non_expiry", "expiry"])
def profile(request):
    previous = settings._tuning
    settings.apply_profile(request.param)
    yield request.param
    settings.apply_profile(previous)


def candle(side, minute, prices):
    """Mirror CE geometry about the strike for the bullish PE cases."""
    sign = 1 if side == "CE" else -1
    opening, high, low, close = [24100 + sign * offset for offset in prices]
    return OHLCVCandle(
        timestamp=datetime(2026, 9, 5, 9, 30) + timedelta(minutes=minute),
        open=opening, high=max(high, low), low=min(high, low), close=close,
        volume=1000,
    )


def chain(side):
    row = {"strike": 24100}
    for option_type in ("CE", "PE"):
        prefix = option_type.lower()
        row.update({
            f"{prefix}_oi": 20000000 if option_type == side else 100000,
            f"{prefix}_oi_prev": 16000000 if option_type == side else 100000,
            f"{prefix}_oi_change_pct": 25.0 if option_type == side else 0.0,
        })
    return [row]


GEOMETRY = [
    (-25, -5, -30, -20),  # interaction in both profiles
    (-25, -23, -50, -45),  # excursion
    (-45, -40, -55, -50),  # persistence -> ready
    (-35, -5, -38, -22),  # later defended re-test, not confirmation
    (-22, -20, -45, -40),  # directional close beyond the re-test extreme
]


def update(detector, entry_filter, side, minute, prices):
    current = candle(side, minute, prices)
    bias = detector.update(current.close, chain(side), current, [])
    return entry_filter.update(bias, current, [])


def qualify(detector, entry_filter, side, start=0):
    qualified = None
    for index, prices in enumerate(GEOMETRY):
        decision = update(detector, entry_filter, side, start + index, prices)
        if decision.status == "QUALIFIED":
            qualified = decision
    assert qualified is not None, "genuine follow-through must qualify"
    return qualified


@pytest.mark.parametrize("side", ["CE", "PE"])
def test_consumed_wall_does_not_consume_alternating_wall(profile, side):
    detector, entry_filter = OIWallDetector(), OIWallEntryFilter()
    qualified = qualify(detector, entry_filter, side)
    entry_filter.acknowledge(qualified, "EMITTED")
    other = "PE" if side == "CE" else "CE"
    update(detector, entry_filter, other, 5, GEOMETRY[0])
    update(detector, entry_filter, side, 6, GEOMETRY[0])
    assert qualify(detector, entry_filter, other, 7).wall_key == f"{other}:24100"


@pytest.mark.parametrize("side", ["CE", "PE"])
def test_return_to_consumed_wall_clears_other_wall_geometry(profile, side):
    detector, entry_filter = OIWallDetector(), OIWallEntryFilter()
    entry_filter.acknowledge(qualify(detector, entry_filter, side), "EMITTED")
    other = "PE" if side == "CE" else "CE"
    for index, prices in enumerate(GEOMETRY[:4]):
        update(detector, entry_filter, other, 5 + index, prices)
    decision = update(detector, entry_filter, side, 9, GEOMETRY[0])
    assert entry_filter.latest_expired_decision is not None
    assert entry_filter.latest_expired_decision.status == "EXPIRED"
    assert entry_filter.latest_expired_decision.wall_key == f"{other}:24100"
    assert decision.status == "CONSUMED"
    assert (
        decision.telemetry.initial_interaction_timestamp,
        decision.telemetry.favourable_excursion_pts,
        decision.telemetry.retest_timestamp,
    ) == (None, None, None)


@pytest.mark.parametrize("side", ["CE", "PE"])
def test_tracked_wall_emits_expiration_on_replacement_wall(profile, side):
    detector, entry_filter = OIWallDetector(), OIWallEntryFilter()
    for index, prices in enumerate(GEOMETRY[:3]):
        update(detector, entry_filter, side, index, prices)
    assert entry_filter.state == "RETEST_READY"
    other = "PE" if side == "CE" else "CE"
    decision = update(detector, entry_filter, other, 3, GEOMETRY[0])
    expired = entry_filter.latest_expired_decision
    assert expired is not None
    assert expired.status == "EXPIRED"
    assert expired.wall_key == f"{side}:24100"
    assert expired.telemetry.filter_state == "EXPIRED"
    assert expired.telemetry.initial_interaction_timestamp is not None
    assert expired.telemetry.favourable_excursion_pts == 55.0
    assert "replaced" in expired.rejection_reason.lower()
    assert decision.status == "WAITING"
    assert decision.wall_key == f"{other}:24100"
    assert entry_filter.state == "INTERACTED"
    next_decision = update(detector, entry_filter, other, 4, GEOMETRY[0])
    assert next_decision.status == "WAITING"
    assert next_decision.wall_key == f"{other}:24100"


@pytest.mark.parametrize("side", ["CE", "PE"])
def test_tracked_wall_at_exact_equality_evaluated_as_defended(profile, side):
    detector, entry_filter = OIWallDetector(), OIWallEntryFilter()
    # Step 1: Establish tracking on candle 0 away from the wall strike
    update(detector, entry_filter, side, 0, GEOMETRY[0])
    assert detector.current_wall_key == f"{side}:24100"
    assert entry_filter.state == "INTERACTED"

    # Step 2: Price reaches exact equality at strike (offset 0: close == strike)
    equality_decision = update(detector, entry_filter, side, 1, (-10, 0, -15, 0))
    assert detector.current_wall_key == f"{side}:24100"
    assert equality_decision.status == "WAITING"
    assert equality_decision.wall_key == f"{side}:24100"
    assert equality_decision.telemetry.filter_state == "INTERACTED"
    assert entry_filter.state == "INTERACTED"


@pytest.mark.parametrize("side", ["CE", "PE"])
def test_tracked_wall_breach_evaluates_and_resets_detector(profile, side):
    detector, entry_filter = OIWallDetector(), OIWallEntryFilter()
    # Step 1: Establish tracking on candle 0
    update(detector, entry_filter, side, 0, GEOMETRY[0])
    assert detector.current_wall_key == f"{side}:24100"

    # Step 2: Price closes beyond strike (breach: close offset +5)
    breach_decision = update(detector, entry_filter, side, 1, (-5, 10, -5, 5))
    assert breach_decision.status == "EXPIRED"
    assert breach_decision.wall_key == f"{side}:24100"
    assert "breach" in breach_decision.rejection_reason.lower()
    assert detector.current_wall_key is None

    # Step 3: Next candle - detector does not force breached strike
    c3 = candle(side, 2, (-5, 10, -5, 5))
    bias3 = detector.update(c3.close, chain(side), c3, [])
    assert bias3 is None or bias3.wall_key != f"{side}:24100"


@pytest.mark.parametrize("side", ["CE", "PE"])
def test_defended_retest_waits_for_later_confirmation(profile, side):
    detector, entry_filter = OIWallDetector(), OIWallEntryFilter()
    decisions = [update(detector, entry_filter, side, i, p) for i, p in enumerate(GEOMETRY)]
    assert [decision.status for decision in decisions] == ["WAITING"] * 4 + ["QUALIFIED"]


@pytest.mark.parametrize("side", ["CE", "PE"])
@pytest.mark.parametrize("prices", [
    (-40, -39, -42, -40),  # flat body, even beyond candidate extreme
    (-45, -39, -46, -40),  # wrong body direction despite extreme break
    (-22, -20, -39, -38),  # equality with candidate extreme is not a break
])
def test_confirmation_requires_direction_and_strict_extreme_break(profile, side, prices):
    detector, entry_filter = OIWallDetector(), OIWallEntryFilter()
    for index, geometry in enumerate(GEOMETRY[:4]):
        update(detector, entry_filter, side, index, geometry)
    assert update(detector, entry_filter, side, 4, prices).status == "WAITING"


@pytest.mark.parametrize("side", ["CE", "PE"])
def test_same_timestamp_cannot_confirm_retest(profile, side):
    detector, entry_filter = OIWallDetector(), OIWallEntryFilter()
    for index, prices in enumerate(GEOMETRY[:4]):
        update(detector, entry_filter, side, index, prices)
    assert update(detector, entry_filter, side, 3, GEOMETRY[4]).status == "WAITING"


@pytest.mark.parametrize("side", ["CE", "PE"])
def test_failed_confirmation_requires_fresh_touch_before_recovery(profile, side):
    detector, entry_filter = OIWallDetector(), OIWallEntryFilter()
    for index, prices in enumerate(GEOMETRY[:4]):
        update(detector, entry_filter, side, index, prices)
    update(detector, entry_filter, side, 4, (-40, -39, -42, -40))
    unconfirmed = update(detector, entry_filter, side, 5, (-40, -35, -50, -45))
    update(detector, entry_filter, side, 6, GEOMETRY[3])
    confirmed = update(detector, entry_filter, side, 7, GEOMETRY[4])
    assert (unconfirmed.status, confirmed.status) == ("WAITING", "QUALIFIED")


@pytest.mark.parametrize("side", ["CE", "PE"])
@pytest.mark.parametrize("outcome", ["SUPPRESSED_BY_COOLDOWN", "SUPPRESSED_BY_PRIORITY"])
def test_suppression_requires_a_fresh_retest(profile, side, outcome):
    detector, entry_filter = OIWallDetector(), OIWallEntryFilter()
    entry_filter.acknowledge(qualify(detector, entry_filter, side), outcome)
    # More follow-through is not a new wall touch.
    decision = update(detector, entry_filter, side, 5, (-40, -35, -55, -50))
    assert decision.status == "WAITING"
    update(detector, entry_filter, side, 6, GEOMETRY[3])
    assert update(detector, entry_filter, side, 7, GEOMETRY[4]).status == "QUALIFIED"


@pytest.mark.parametrize("side", ["CE", "PE"])
def test_rr_expiration_remains_authoritative(profile, side):
    detector, entry_filter = OIWallDetector(), OIWallEntryFilter()
    expired = entry_filter.acknowledge(qualify(detector, entry_filter, side), "REJECTED_BY_RR")
    decisions = [update(detector, entry_filter, side, 5 + i, p) for i, p in enumerate(GEOMETRY)]
    assert [(d.status, d.telemetry.entry_status, d.rejection_reason, d.telemetry.rejection_reason)
            for d in decisions] == [("EXPIRED", "EXPIRED", expired.rejection_reason, expired.rejection_reason)] * 5


@pytest.mark.parametrize("side", ["CE", "PE"])
def test_breach_expiration_remains_authoritative(profile, side):
    detector, entry_filter = OIWallDetector(), OIWallEntryFilter()
    first = candle(side, 0, GEOMETRY[0])
    bias = detector.update(first.close, chain(side), first, [])
    expired = entry_filter.update(bias, candle(side, 1, (-10, 10, -15, 5)), [])
    decision = entry_filter.update(bias, candle(side, 2, GEOMETRY[0]), [])
    assert (decision.status, decision.telemetry.rejection_reason) == ("EXPIRED", expired.rejection_reason)


def tick(engine, side, minute, prices):
    current = candle(side, minute, prices)
    atm = ATMStrikes(current.close, *[
        OptionRow(24100, option_type, 100.0, 12.0, 100000, 100000, 0.0)
        for option_type in ("CE", "PE")
    ])
    return engine.tick(current, chain(side), atm, 0.0, [])


@pytest.mark.parametrize("side", ["CE", "PE"])
def test_real_engine_never_emits_on_flat_two_point_candles(profile, side):
    engine = AresEngine()
    distance = profile.oi_wall_initial_interaction_distance_pts
    flat = (-distance, 1 - distance, -1 - distance, -distance)
    assert [tick(engine, side, i, flat) for i in range(8)] == [None] * 8


@pytest.mark.parametrize("side", ["CE", "PE"])
def test_real_engine_confirmation_keeps_central_risk_policy(profile, side):
    engine = AresEngine()
    signals = [tick(engine, side, i, prices) for i, prices in enumerate(GEOMETRY)]
    assert signals[:4] == [None] * 4
    signal = signals[4]
    assert signal is not None
    assert (signal.setup_type, abs(signal.stop_loss - signal.trigger_price),
            abs(signal.target_1 - signal.trigger_price), abs(signal.target_2 - signal.trigger_price)) == (
        SetupType.OI_WALL_REJECTION, 16.0, 25.0, 40.0,
    )
