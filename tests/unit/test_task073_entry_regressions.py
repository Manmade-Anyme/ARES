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
    assert (
        decisions[-1].retest_timestamp,
        decisions[-1].telemetry.retest_timestamp,
    ) == (candle(side, 3, GEOMETRY[3]).timestamp,) * 2


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


def test_nearest_wall_selected_across_sides_when_spot_closer_to_opposite_side(profile):
    detector, entry_filter = OIWallDetector(), OIWallEntryFilter()
    # Step 1: Track CE:24100 when spot is 24080 (ce_dist = 20)
    c1 = OHLCVCandle(
        timestamp=datetime(2026, 9, 5, 9, 30),
        open=24075.0, high=24085.0, low=24070.0, close=24080.0, volume=1000,
    )
    ch1 = [
        {"strike": 24100, "ce_oi": 20000000, "ce_oi_change_pct": 25.0, "pe_oi": 100000, "pe_oi_change_pct": 0.0},
    ]
    bias1 = detector.update(c1.close, ch1, c1, [])
    decision1 = entry_filter.update(bias1, c1, [])
    assert detector.current_wall_key == "CE:24100"
    assert decision1.wall_key == "CE:24100"

    # Step 2: Spot falls to 24010.
    # Now both CE:24100 (dist 90) and PE:24000 (dist 10) qualify.
    c2 = OHLCVCandle(
        timestamp=datetime(2026, 9, 5, 9, 31),
        open=24020.0, high=24025.0, low=24005.0, close=24010.0, volume=1000,
    )
    ch2 = [
        {"strike": 24100, "ce_oi": 20000000, "ce_oi_change_pct": 25.0, "pe_oi": 100000, "pe_oi_change_pct": 0.0},
        {"strike": 24000, "ce_oi": 100000, "ce_oi_change_pct": 0.0, "pe_oi": 20000000, "pe_oi_change_pct": 25.0},
    ]
    bias2 = detector.update(c2.close, ch2, c2, [])
    # Distance to PE:24000 is 10, distance to CE:24100 is 90 -> PE:24000 must be selected
    assert bias2 is not None
    assert bias2.wall_key == "PE:24000"
    decision2 = entry_filter.update(bias2, c2, [])
    assert decision2.wall_key == "PE:24000"
    assert entry_filter.latest_expired_decision is not None
    assert entry_filter.latest_expired_decision.wall_key == "CE:24100"
    assert entry_filter.latest_expired_decision.status == "EXPIRED"


def test_replacement_selection_independent_of_chain_order(profile):
    c = OHLCVCandle(
        timestamp=datetime(2026, 9, 5, 9, 30),
        open=24075.0, high=24085.0, low=24070.0, close=24080.0, volume=1000,
    )
    # Qualifying CE strikes at 24300 (dist 220) and 24200 (dist 120)
    row_24300 = {"strike": 24300, "ce_oi": 20000000, "ce_oi_change_pct": 25.0, "pe_oi": 100000, "pe_oi_change_pct": 0.0}
    row_24200 = {"strike": 24200, "ce_oi": 20000000, "ce_oi_change_pct": 25.0, "pe_oi": 100000, "pe_oi_change_pct": 0.0}

    # Order 1: 24300 first, 24200 second
    detector1 = OIWallDetector()
    bias1 = detector1.update(c.close, [row_24300, row_24200], c, [])
    assert bias1 is not None
    assert bias1.wall_key == "CE:24200"

    # Order 2: 24200 first, 24300 second
    detector2 = OIWallDetector()
    bias2 = detector2.update(c.close, [row_24200, row_24300], c, [])
    assert bias2 is not None
    assert bias2.wall_key == "CE:24200"


def test_engine_persists_expired_wall_telemetry_on_replacement(profile):
    engine = AresEngine()
    # Candle 1: Track CE:24100
    c1 = OHLCVCandle(
        timestamp=datetime(2026, 9, 5, 9, 30),
        open=24075.0, high=24085.0, low=24070.0, close=24080.0, volume=1000,
    )
    ch1 = [
        {"strike": 24100, "ce_oi": 20000000, "ce_oi_change_pct": 25.0, "pe_oi": 100000, "pe_oi_change_pct": 0.0},
    ]
    atm1 = ATMStrikes(c1.close, *[OptionRow(24100, opt, 100.0, 12.0, 100000, 100000, 0.0) for opt in ("CE", "PE")])
    engine.tick(c1, ch1, atm1, 0.0, [])
    assert engine.latest_oi_wall_context is not None
    assert engine.latest_oi_wall_context["wall_key"] == "CE:24100"
    assert engine.latest_oi_wall_context.get("expired_wall") is None

    # Candle 2: Wall shifts to PE:24000
    c2 = OHLCVCandle(
        timestamp=datetime(2026, 9, 5, 9, 31),
        open=24020.0, high=24025.0, low=24005.0, close=24010.0, volume=1000,
    )
    ch2 = [
        {"strike": 24100, "ce_oi": 20000000, "ce_oi_change_pct": 25.0, "pe_oi": 100000, "pe_oi_change_pct": 0.0},
        {"strike": 24000, "ce_oi": 100000, "ce_oi_change_pct": 0.0, "pe_oi": 20000000, "pe_oi_change_pct": 25.0},
    ]
    atm2 = ATMStrikes(c2.close, *[OptionRow(24000, opt, 100.0, 12.0, 100000, 100000, 0.0) for opt in ("CE", "PE")])
    engine.tick(c2, ch2, atm2, 0.0, [])

    ctx = engine.latest_oi_wall_context
    assert ctx is not None
    assert ctx["wall_key"] == "PE:24000"
    assert "expired_wall" in ctx
    expired = ctx["expired_wall"]
    assert expired["wall_key"] == "CE:24100"
    assert expired["entry_status"] == "EXPIRED"
    assert "replaced" in expired["rejection_reason"].lower()

    assert engine.latest_expired_decision is not None
    assert engine.latest_expired_decision.wall_key == "CE:24100"
    assert engine.latest_expired_oi_wall_context == expired


@pytest.mark.parametrize("side", ["CE", "PE"])
def test_tracked_wall_not_displaced_by_closer_opposite_side_during_retest(profile, side):
    """
    Regression: MANM-110.

    Scenario: A wall is being tracked (price approaching for a retest).
    A qualifying opposite-side wall appears slightly closer to spot than the tracked wall.
    The tracked wall must NOT be displaced — doing so would reset the entry filter state
    machine and silently prevent OI_WALL_REJECTION signals from ever firing.
    """
    detector, entry_filter = OIWallDetector(), OIWallEntryFilter()

    tracked_strike = 24100 if side == "CE" else 24000
    competing_strike = 24075 if side == "CE" else 24025
    expected_wall_key = f"{side}:{tracked_strike}"
    opp_side = "PE" if side == "CE" else "CE"

    # Step 1: Establish tracking — distance is 40 pts.
    spot1 = 24060.0 if side == "CE" else 24040.0
    c1 = OHLCVCandle(
        timestamp=datetime(2026, 9, 5, 9, 30),
        open=spot1 - 5.0, high=spot1 + 5.0, low=spot1 - 10.0, close=spot1, volume=1000,
    )
    ch1 = [
        {
            "strike": tracked_strike,
            "ce_oi": 20000000 if side == "CE" else 100000,
            "ce_oi_change_pct": 25.0 if side == "CE" else 0.0,
            "pe_oi": 20000000 if side == "PE" else 100000,
            "pe_oi_change_pct": 25.0 if side == "PE" else 0.0,
        },
    ]
    bias1 = detector.update(c1.close, ch1, c1, [])
    entry_filter.update(bias1, c1, [])
    assert detector.current_wall_key == expected_wall_key

    # Step 2: Price moves towards tracked wall (dist = 15 pts, <= 2x interaction_dist=40).
    # A qualifying opposite-side wall appears 10 pts from spot (closer than tracked wall).
    # The tracked wall must survive.
    spot2 = 24085.0 if side == "CE" else 24015.0
    c2 = OHLCVCandle(
        timestamp=datetime(2026, 9, 5, 9, 31),
        open=spot2 - 3.0, high=spot2 + 5.0, low=spot2 - 5.0, close=spot2, volume=1000,
    )
    ch2 = [
        {
            "strike": tracked_strike,
            "ce_oi": 20000000 if side == "CE" else 100000,
            "ce_oi_change_pct": 25.0 if side == "CE" else 0.0,
            "pe_oi": 20000000 if side == "PE" else 100000,
            "pe_oi_change_pct": 25.0 if side == "PE" else 0.0,
        },
        {
            "strike": competing_strike,
            "ce_oi": 20000000 if opp_side == "CE" else 100000,
            "ce_oi_change_pct": 25.0 if opp_side == "CE" else 0.0,
            "pe_oi": 20000000 if opp_side == "PE" else 100000,
            "pe_oi_change_pct": 25.0 if opp_side == "PE" else 0.0,
        },
    ]
    bias2 = detector.update(c2.close, ch2, c2, [])
    assert bias2 is not None, f"{expected_wall_key} must still be emitted"
    assert bias2.wall_key == expected_wall_key, (
        f"Tracked {expected_wall_key} was displaced by {bias2.wall_key} — "
        "this is the MANM-110 regression: mid-retest displacement resets filter state"
    )
    decision2 = entry_filter.update(bias2, c2, [])
    assert decision2.wall_key == expected_wall_key
    assert entry_filter.latest_expired_decision is None, "No expiry should occur for a stable tracked wall"


@pytest.mark.parametrize("side", ["CE", "PE"])
def test_tracked_wall_yields_to_opposite_side_when_spot_moves_far(profile, side):
    """
    Regression guard: when spot moves far beyond the tracked wall's proximity window,
    the closer opposite-side wall correctly takes over (the legitimate cross-side switch
    scenario, NOT suppressed by MANM-110 fix).
    """
    detector, entry_filter = OIWallDetector(), OIWallEntryFilter()

    tracked_strike = 24100 if side == "CE" else 24000
    competing_strike = 24000 if side == "CE" else 24100
    expected_wall_key = f"{side}:{tracked_strike}"
    opp_side = "PE" if side == "CE" else "CE"
    opp_wall_key = f"{opp_side}:{competing_strike}"

    # Step 1: Track wall with spot 20 pts away.
    spot1 = 24080.0 if side == "CE" else 24020.0
    c1 = OHLCVCandle(
        timestamp=datetime(2026, 9, 5, 9, 30),
        open=spot1 - 5.0, high=spot1 + 5.0, low=spot1 - 10.0, close=spot1, volume=1000,
    )
    ch1 = [
        {
            "strike": tracked_strike,
            "ce_oi": 20000000 if side == "CE" else 100000,
            "ce_oi_change_pct": 25.0 if side == "CE" else 0.0,
            "pe_oi": 20000000 if side == "PE" else 100000,
            "pe_oi_change_pct": 25.0 if side == "PE" else 0.0,
        },
    ]
    bias1 = detector.update(c1.close, ch1, c1, [])
    entry_filter.update(bias1, c1, [])
    assert detector.current_wall_key == expected_wall_key

    # Step 2: Spot moves far (90 pts away, >> 2x interaction_dist=40).
    # Opposite-side wall is 10 pts away — clearly the more relevant wall.
    spot2 = 24010.0 if side == "CE" else 24090.0
    c2 = OHLCVCandle(
        timestamp=datetime(2026, 9, 5, 9, 31),
        open=spot2 + 10.0 if side == "CE" else spot2 - 10.0,
        high=spot2 + 15.0 if side == "CE" else spot2 + 5.0,
        low=spot2 - 5.0 if side == "CE" else spot2 - 15.0,
        close=spot2,
        volume=1000,
    )
    ch2 = [
        {
            "strike": tracked_strike,
            "ce_oi": 20000000 if side == "CE" else 100000,
            "ce_oi_change_pct": 25.0 if side == "CE" else 0.0,
            "pe_oi": 20000000 if side == "PE" else 100000,
            "pe_oi_change_pct": 25.0 if side == "PE" else 0.0,
        },
        {
            "strike": competing_strike,
            "ce_oi": 20000000 if opp_side == "CE" else 100000,
            "ce_oi_change_pct": 25.0 if opp_side == "CE" else 0.0,
            "pe_oi": 20000000 if opp_side == "PE" else 100000,
            "pe_oi_change_pct": 25.0 if opp_side == "PE" else 0.0,
        },
    ]
    bias2 = detector.update(c2.close, ch2, c2, [])
    assert bias2 is not None
    assert bias2.wall_key == opp_wall_key, f"Tracked {expected_wall_key} is 90 pts away — {opp_wall_key} must win"
    decision2 = entry_filter.update(bias2, c2, [])
    assert decision2.wall_key == opp_wall_key
    assert entry_filter.latest_expired_decision is not None
    assert entry_filter.latest_expired_decision.wall_key == expected_wall_key


def test_terminal_wall_does_not_pin_detector_over_closer_opposite_side(profile):
    """A terminal filter decision must release detector priority for that wall."""
    detector = OIWallDetector()

    # Start by tracking a CE wall.  It remains close enough to win MANM-110's
    # cross-side priority rule unless terminal state releases it.
    initial = OHLCVCandle(
        timestamp=datetime(2026, 9, 5, 9, 30),
        open=24055.0, high=24065.0, low=24050.0, close=24060.0, volume=1000,
    )
    ce_wall = {
        "strike": 24100,
        "ce_oi": 20000000,
        "ce_oi_change_pct": 25.0,
        "pe_oi": 100000,
        "pe_oi_change_pct": 0.0,
    }
    assert detector.update(initial.close, [ce_wall], initial, []).wall_key == "CE:24100"

    detector.release_terminal_wall("CE:24100")

    # The CE wall is 15 points away, but the fresh PE wall is 10 points away.
    # A terminal CE wall must not suppress the nearer, live PE opportunity.
    follow_up = OHLCVCandle(
        timestamp=datetime(2026, 9, 5, 9, 31),
        open=24085.0, high=24090.0, low=24075.0, close=24085.0, volume=1000,
    )
    pe_wall = {
        "strike": 24075,
        "ce_oi": 100000,
        "ce_oi_change_pct": 0.0,
        "pe_oi": 20000000,
        "pe_oi_change_pct": 25.0,
    }
    bias = detector.update(follow_up.close, [ce_wall, pe_wall], follow_up, [])

    assert bias is not None
    assert bias.wall_key == "PE:24075", "Terminal CE wall must not retain priority"


@pytest.mark.parametrize("side", ["CE", "PE"])
def test_pre_interaction_tracked_wall_does_not_block_closer_opposite_wall(profile, side):
    """
    P1 Regression Guard:
    When a tracked wall is merely in TRACKING and has never interacted with price
    (e.g. 39 pts away), it must not be granted unconditional priority over a substantially
    closer, immediately actionable opposite-side wall (e.g. 1 pt away).
    """
    detector, entry_filter = OIWallDetector(), OIWallEntryFilter()

    tracked_strike = 24100 if side == "CE" else 24000
    outer_dist = (profile.oi_wall_initial_interaction_distance_pts * 2.0) - 1.0
    spot1 = tracked_strike - outer_dist if side == "CE" else tracked_strike + outer_dist
    competing_strike = int(spot1 - 1.0) if side == "CE" else int(spot1 + 1.0)

    expected_wall_key = f"{side}:{tracked_strike}"
    opp_side = "PE" if side == "CE" else "CE"
    opp_wall_key = f"{opp_side}:{competing_strike}"

    # Step 1: Establish tracking on candidate wall in the outer proximity band.
    # Narrow candle does NOT touch initial interaction band.
    c1 = OHLCVCandle(
        timestamp=datetime(2026, 9, 5, 9, 30),
        open=spot1, high=spot1 + 0.5, low=spot1 - 0.5, close=spot1, volume=1000,
    )
    ch1 = [
        {
            "strike": tracked_strike,
            "ce_oi": 20000000 if side == "CE" else 100000,
            "ce_oi_change_pct": 25.0 if side == "CE" else 0.0,
            "pe_oi": 20000000 if side == "PE" else 100000,
            "pe_oi_change_pct": 25.0 if side == "PE" else 0.0,
        },
    ]
    bias1 = detector.update(c1.close, ch1, c1, [])
    decision1 = entry_filter.update(bias1, c1, [])
    assert detector.current_wall_key == expected_wall_key
    assert decision1.wall_key == expected_wall_key
    assert entry_filter.initial_interaction_timestamp is None, "Candidate wall has not interacted"

    # Step 2: Price stays at spot1. A qualifying opposite wall appears 1 pt away.
    # The pre-interaction tracked wall must NOT suppress the 1 pt opposite wall.
    c2 = OHLCVCandle(
        timestamp=datetime(2026, 9, 5, 9, 31),
        open=spot1, high=spot1 + 0.5, low=spot1 - 0.5, close=spot1, volume=1000,
    )
    ch2 = [
        {
            "strike": tracked_strike,
            "ce_oi": 20000000 if side == "CE" else 100000,
            "ce_oi_change_pct": 25.0 if side == "CE" else 0.0,
            "pe_oi": 20000000 if side == "PE" else 100000,
            "pe_oi_change_pct": 25.0 if side == "PE" else 0.0,
        },
        {
            "strike": competing_strike,
            "ce_oi": 20000000 if opp_side == "CE" else 100000,
            "ce_oi_change_pct": 25.0 if opp_side == "CE" else 0.0,
            "pe_oi": 20000000 if opp_side == "PE" else 100000,
            "pe_oi_change_pct": 25.0 if opp_side == "PE" else 0.0,
        },
    ]
    bias2 = detector.update(c2.close, ch2, c2, [])
    assert bias2 is not None
    assert bias2.wall_key == opp_wall_key, (
        f"Pre-interaction tracked wall {expected_wall_key} ({outer_dist} pts away) wrongly suppressed "
        f"immediately actionable opposite wall {opp_wall_key} (1 pt away)"
    )
    decision2 = entry_filter.update(bias2, c2, [])
    assert decision2.wall_key == opp_wall_key

