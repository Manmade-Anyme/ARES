import math
import json
from typing import Any, Dict, Optional, Tuple
from datetime import datetime


def _safe_float(val: Any) -> Optional[float]:
    """Convert to float; return None for missing/invalid values. Never returns 0.0 for None."""
    if val is None:
        return None
    try:
        f = float(val)
        if math.isnan(f) or math.isinf(f):
            return None
        return f
    except (TypeError, ValueError):
        return None


def _safe_int(val: Any) -> Optional[int]:
    """Convert to int; return None for missing/invalid values."""
    if val is None:
        return None
    try:
        return int(val)
    except (TypeError, ValueError):
        return None


def _parse_jsonb(val: Any) -> Optional[Dict[str, Any]]:
    """Parse a JSONB column value. Handles str, dict, or None."""
    if val is None:
        return None
    if isinstance(val, dict):
        return val
    if isinstance(val, str):
        try:
            return json.loads(val)
        except (json.JSONDecodeError, TypeError):
            return None
    return None


class BarrierValidationError(ValueError):
    """Raised when signal barrier geometry is invalid."""
    pass


def validate_barrier_geometry(
    direction: str,
    entry_price: float,
    target_1: float,
    target_2: float,
    stop_loss: float,
) -> None:
    """Validate that barriers are direction-consistent, finite, and properly ordered.
    
    For BULLISH: entry < T1 < T2 and SL < entry
    For BEARISH: entry > T1 > T2 and SL > entry
    All values must be finite and positive.
    
    Raises BarrierValidationError on any violation.
    """
    for name, val in [("entry_price", entry_price), ("target_1", target_1),
                       ("target_2", target_2), ("stop_loss", stop_loss)]:
        if not math.isfinite(val) or val <= 0:
            raise BarrierValidationError(f"{name} must be finite and positive, got {val}")

    if direction == "BULLISH":
        if not (stop_loss < entry_price < target_1 <= target_2):
            raise BarrierValidationError(
                f"BULLISH barrier order violated: SL={stop_loss} < entry={entry_price} < T1={target_1} <= T2={target_2}"
            )
    elif direction == "BEARISH":
        if not (stop_loss > entry_price > target_1 >= target_2):
            raise BarrierValidationError(
                f"BEARISH barrier order violated: SL={stop_loss} > entry={entry_price} > T1={target_1} >= T2={target_2}"
            )
    else:
        raise BarrierValidationError(f"Unknown direction: {direction}")


def build_context(
    signal_row: Dict[str, Any],
    snapshot_row: Dict[str, Any],
    xgboost_row: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Build the exact Jev state from persisted signal and ML snapshot rows.
    
    Args:
        signal_row: Row from ares_signals table.
        snapshot_row: Row from ml_collection table (signal-bound).
        xgboost_row: Optional row from ml_predictions for comparison.
        
    Returns:
        Dict suitable for TypeSafe system_one state parameter.
        
    Raises:
        BarrierValidationError: If barrier geometry is invalid.
        ValueError: If required fields are missing.
    """
    # Extract and validate signal fields
    direction = signal_row.get("direction")
    if direction not in ("BULLISH", "BEARISH"):
        raise ValueError(f"Invalid direction: {direction}")

    entry_price = _safe_float(signal_row.get("spot_at_signal"))
    if entry_price is None:
        raise ValueError("Missing spot_at_signal in signal row")

    trigger_price = _safe_float(signal_row.get("trigger_price"))
    target_1 = _safe_float(signal_row.get("target_1"))
    target_2 = _safe_float(signal_row.get("target_2"))
    stop_loss = _safe_float(signal_row.get("stop_loss"))

    if target_1 is None or target_2 is None or stop_loss is None:
        raise ValueError("Missing required barrier prices (target_1, target_2, stop_loss)")

    validate_barrier_geometry(direction, entry_price, target_1, target_2, stop_loss)

    # Compute distances and ratios
    sl_distance = abs(entry_price - stop_loss)
    t1_distance = abs(target_1 - entry_price)
    t2_distance = abs(target_2 - entry_price)
    reward_to_risk = t1_distance / sl_distance if sl_distance > 0 else None

    # Parse feature groups from snapshot
    candle_features = _parse_jsonb(snapshot_row.get("candle_features"))
    volume_features = _parse_jsonb(snapshot_row.get("volume_features"))
    iv_features = _parse_jsonb(snapshot_row.get("iv_features"))
    oi_features = _parse_jsonb(snapshot_row.get("oi_features"))
    greek_features = _parse_jsonb(snapshot_row.get("greek_features"))
    structure_features = _parse_jsonb(snapshot_row.get("structure_features"))
    meta_features = _parse_jsonb(snapshot_row.get("meta_features"))
    raw_candle = _parse_jsonb(snapshot_row.get("raw_candle"))
    raw_atm_oi = _parse_jsonb(snapshot_row.get("raw_atm_oi"))
    oi_wall_ctx = _parse_jsonb(snapshot_row.get("oi_wall_context"))

    # Semantic assessments to help Jev reason without guessing domain rules
    rr_assessment = None
    if reward_to_risk:
        if reward_to_risk < 1.0:
            rr_assessment = "Poor R:R, risk exceeds reward to T1."
        elif reward_to_risk < 1.5:
            rr_assessment = "Standard acceptable R:R to T1."
        else:
            rr_assessment = "Highly favorable asymmetric R:R to T1."

    signal_ctx = {
        "setup_type": signal_row.get("setup_type"),
        "direction": direction,
        "entry_price": entry_price,
        "trigger_price": trigger_price,
        "target_1": target_1,
        "target_2": target_2,
        "original_stop_loss": stop_loss,
        "post_t1_stop_price": entry_price,  # Breakeven after T1
        "t1_distance_pts": round(t1_distance, 2),
        "t2_distance_pts": round(t2_distance, 2),
        "sl_distance_pts": round(sl_distance, 2),
        "reward_to_risk": round(reward_to_risk, 4) if reward_to_risk else None,
        "reward_risk_assessment": rr_assessment,
        "confidence": signal_row.get("confidence"),
        "display_id": signal_row.get("display_id"),
        "reasons": signal_row.get("reasons"),
    }

    # Build market snapshot context — absent evidence stays None
    def _extract(features_dict: Optional[Dict], *keys: str) -> Dict[str, Any]:
        if features_dict is None:
            return {k: None for k in keys}
        return {k: _safe_float(features_dict.get(k)) for k in keys}

    candle_ctx = _extract(candle_features,
        "body_ratio", "upper_wick_ratio", "lower_wick_ratio",
        "range_pct", "is_green", "close_position", "open_close_spread")

    volume_ctx = _extract(volume_features,
        "vol_ratio", "vol_slope_5", "vol_percentile", "vol_ratio_20bar")

    iv_ctx = _extract(iv_features,
        "iv_current", "iv_change_1", "iv_change_5",
        "iv_spread_ce_pe", "iv_percentile")

    oi_ctx = _extract(oi_features,
        "pcr_oi", "pcr_change", "oi_concentration_atm",
        "ce_oi_change_pct", "pe_oi_change_pct")

    structure_ctx = _extract(structure_features,
        "dist_nearest_level_pct", "levels_above", "levels_below",
        "dist_pdh_pct", "dist_pdl_pct", "avg_wall_distance_pct")

    # Structural runway: levels between entry and targets
    if structure_features and entry_price:
        levels_above_val = _safe_float(structure_features.get("levels_above"))
        levels_below_val = _safe_float(structure_features.get("levels_below"))
        runway = levels_above_val if direction == "BULLISH" else levels_below_val
        
        assessment = "Unknown"
        if runway is not None:
            if runway == 0:
                assessment = "Immediate structural blockage; no clear runway."
            elif runway == 1:
                assessment = "Minimal runway; one level of friction present."
            else:
                assessment = "Clear structural runway with multiple levels of clearance."
                
        structure_ctx["runway_levels"] = runway
        structure_ctx["runway_assessment"] = assessment

    # OI Wall position relative to entry
    wall_ctx = None
    if oi_wall_ctx and oi_wall_ctx.get("wall_strike") is not None:
        wall_strike = _safe_float(oi_wall_ctx.get("wall_strike"))
        if wall_strike is not None and entry_price:
            wall_distance = wall_strike - entry_price  # positive = above entry
            wall_ctx = {
                "wall_strike": wall_strike,
                "wall_option_type": oi_wall_ctx.get("wall_option_type"),
                "wall_oi": _safe_int(oi_wall_ctx.get("wall_oi")),
                "wall_oi_change_pct": _safe_float(oi_wall_ctx.get("wall_oi_change_pct")),
                "wall_distance_from_entry": round(wall_distance, 2),
                "wall_persistence_snapshots": _safe_int(oi_wall_ctx.get("persistence_snapshots")),
            }

    # Raw candle wick profile
    wick_profile = None
    if raw_candle:
        o = _safe_float(raw_candle.get("open"))
        h = _safe_float(raw_candle.get("high"))
        l = _safe_float(raw_candle.get("low"))
        c = _safe_float(raw_candle.get("close"))
        if all(v is not None for v in [o, h, l, c]):
            full_range = h - l
            if full_range > 0:
                body = abs(c - o)
                upper = h - max(o, c)
                lower = min(o, c) - l
                wick_profile = {
                    "body_pct": round(body / full_range, 4),
                    "upper_wick_pct": round(upper / full_range, 4),
                    "lower_wick_pct": round(lower / full_range, 4),
                    "is_green": c >= o,
                }

    # ATM Greeks for IV behavior
    atm_ctx = None
    if raw_atm_oi:
        ce = raw_atm_oi.get("ce", {})
        pe = raw_atm_oi.get("pe", {})
        atm_ctx = {
            "ce_iv": _safe_float(ce.get("iv")),
            "pe_iv": _safe_float(pe.get("iv")),
            "ce_delta": _safe_float(ce.get("delta")),
            "pe_delta": _safe_float(pe.get("delta")),
            "ce_oi": _safe_int(ce.get("oi")),
            "pe_oi": _safe_int(pe.get("oi")),
        }

    # Assemble full context
    context: Dict[str, Any] = {
        "signal": signal_ctx,
        "candle": candle_ctx,
        "volume": volume_ctx,
        "iv": iv_ctx,
        "oi": oi_ctx,
        "structure": structure_ctx,
        "wick_profile": wick_profile,
        "oi_wall": wall_ctx,
        "atm_greeks": atm_ctx,
        "spot": _safe_float(snapshot_row.get("spot")),
        "snapshot_timestamp": snapshot_row.get("timestamp"),
    }

    # Optional XGBoost comparison
    if xgboost_row:
        context["xgboost_comparison"] = {
            "probability": _safe_float(xgboost_row.get("probability")),
            "confidence_tier": xgboost_row.get("confidence_tier"),
            "model_version": xgboost_row.get("model_version"),
        }

    return context
