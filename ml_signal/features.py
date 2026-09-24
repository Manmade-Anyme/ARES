import math
from typing import Dict, List, Optional, Any

import numpy as np


def compute_candle_features(candle: Dict[str, float]) -> Dict[str, float]:
    open_p = candle["open"]
    high = candle["high"]
    low = candle["low"]
    close = candle["close"]
    volume = candle.get("volume", 0)

    body = abs(close - open_p)
    candle_range = high - low

    features = {
        "body_pct": body / close * 100 if close != 0 else 0,
        "upper_wick_pct": (high - max(open_p, close)) / close * 100 if close != 0 else 0,
        "lower_wick_pct": (min(open_p, close) - low) / close * 100 if close != 0 else 0,
        "range_pct": candle_range / close * 100 if close != 0 else 0,
        "body_to_range_ratio": body / candle_range if candle_range != 0 else 0,
        "is_bullish": 1 if close > open_p else 0,
        "volume": volume,
    }

    if "vwap" in candle and candle["vwap"]:
        vwap = candle["vwap"]
        features["vwap_distance_pct"] = (close - vwap) / vwap * 100 if vwap != 0 else 0
    else:
        features["vwap_distance_pct"] = 0

    return features


def compute_volume_features(
    current_volume: int,
    volume_history: List[int],
) -> Dict[str, float]:
    if not volume_history:
        return {
            "vol_ratio": 1.0,
            "vol_slope_5": 0.0,
            "vol_above_avg": 0,
        }

    avg_vol = np.mean(volume_history)
    features = {
        "vol_ratio": current_volume / avg_vol if avg_vol > 0 else 1.0,
        "vol_above_avg": 1 if current_volume > avg_vol else 0,
    }

    if len(volume_history) >= 5:
        vol_series = list(volume_history[-5:]) + [current_volume]
        features["vol_slope_5"] = vol_series[-1] - vol_series[0]
    else:
        features["vol_slope_5"] = 0.0

    return features


def compute_iv_features(
    current_iv: Optional[float] = None,
    iv_ce: Optional[float] = None,
    iv_pe: Optional[float] = None,
    iv_history: Optional[List[float]] = None,
) -> Dict[str, Optional[float]]:
    features: Dict[str, Optional[float]] = {
        "iv_level": current_iv,
        "iv_ce_pe_spread": abs(iv_ce - iv_pe) if (iv_ce is not None and iv_pe is not None) else None,
    }

    if current_iv is not None:
        if iv_history and len(iv_history) >= 2:
            features["iv_change_1"] = current_iv - iv_history[-1]
        else:
            features["iv_change_1"] = 0.0

        if iv_history and len(iv_history) >= 5:
            features["iv_change_5"] = current_iv - iv_history[-5]
        else:
            features["iv_change_5"] = 0.0

        # Acceleration must include the current bar, so append it to the prior history
        # rather than differencing history against itself.
        if iv_history and len(iv_history) >= 2:
            series = list(iv_history) + [current_iv]
            changes = [series[i] - series[i - 1] for i in range(1, len(series))]
            features["iv_acceleration"] = changes[-1] - changes[-2] if len(changes) >= 2 else 0.0
        else:
            features["iv_acceleration"] = 0.0

        if iv_history and len(iv_history) >= 20:
            hv = np.array(iv_history)
            percentile = float((hv < current_iv).mean() * 100)
            features["iv_percentile"] = percentile
        else:
            features["iv_percentile"] = 50.0
    else:
        features["iv_change_1"] = None
        features["iv_change_5"] = None
        features["iv_acceleration"] = None
        features["iv_percentile"] = None

    return features


def compute_oi_features(
    atm_ce_oi: Optional[int] = None,
    atm_pe_oi: Optional[int] = None,
    total_ce_oi: Optional[int] = None,
    total_pe_oi: Optional[int] = None,
    ce_oi_change_pct: Optional[float] = None,
    pe_oi_change_pct: Optional[float] = None,
    all_ce_oi: Optional[List[int]] = None,
    all_pe_oi: Optional[List[int]] = None,
) -> Dict[str, Optional[float]]:
    features: Dict[str, Optional[float]] = {
        # None, not 1.0 — a missing chain must stay distinguishable from a genuinely
        # neutral PCR, otherwise a zeroed total reads as a real market reading.
        "pcr_oi": (
            total_pe_oi / total_ce_oi
            if (total_ce_oi is not None and total_pe_oi is not None and total_ce_oi > 0)
            else None
        ),
        "oi_bias": (
            ce_oi_change_pct - pe_oi_change_pct
            if (ce_oi_change_pct is not None and pe_oi_change_pct is not None)
            else None
        ),
        "atm_ce_oi_change_pct": ce_oi_change_pct,
        "atm_pe_oi_change_pct": pe_oi_change_pct,
    }

    if atm_ce_oi is not None and atm_pe_oi is not None:
        atm_total_oi = atm_ce_oi + atm_pe_oi
        features["atm_total_oi"] = atm_total_oi
    else:
        atm_total_oi = None
        features["atm_total_oi"] = None

    if atm_total_oi is not None and (all_ce_oi is not None or all_pe_oi is not None):
        all_total_oi = (all_ce_oi or []) + (all_pe_oi or [])
        total_oi = sum(all_total_oi) if all_total_oi else 0
        features["oi_concentration"] = atm_total_oi / total_oi if total_oi > 0 else 0.0
    else:
        features["oi_concentration"] = None

    # The chain's SHAPE, not just its sum. Without this the "wall = OI >= p85 of
    # strikes with non-zero OI" rule cannot be validated against history, and the
    # fact that `oi_wall_min_oi = 4_000_000` sits above the entire live chain for
    # most of a weekly cycle stays invisible. Sum alone hid both.
    for side, values in (("ce", all_ce_oi), ("pe", all_pe_oi)):
        if values is not None:
            live = [int(v) for v in values if v]
            features[f"strikes_with_{side}_oi"] = len(live)
            features[f"max_{side}_oi"] = max(live) if live else None
            features[f"p85_{side}_oi"] = _percentile_nearest_rank(live, 0.85)
        else:
            features[f"strikes_with_{side}_oi"] = None
            features[f"max_{side}_oi"] = None
            features[f"p85_{side}_oi"] = None

    return features


def _percentile_nearest_rank(values: List[int], q: float) -> Optional[int]:
    """Nearest-rank percentile — returns an OI level some strike actually has.

    Deliberately not interpolated: a wall threshold is compared against real
    per-strike OI, so a synthetic value between two strikes would qualify a wall
    that does not exist. Returns None on an empty chain so "no data" stays
    distinguishable from a genuine zero.
    """
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(q * len(ordered)))
    return ordered[rank - 1]


def compute_greek_features(
    atm_ce_gamma: Optional[float] = None,
    atm_pe_gamma: Optional[float] = None,
    atm_ce_theta: Optional[float] = None,
    atm_pe_theta: Optional[float] = None,
    atm_ce_vega: Optional[float] = None,
    atm_pe_vega: Optional[float] = None,
    spot: float = 0.0,
    atm_ce_delta: Optional[float] = None,  # CE delta ∈ [0, 1]; None = old row → NaN
    atm_pe_delta: Optional[float] = None,  # PE delta ∈ [-1, 0]; None = old row → NaN
) -> Dict[str, Optional[float]]:
    features: Dict[str, Optional[float]] = {}

    if (
        atm_ce_gamma is not None and atm_pe_gamma is not None and
        atm_ce_theta is not None and atm_pe_theta is not None and
        spot > 0
    ):
        total_gamma = atm_ce_gamma + atm_pe_gamma
        total_theta = abs(atm_ce_theta) + abs(atm_pe_theta)
        denom = total_theta / spot
        features["gamma_theta_ratio"] = total_gamma / denom if denom != 0 else 0.0
    else:
        features["gamma_theta_ratio"] = None

    if atm_ce_vega is not None and atm_pe_vega is not None:
        features["total_vega"] = atm_ce_vega + atm_pe_vega
    else:
        features["total_vega"] = None

    # net_delta > 0 = directional bias bullish; < 0 = bearish; ~0 = balanced.
    # CE delta is positive (0→1), PE delta is negative (-1→0), so:
    #   net_delta = ce_delta − |pe_delta| = ce_delta − abs(pe_delta)
    #             = ce_delta + pe_delta   (since pe_delta is already negative)
    # None is kept as None rather than coerced to 0.0 so that old ml_collection
    # rows (pre-delta) become NaN in the feature matrix via _numeric_only, which
    # XGBoost handles natively.  A zero delta is a real market state (exactly ATM).
    if atm_ce_delta is not None and atm_pe_delta is not None:
        features["net_delta"] = atm_ce_delta - abs(atm_pe_delta)
    else:
        features["net_delta"] = None

    return features



def compute_structure_features(
    spot: float,
    levels: List[float],
    full_chain: List[Dict[str, Any]],
    pdh: Optional[float] = None,
    pdl: Optional[float] = None,
) -> Dict[str, float]:
    features = {}

    # None, not 100.0 — a literal sentinel is indistinguishable from a real
    # 100-point distance, and 57% of collected rows carried it, so any model
    # would learn "distance == 100" as a genuine market state. dataset._numeric_only
    # drops None, landing it as NaN in the matrix, which XGBoost handles natively.
    resistances = sorted([lvl for lvl in levels if lvl > spot]) if levels else []
    supports = sorted([lvl for lvl in levels if lvl < spot], reverse=True) if levels else []
    features["dist_to_nearest_resistance"] = resistances[0] - spot if resistances else None
    features["dist_to_nearest_support"] = spot - supports[0] if supports else None

    features["dist_to_pdh"] = pdh - spot if pdh is not None else None
    features["dist_to_pdl"] = spot - pdl if pdl is not None else None

    return features


def compute_meta_features(
    timestamp,
    dte: Optional[int] = None,
    is_expiry: bool = False,
) -> Dict[str, float]:
    features = {
        "dte": float(dte) if dte is not None else 7.0,
        "is_expiry_day": 1 if is_expiry else 0,
    }

    minutes_since_open = 0
    if timestamp is not None:
        try:
            ts = timestamp
            if hasattr(ts, "tzinfo") and ts.tzinfo is not None:
                from datetime import timezone, timedelta
                ist = timezone(timedelta(hours=5, minutes=30))
                ts = ts.astimezone(ist)
            seconds = ts.hour * 3600 + ts.minute * 60
            session_start = 9 * 3600 + 15 * 60
            minutes_since_open = (seconds - session_start) / 60.0
        except Exception:
            minutes_since_open = 0

    features["minutes_since_open"] = max(0, minutes_since_open)

    if minutes_since_open <= 60:
        features["session_phase"] = 0
    elif minutes_since_open <= 4 * 60 + 45:
        features["session_phase"] = 1
    else:
        features["session_phase"] = 2

    return features


def build_feature_vector(
    candle: Dict[str, float],
    volume_history: List[int],
    iv_history: Optional[List[float]],
    atm_ce: Optional[Dict[str, Any]],
    atm_pe: Optional[Dict[str, Any]],
    total_ce_oi: Optional[int],
    total_pe_oi: Optional[int],
    all_ce_oi: Optional[List[int]],
    all_pe_oi: Optional[List[int]],
    levels: List[float],
    timestamp,
    spot: float,
    pdh: Optional[float] = None,
    pdl: Optional[float] = None,
    dte: Optional[int] = None,
    is_expiry: bool = False,
    config=None,
) -> Dict[str, float]:
    features = {}

    use = getattr(config, "use_candle_features", True)
    if use:
        candle_feats = compute_candle_features(candle)
        features.update({f"candle_features__{k}": v for k, v in candle_feats.items()})

    use = getattr(config, "use_volume_features", True)
    if use:
        vol_feats = compute_volume_features(candle.get("volume", 0), volume_history)
        features.update({f"volume_features__{k}": v for k, v in vol_feats.items()})

    use = getattr(config, "use_iv_features", True)
    if use:
        iv_ce = atm_ce.get("iv") if isinstance(atm_ce, dict) else None
        iv_pe = atm_pe.get("iv") if isinstance(atm_pe, dict) else None
        current_iv = iv_ce if iv_ce is not None else iv_pe
        iv_feats = compute_iv_features(
            current_iv=current_iv,
            iv_ce=iv_ce,
            iv_pe=iv_pe,
            iv_history=iv_history,
        )
        features.update({f"iv_features__{k}": v for k, v in iv_feats.items()})

    use = getattr(config, "use_oi_features", True)
    if use:
        atm_ce_oi = atm_ce.get("oi") if isinstance(atm_ce, dict) else None
        atm_pe_oi = atm_pe.get("oi") if isinstance(atm_pe, dict) else None
        ce_oi_change_pct = atm_ce.get("oi_change_pct") if isinstance(atm_ce, dict) else None
        pe_oi_change_pct = atm_pe.get("oi_change_pct") if isinstance(atm_pe, dict) else None

        oi_feats = compute_oi_features(
            atm_ce_oi=atm_ce_oi,
            atm_pe_oi=atm_pe_oi,
            total_ce_oi=total_ce_oi,
            total_pe_oi=total_pe_oi,
            ce_oi_change_pct=ce_oi_change_pct,
            pe_oi_change_pct=pe_oi_change_pct,
            all_ce_oi=all_ce_oi,
            all_pe_oi=all_pe_oi,
        )
        features.update({f"oi_features__{k}": v for k, v in oi_feats.items()})

    use = getattr(config, "use_greek_features", True)
    if use:
        atm_ce_gamma = atm_ce.get("gamma") if isinstance(atm_ce, dict) else None
        atm_pe_gamma = atm_pe.get("gamma") if isinstance(atm_pe, dict) else None
        atm_ce_theta = atm_ce.get("theta") if isinstance(atm_ce, dict) else None
        atm_pe_theta = atm_pe.get("theta") if isinstance(atm_pe, dict) else None
        atm_ce_vega = atm_ce.get("vega") if isinstance(atm_ce, dict) else None
        atm_pe_vega = atm_pe.get("vega") if isinstance(atm_pe, dict) else None
        atm_ce_delta = atm_ce.get("delta") if isinstance(atm_ce, dict) else None
        atm_pe_delta = atm_pe.get("delta") if isinstance(atm_pe, dict) else None

        greek_feats = compute_greek_features(
            atm_ce_gamma=atm_ce_gamma,
            atm_pe_gamma=atm_pe_gamma,
            atm_ce_theta=atm_ce_theta,
            atm_pe_theta=atm_pe_theta,
            atm_ce_vega=atm_ce_vega,
            atm_pe_vega=atm_pe_vega,
            spot=spot,
            atm_ce_delta=atm_ce_delta,
            atm_pe_delta=atm_pe_delta,
        )
        features.update({f"greek_features__{k}": v for k, v in greek_feats.items()})


    use = getattr(config, "use_structure_features", True)
    if use:
        struct_feats = compute_structure_features(
            spot=spot,
            levels=levels,
            full_chain=[],
            pdh=pdh,
            pdl=pdl,
        )
        features.update({f"structure_features__{k}": v for k, v in struct_feats.items()})

    use = getattr(config, "use_meta_features", True)
    if use:
        meta_feats = compute_meta_features(
            timestamp=timestamp,
            dte=dte,
            is_expiry=is_expiry,
        )
        features.update({f"meta_features__{k}": v for k, v in meta_feats.items()})

    # Explicit missingness presence indicators (MANM-154)
    # Aligns serving feature vector with offline dataset.flatten_features()
    features["structure__has_nearest_support"] = (
        1.0 if features.get("structure_features__dist_to_nearest_support") is not None else 0.0
    )
    features["structure__has_nearest_resistance"] = (
        1.0 if features.get("structure_features__dist_to_nearest_resistance") is not None else 0.0
    )
    features["greek__has_net_delta"] = (
        1.0 if features.get("greek_features__net_delta") is not None else 0.0
    )

    return features

