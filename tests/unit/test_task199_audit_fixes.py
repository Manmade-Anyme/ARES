"""TASK-199 — regression guards for the four defects found auditing PRs #57-#61.

Every test here fails against the code as merged on 2026-08-01. They are written
against the REAL model and the REAL requirements file on purpose: the defects
they cover all survived a 330-test green suite because the existing coverage
mocks the boundary the bugs live at (a MagicMock accepts an object-dtype frame
that XGBoost rejects; nothing at all imported main.py's dependency set).
"""

import json
import math
from pathlib import Path

import pandas as pd
import pytest

REPO = Path(__file__).resolve().parents[2]


# --------------------------------------------------------------------------
# P0 — the container could not start: joblib/xgboost were never declared.
# --------------------------------------------------------------------------

def _requirement_names():
    """Distribution names declared in requirements.txt, lowercased."""
    names = set()
    for raw in (REPO / "requirements.txt").read_text().splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        # strip environment markers, extras and version pins
        line = line.split(";", 1)[0].strip()
        for sep in ("==", ">=", "<=", "~=", ">", "<", "["):
            line = line.split(sep, 1)[0]
        names.add(line.strip().lower())
    return names


@pytest.mark.parametrize("dist", ["joblib", "xgboost"])
def test_ml_runtime_deps_are_declared(dist):
    """main.py imports ml_signal.predictor at module scope, which imports joblib
    and unpickles an XGBClassifier. The Dockerfile installs ONLY requirements.txt,
    so an undeclared import here is a hard startup crash, not a degraded feature.
    """
    assert dist in _requirement_names(), (
        f"{dist} is imported on main.py's startup path but missing from "
        f"requirements.txt — the Fly container will raise ModuleNotFoundError."
    )


def test_predictor_import_has_no_undeclared_third_party_imports():
    """The predictor module must import cleanly using only declared deps."""
    import ml_signal.predictor as predictor  # noqa: F401

    assert hasattr(predictor, "SignalPredictor")


# --------------------------------------------------------------------------
# P1 — live prediction raised on ~67% of ticks (unknown structure features).
# --------------------------------------------------------------------------

MODEL_PATH = REPO / "ml_signal" / "models" / "v1.joblib"


@pytest.mark.skipif(not MODEL_PATH.exists(), reason="v1.joblib not present")
@pytest.mark.parametrize(
    "levels,pdh,pdl",
    [
        ([23900.0, 24100.0], 24080.0, 23950.0),  # full data — the only covered case
        ([], 24080.0, 23950.0),                  # no levels at all
        ([23900.0, 24100.0], None, None),        # no prior-day high/low
        ([24100.0, 24200.0], None, None),        # every level above spot (~67% of ticks)
        ([], None, None),                        # nothing known
    ],
)
def test_predict_from_raw_survives_unknown_structure_features(levels, pdh, pdl):
    """compute_structure_features returns None when a distance is unknown. On a
    single-row frame that makes the column object-dtype, which XGBoost refuses.
    """
    from datetime import datetime

    from ml_signal.predictor import SignalPredictor

    predictor = SignalPredictor()
    predictor.load_model(str(MODEL_PATH))

    result = predictor.predict_from_raw(
        candle={"open": 24000.0, "high": 24050.0, "low": 23980.0,
                "close": 24030.0, "volume": 120000, "vwap": 24010.0},
        volume_history=[100000, 110000, 105000],
        iv_history=[13.0, 13.2, 13.4],
        atm_ce={"iv": 13.5, "oi": 1200000, "oi_change_pct": 4.2,
                "gamma": 0.0007, "theta": -9.1, "vega": 12.3},
        atm_pe={"iv": 13.4, "oi": 1100000, "oi_change_pct": 3.1,
                "gamma": 0.0006, "theta": -8.8, "vega": 12.0},
        total_ce_oi=9_000_000,
        total_pe_oi=8_000_000,
        all_ce_oi=[900000, 950000, 1000000],
        all_pe_oi=[800000, 850000, 900000],
        levels=levels,
        timestamp=datetime(2026, 8, 3, 10, 30),
        spot=24030.0,
        pdh=pdh,
        pdl=pdl,
        dte=2,
        is_expiry=False,
    )

    assert 0.0 <= result["probability"] <= 1.0
    assert result["confidence_tier"] in ("HIGH", "MEDIUM", "LOW")


# --------------------------------------------------------------------------
# P1 — STOPPED_OUT_AT_BE posted a Discord embed with a blank Action line.
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "update_type",
    ["T1_HIT", "T2_HIT", "SL_HIT", "TIME_STOP", "STOPPED_OUT_AT_BE"],
)
def test_every_exit_state_has_alert_text(update_type):
    """position_manager emits STOPPED_OUT_AT_BE since TASK-198; an unmapped state
    renders '⚡ Action : ****' in the live Discord embed.
    """
    from alerts import trade_update_action_text

    trade = {"state": "T1_HIT", "stop_loss": 24000.0, "entry_price": 24000.0}
    assert trade_update_action_text(update_type, trade).strip()


def test_position_manager_exit_states_are_all_mapped():
    """Whatever position_manager can emit must be renderable by alerts."""
    import re

    from alerts import trade_update_action_text

    source = (REPO / "position_manager.py").read_text()
    emitted = set(re.findall(r'update_type = "([A-Z0-9_]+)"', source))
    assert emitted, "no update_type literals found — regex needs updating"

    trade = {"state": "T1_HIT", "stop_loss": 24000.0, "entry_price": 24000.0}
    unmapped = [s for s in sorted(emitted) if not trade_update_action_text(s, trade).strip()]
    assert not unmapped, f"position_manager emits states alerts cannot render: {unmapped}"


# --------------------------------------------------------------------------
# P2 — an unknown distance became 0.0 ("spot exactly AT the level") in training.
# --------------------------------------------------------------------------

def test_unknown_feature_is_nan_not_zero():
    """flatten_features filled absent keys with 0.0. For structure distances that
    reads as 'price is exactly at support/resistance' — a stronger and more
    misleading claim than the 100.0 sentinel TASK-194/195 set out to remove.
    """
    from ml_signal.dataset import flatten_features
    from ml_signal.features import compute_structure_features

    known = compute_structure_features(
        spot=24030.0, levels=[23900.0, 24100.0], full_chain=[], pdh=24080.0, pdl=23950.0
    )
    unknown = compute_structure_features(
        spot=24030.0, levels=[24100.0], full_chain=[], pdh=None, pdl=None
    )
    assert unknown["dist_to_nearest_support"] is None  # precondition

    def row(feats, ts):
        return {
            "timestamp": ts,
            "raw_candle": json.dumps({"close": 24030.0}),
            "structure_features": json.dumps(feats),
        }

    df = flatten_features([row(known, "2026-08-01T10:30:00Z"),
                           row(unknown, "2026-08-01T10:35:00Z")])

    for col in ("structure_features__dist_to_nearest_support",
                "structure_features__dist_to_pdh",
                "structure_features__dist_to_pdl"):
        value = df.iloc[1][col]
        assert value != 0.0, f"{col} encodes 'unknown' as 'exactly at the level'"
        assert math.isnan(value), f"{col} should be NaN so XGBoost treats it as missing"

    # A genuinely known distance must survive untouched.
    assert df.iloc[0]["structure_features__dist_to_nearest_support"] == pytest.approx(130.0)


def test_known_zero_distance_is_preserved():
    """Guard the fix's own edge: spot sitting exactly on a level is a REAL 0.0
    and must not be turned into NaN.
    """
    from ml_signal.dataset import flatten_features
    from ml_signal.features import compute_structure_features

    feats = compute_structure_features(
        spot=24030.0, levels=[24030.0, 24100.0], full_chain=[], pdh=24030.0, pdl=23900.0
    )
    assert feats["dist_to_pdh"] == 0.0  # precondition: a true zero

    df = flatten_features([{
        "timestamp": "2026-08-01T10:30:00Z",
        "raw_candle": json.dumps({"close": 24030.0}),
        "structure_features": json.dumps(feats),
    }])
    assert df.iloc[0]["structure_features__dist_to_pdh"] == 0.0
    assert not pd.isna(df.iloc[0]["structure_features__dist_to_pdh"])
