"""
TASK-185 — per-setup-type SL / T1 / T2 override.

The engine applies a config-driven, per-setup-type level policy to every signal
before the R:R gate:
  * SL and T1 are REPLACED with fixed absolute distances from the trigger price.
  * T2 stays structural (whatever the detector found) as long as it lies beyond
    the new T1; otherwise it falls back to the per-type distance.

These tests pin that behavior and the shipped config defaults.
"""
from datetime import datetime

import pytest

from models import AresSignal, SetupType, Direction, ResistanceLevel
from config_profiles import TuningConfig, SetupLevels, _PER_TYPE_LEVELS_DEFAULT
from engine import apply_per_type_levels


def lvl(price):
    return ResistanceLevel(price=price, source="test", strength=2)


APPROVED = {
    "EXHAUSTION_REVERSAL": (10.0, 18.0, 40.0),
    "TREND_CONTINUATION": (25.0, 25.0, 80.0),
    "OI_WALL_REJECTION": (16.0, 25.0, 40.0),
    "FAILED_BREAKOUT": (12.0, 20.0, 55.0),
}


def make_signal(setup_type, direction, entry, stop_loss, target_1, target_2):
    """Minimal AresSignal for policy tests (structural SL/T1/T2 supplied raw)."""
    return AresSignal(
        setup_type=setup_type,
        direction=direction,
        trigger_price=entry,
        entry_zone=(entry - 5.0, entry + 5.0),
        stop_loss=stop_loss,
        target_1=target_1,
        target_2=target_2,
        confidence="HIGH",
        reasons=["test"],
        timestamp=datetime(2026, 7, 9, 10, 0, 0),
        strike_to_trade=25000,
        option_type="CE",
    )


@pytest.fixture
def settings():
    return TuningConfig()


# ── SL / T1 become fixed per-type distances, both directions ──────────────────

@pytest.mark.parametrize("setup_key", list(APPROVED))
def test_bullish_sl_t1_replaced_with_fixed_distance(settings, setup_key):
    sl_pts, t1_pts, _ = APPROVED[setup_key]
    entry = 25000.0
    sig = make_signal(
        SetupType(setup_key), Direction.BULLISH, entry,
        # deliberately absurd structural values that must be overwritten
        stop_loss=entry - 3.0, target_1=entry + 500.0, target_2=entry + 900.0,
    )
    apply_per_type_levels(sig, settings)
    assert sig.stop_loss == pytest.approx(entry - sl_pts)
    assert sig.target_1 == pytest.approx(entry + t1_pts)


@pytest.mark.parametrize("setup_key", list(APPROVED))
def test_bearish_sl_t1_replaced_with_fixed_distance(settings, setup_key):
    sl_pts, t1_pts, _ = APPROVED[setup_key]
    entry = 25000.0
    sig = make_signal(
        SetupType(setup_key), Direction.BEARISH, entry,
        stop_loss=entry + 3.0, target_1=entry - 500.0, target_2=entry - 900.0,
    )
    apply_per_type_levels(sig, settings)
    assert sig.stop_loss == pytest.approx(entry + sl_pts)
    assert sig.target_1 == pytest.approx(entry - t1_pts)


# ── T2: nearest structural level beyond T1, else per-type fallback ────────────

def test_bullish_t2_is_nearest_structural_level_beyond_t1(settings):
    entry = 25000.0  # EXHAUSTION new T1 = entry+24
    # levels: one below T1 (ignored), two beyond T1 -> pick the nearer (entry+60)
    levels = [lvl(entry + 10.0), lvl(entry + 60.0), lvl(entry + 160.0)]
    sig = make_signal(SetupType.EXHAUSTION_REVERSAL, Direction.BULLISH, entry,
                      stop_loss=entry - 3.0, target_1=entry + 10.0, target_2=entry + 5.0)
    apply_per_type_levels(sig, settings, levels)
    assert sig.target_2 == pytest.approx(entry + 60.0)


def test_bullish_t2_fallback_when_no_level_beyond_t1(settings):
    entry = 25000.0  # new T1 = entry+24; only a level below it -> per-type fallback 40
    levels = [lvl(entry + 15.0)]
    sig = make_signal(SetupType.EXHAUSTION_REVERSAL, Direction.BULLISH, entry,
                      stop_loss=entry - 3.0, target_1=entry + 10.0, target_2=entry + 5.0)
    apply_per_type_levels(sig, settings, levels)
    assert sig.target_2 == pytest.approx(entry + 40.0)


def test_bullish_t2_fallback_when_no_levels_at_all(settings):
    entry = 25000.0
    sig = make_signal(SetupType.EXHAUSTION_REVERSAL, Direction.BULLISH, entry,
                      stop_loss=entry - 3.0, target_1=entry + 10.0, target_2=entry + 5.0)
    apply_per_type_levels(sig, settings, levels=None)
    assert sig.target_2 == pytest.approx(entry + 40.0)


def test_bearish_t2_is_nearest_structural_level_beyond_t1(settings):
    entry = 25000.0  # new T1 = entry-24; nearer level below T1 is entry-60
    levels = [lvl(entry - 10.0), lvl(entry - 60.0), lvl(entry - 160.0)]
    sig = make_signal(SetupType.EXHAUSTION_REVERSAL, Direction.BEARISH, entry,
                      stop_loss=entry + 3.0, target_1=entry - 10.0, target_2=entry - 5.0)
    apply_per_type_levels(sig, settings, levels)
    assert sig.target_2 == pytest.approx(entry - 60.0)


def test_bearish_t2_fallback_when_no_level_beyond_t1(settings):
    entry = 25000.0
    levels = [lvl(entry - 15.0)]
    sig = make_signal(SetupType.EXHAUSTION_REVERSAL, Direction.BEARISH, entry,
                      stop_loss=entry + 3.0, target_1=entry - 10.0, target_2=entry - 5.0)
    apply_per_type_levels(sig, settings, levels)
    assert sig.target_2 == pytest.approx(entry - 40.0)


# ── R:R gate: every default config passes (nothing suppressed) ────────────────

@pytest.mark.parametrize("setup_key", list(APPROVED))
def test_default_configs_pass_rr_gate(settings, setup_key):
    entry = 25000.0
    sig = make_signal(
        SetupType(setup_key), Direction.BULLISH, entry,
        stop_loss=entry - 3.0, target_1=entry + 500.0, target_2=entry + 900.0,
    )
    apply_per_type_levels(sig, settings)
    risk = abs(sig.trigger_price - sig.stop_loss)
    reward = abs(sig.target_1 - sig.trigger_price)
    assert risk > 0
    assert reward / risk >= settings.min_rr_ratio


# ── Safety: unknown / missing setup key leaves structural levels intact ───────

def test_unknown_setup_type_leaves_levels_untouched():
    settings = TuningConfig()
    settings.per_type_levels = {}  # nothing configured
    entry = 25000.0
    sig = make_signal(
        SetupType.OI_WALL_REJECTION, Direction.BULLISH, entry,
        stop_loss=entry - 48.0, target_1=entry + 33.0, target_2=entry + 66.0,
    )
    apply_per_type_levels(sig, settings)
    assert sig.stop_loss == pytest.approx(entry - 48.0)
    assert sig.target_1 == pytest.approx(entry + 33.0)
    assert sig.target_2 == pytest.approx(entry + 66.0)


# ── Config: both shipped profiles expose the approved table ───────────────────

def test_default_table_matches_approved_values():
    for key, (sl, t1, t2) in APPROVED.items():
        lv = _PER_TYPE_LEVELS_DEFAULT[key]
        assert (lv.stop_pts, lv.target_1_pts, lv.target_2_fallback_pts) == (sl, t1, t2)


def test_both_profiles_carry_all_setup_types():
    from config_profiles import NON_EXPIRY_CONFIG, EXPIRY_CONFIG
    for profile in (NON_EXPIRY_CONFIG, EXPIRY_CONFIG):
        assert set(profile.per_type_levels) == set(APPROVED)
        for key, (sl, t1, t2) in APPROVED.items():
            lv = profile.per_type_levels[key]
            assert isinstance(lv, SetupLevels)
            assert (lv.stop_pts, lv.target_1_pts, lv.target_2_fallback_pts) == (sl, t1, t2)
