"""Boundary tests for persisted market evidence and the Jev dispatch budget."""

import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from system_one.context import BarrierValidationError, build_context, validate_barrier_geometry
from system_one.jev import invoke_jev


@pytest.fixture
def signal():
    return {
        "direction": "BULLISH", "spot_at_signal": 100, "trigger_price": 100,
        "target_1": 110, "target_2": 120, "stop_loss": 90,
        "setup_type": "BREAKOUT", "display_id": "ABC", "reasons": ["confirmation"],
    }


@pytest.mark.parametrize("direction", [None, "SIDEWAYS", "bullish"])
def test_unknown_direction_is_rejected_before_context(signal, direction):
    signal["direction"] = direction
    with pytest.raises(ValueError, match="Invalid direction"):
        build_context(signal, {})


@pytest.mark.parametrize("price", [None, "bad", float("nan"), float("inf"), {}])
def test_invalid_entry_cannot_become_a_zero_price(signal, price):
    signal["spot_at_signal"] = price
    with pytest.raises(ValueError, match="Missing spot_at_signal"):
        build_context(signal, {})


@pytest.mark.parametrize("barrier", ["target_1", "target_2", "stop_loss"])
def test_missing_barrier_rejects_entire_setup(signal, barrier):
    signal.pop(barrier)
    with pytest.raises(ValueError, match="Missing required barrier prices"):
        build_context(signal, {})


@pytest.mark.parametrize("index", range(4))
@pytest.mark.parametrize("price", [0, -1, float("nan"), float("inf")])
def test_geometry_rejects_each_nonpositive_or_nonfinite_price(index, price):
    prices = [100, 110, 120, 90]
    prices[index] = price
    with pytest.raises(BarrierValidationError, match="finite and positive"):
        validate_barrier_geometry("BULLISH", *prices)


@pytest.mark.parametrize("direction,prices,reason", [
    ("BULLISH", (100, 110, 120, 100), "BULLISH barrier order"),
    ("BEARISH", (100, 90, 95, 110), "BEARISH barrier order"),
    ("BEARISH", (100, 90, 80, 100), "BEARISH barrier order"),
    ("UNKNOWN", (100, 110, 120, 90), "Unknown direction"),
])
def test_invalid_directional_geometry_has_specific_reason(direction, prices, reason):
    with pytest.raises(BarrierValidationError, match=reason):
        validate_barrier_geometry(direction, *prices)


@pytest.mark.parametrize("direction,t1,t2,sl", [
    ("BULLISH", 110, 110, 90), ("BEARISH", 90, 90, 110),
])
def test_equal_targets_remain_valid_and_preserve_breakeven_stop(signal, direction, t1, t2, sl):
    signal.update(direction=direction, target_1=t1, target_2=t2, stop_loss=sl)
    context = build_context(signal, {})
    assert context["signal"]["t1_distance_pts"] == context["signal"]["t2_distance_pts"] == 10
    assert context["signal"]["post_t1_stop_price"] == 100


@pytest.mark.parametrize("t1,ratio,assessment", [
    (105, .5, "Poor R:R, risk exceeds reward to T1."),
    (110, 1, "Standard acceptable R:R to T1."),
    (115, 1.5, "Highly favorable asymmetric R:R to T1."),
])
def test_reward_to_risk_thresholds_are_exact(signal, t1, ratio, assessment):
    signal["target_1"] = t1
    context = build_context(signal, {})
    assert context["signal"]["reward_to_risk"] == ratio
    assert context["signal"]["reward_risk_assessment"] == assessment


@pytest.mark.parametrize("serialized", [True, False])
def test_numeric_features_preserve_categories_and_flags_without_inventing_values(signal, serialized):
    features = {"missing": None, "confirmed": False, "ratio": "1.25", "regime": "CRUSHING",
                "invalid_object": {}, "nonfinite": float("nan")}
    if serialized:
        features = json.dumps(features)
    context = build_context(signal, {"iv_features": features})
    assert context["iv"] == {"confirmed": False, "ratio": 1.25, "regime": "CRUSHING"}


@pytest.mark.parametrize("features", [None, {}, "null", "{broken", [1, 2], 123])
def test_missing_or_unusable_feature_group_stays_unknown(signal, features):
    context = build_context(signal, {"volume_features": features})
    assert context["volume"] is None


@pytest.mark.parametrize("spot", [None, 0, -1, "bad"])
def test_unusable_snapshot_spot_cannot_reconstruct_saved_structure(signal, spot):
    context = build_context(signal, {"spot": spot, "structure_features": {"dist_to_pdh": 20}})
    assert context["structure"]["nearest_opposing_level_distance_pts"] is None
    assert context["structure"]["path_to_t1_clear"] is None


@pytest.mark.parametrize("wall_strike", [None, "bad", float("inf")])
def test_invalid_wall_price_does_not_create_an_oi_barrier(signal, wall_strike):
    context = build_context(signal, {"oi_wall_context": {"wall_strike": wall_strike}})
    assert context["oi_wall"] is None


@pytest.mark.parametrize("oi,persistence", [(None, None), ("bad", {}), ("500", "3")])
def test_optional_wall_counts_are_not_replaced_with_zero(signal, oi, persistence):
    context = build_context(signal, {"oi_wall_context": {
        "wall_strike": 105, "wall_oi": oi, "persistence_snapshots": persistence,
    }})
    assert context["oi_wall"]["wall_distance_from_entry"] == 5
    assert context["oi_wall"]["wall_oi"] == (500 if oi == "500" else None)
    assert context["oi_wall"]["wall_persistence_snapshots"] == (3 if persistence == "3" else None)


@pytest.mark.parametrize("candle", [
    {"open": 100, "high": 101, "low": 99},
    {"open": 100, "high": 100, "low": 100, "close": 100},
])
def test_incomplete_or_flat_candle_has_no_wick_profile(signal, candle):
    assert build_context(signal, {"raw_candle": candle})["wick_profile"] is None


def test_red_candle_wick_ratios_and_optional_atm_evidence(signal):
    context = build_context(signal, {
        "raw_candle": {"open": 103, "high": 105, "low": 95, "close": 99},
        "raw_atm_oi": {"ce": {"iv": "12.5", "delta": "bad", "oi": "100"}, "pe": {}},
    })
    assert context["wick_profile"] == {
        "body_pct": .4, "upper_wick_pct": .2, "lower_wick_pct": .4, "is_green": False,
    }
    assert context["atm_greeks"] == {
        "ce_iv": 12.5, "ce_delta": None, "ce_oi": 100,
        "pe_iv": None, "pe_delta": None, "pe_oi": None,
    }


def jev_response():
    """Typed SDK response shape, with nontrivial distributions worth archiving."""
    def choice(name, probabilities, confidence):
        return SimpleNamespace(choice=name, probabilities=probabilities, confidence=confidence)

    def score(value, confidence):
        return SimpleNamespace(score=value, confidence=confidence,
                               legend={0: "poor", 1: "fair", 2: "good", 3: "strong", 4: "exceptional"},
                               probabilities={0: .1, 4: .9})

    return SimpleNamespace(
        choices={
            "first_barrier": choice("sl_first", {"t1_first": .2, "sl_first": .7, "neither_by_close": .1}, .8),
            "t2_given_t1": choice("insufficient_evidence", {"t2_hits": .25, "insufficient_evidence": .75}, .6),
            "market_regime": choice("range_choppy", {"range_choppy": .7, "trending": .3}, .7),
        },
        scores={"price_action_strength": score(2, .9), "structural_clarity": score(3, .8),
                "confluence_rating": score(1, .4)},
        nouls={"is_trap": SimpleNamespace(noul=.35)}, model="jev-test", request_id="request-test",
        usage=SimpleNamespace(input_tokens=25, output_tokens=12),
    )


@pytest.mark.parametrize("deadline", [99, 100, float("nan"), float("inf")])
def test_expired_or_invalid_dispatch_budget_cannot_call_jev(deadline):
    with patch("system_one.jev.TypeSafeClient") as sdk, patch("system_one.jev.time.monotonic", return_value=100):
        client = sdk.return_value.__enter__.return_value
        with pytest.raises(ValueError, match="dispatch window elapsed"):
            invoke_jev({"signal": {}}, dispatch_deadline=deadline)
        client.system_one.assert_not_called()
        sdk.return_value.__exit__.assert_called_once()


@pytest.mark.parametrize("deadline,request_timeout", [(100.5, .5), (110, 2)])
def test_valid_dispatch_budget_caps_request_and_preserves_prediction_evidence(deadline, request_timeout):
    with patch("system_one.jev.TypeSafeClient") as sdk, patch("system_one.jev.time.monotonic", side_effect=[100, 100, 100.25]):
        client = sdk.return_value.__enter__.return_value
        client.system_one.return_value = jev_response()
        state = {"signal": {"direction": "BEARISH"}}
        result = invoke_jev(state, model="jev-test", timeout=2, dispatch_deadline=deadline)
        assert client.system_one.call_args.kwargs["timeout"] == request_timeout
        assert client.system_one.call_args.kwargs["state"] is state
        assert sdk.call_args.kwargs["retry"].max_retries == 0
        assert result.t1_hit_prob == .2
        assert result.t2_hit_prob == .05
        assert result.sl_hit_prob == .7
        assert result.regime == "range_choppy"
        assert result.setup_quality == 5.5
        assert result.setup_quality_confidence == .4
        assert result.latency_ms == 250
        assert result.raw_response["t2_given_t1"]["probabilities"]["insufficient_evidence"] == .75
        assert result.raw_response["price_action_strength"]["probabilities"] == {"0": .1, "4": .9}
        assert json.loads(json.dumps(result.raw_response))["usage"]["input_tokens"] == 25
