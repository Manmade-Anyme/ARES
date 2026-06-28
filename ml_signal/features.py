from typing import Dict, List, Optional, Any

import numpy as np
import pandas as pd


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
    current_iv: float,
    iv_ce: float,
    iv_pe: float,
    iv_history: Optional[List[float]] = None,
) -> Dict[str, float]:
    features = {
        "iv_level": current_iv,
        "iv_ce_pe_spread": abs(iv_ce - iv_pe),
    }

    if iv_history and len(iv_history) >= 2:
        features["iv_change_1"] = current_iv - iv_history[-1]
    else:
        features["iv_change_1"] = 0.0

    if iv_history and len(iv_history) >= 5:
        features["iv_change_5"] = current_iv - iv_history[-5]
    else:
        features["iv_change_5"] = 0.0

    if iv_history and len(iv_history) >= 2:
        prev_changes = [iv_history[i] - iv_history[i - 1] for i in range(1, len(iv_history))]
        features["iv_acceleration"] = prev_changes[-1] - prev_changes[-2] if len(prev_changes) >= 2 else 0.0
    else:
        features["iv_acceleration"] = 0.0

    if iv_history and len(iv_history) >= 20:
        hv = np.array(iv_history)
        percentile = (hv < current_iv).mean() * 100
        features["iv_percentile"] = percentile
    else:
        features["iv_percentile"] = 50.0

    return features


def compute_oi_features(
    atm_ce_oi: int,
    atm_pe_oi: int,
    total_ce_oi: int,
    total_pe_oi: int,
    ce_oi_change_pct: float,
    pe_oi_change_pct: float,
    all_ce_oi: Optional[List[int]] = None,
    all_pe_oi: Optional[List[int]] = None,
) -> Dict[str, float]:
    features = {
        "pcr_oi": total_pe_oi / total_ce_oi if total_ce_oi > 0 else 1.0,
        "oi_bias": ce_oi_change_pct - pe_oi_change_pct,
        "atm_ce_oi_change_pct": ce_oi_change_pct,
        "atm_pe_oi_change_pct": pe_oi_change_pct,
    }

    atm_total_oi = atm_ce_oi + atm_pe_oi
    all_total_oi = (all_ce_oi or [0]) + (all_pe_oi or [0])
    total_oi = sum(all_total_oi) if all_total_oi else 1
    features["oi_concentration"] = atm_total_oi / total_oi if total_oi > 0 else 0
    features["atm_total_oi"] = atm_total_oi

    return features


def compute_greek_features(
    atm_ce_gamma: float,
    atm_pe_gamma: float,
    atm_ce_theta: float,
    atm_pe_theta: float,
    atm_ce_vega: float,
    atm_pe_vega: float,
    spot: float,
) -> Dict[str, float]:
    total_gamma = atm_ce_gamma + atm_pe_gamma
    total_theta = abs(atm_ce_theta) + abs(atm_pe_theta)
    total_vega = atm_ce_vega + atm_pe_vega

    features = {
        "gamma_theta_ratio": total_gamma / (total_theta / spot) if (total_theta / spot) != 0 else 0,
        "total_vega": total_vega,
    }

    return features


def compute_structure_features(
    spot: float,
    levels: List[float],
    full_chain: List[Dict[str, Any]],
    pdh: Optional[float] = None,
    pdl: Optional[float] = None,
) -> Dict[str, float]:
    features = {}

    if levels:
        resistances = sorted([l for l in levels if l > spot])
        supports = sorted([l for l in levels if l < spot], reverse=True)
        features["dist_to_nearest_resistance"] = resistances[0] - spot if resistances else 100.0
        features["dist_to_nearest_support"] = spot - supports[0] if supports else 100.0
    else:
        features["dist_to_nearest_resistance"] = 100.0
        features["dist_to_nearest_support"] = 100.0

    if pdh is not None:
        features["dist_to_pdh"] = pdh - spot
    else:
        features["dist_to_pdh"] = 100.0
    if pdl is not None:
        features["dist_to_pdl"] = spot - pdl
    else:
        features["dist_to_pdl"] = 100.0

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
            seconds = timestamp.hour * 3600 + timestamp.minute * 60
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
    atm_ce: Dict[str, Any],
    atm_pe: Dict[str, Any],
    total_ce_oi: int,
    total_pe_oi: int,
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
        features.update(candle_feats)

    use = getattr(config, "use_volume_features", True)
    if use:
        vol_feats = compute_volume_features(candle.get("volume", 0), volume_history)
        features.update(vol_feats)

    use = getattr(config, "use_iv_features", True)
    if use:
        iv_feats = compute_iv_features(
            current_iv=atm_ce.get("iv", 0),
            iv_ce=atm_ce.get("iv", 0),
            iv_pe=atm_pe.get("iv", 0),
            iv_history=iv_history,
        )
        features.update(iv_feats)

    use = getattr(config, "use_oi_features", True)
    if use:
        oi_feats = compute_oi_features(
            atm_ce_oi=atm_ce.get("oi", 0),
            atm_pe_oi=atm_pe.get("oi", 0),
            total_ce_oi=total_ce_oi,
            total_pe_oi=total_pe_oi,
            ce_oi_change_pct=atm_ce.get("oi_change_pct", 0),
            pe_oi_change_pct=atm_pe.get("oi_change_pct", 0),
            all_ce_oi=all_ce_oi,
            all_pe_oi=all_pe_oi,
        )
        features.update(oi_feats)

    use = getattr(config, "use_greek_features", True)
    if use:
        greek_feats = compute_greek_features(
            atm_ce_gamma=atm_ce.get("gamma", 0),
            atm_pe_gamma=atm_pe.get("gamma", 0),
            atm_ce_theta=atm_ce.get("theta", 0),
            atm_pe_theta=atm_pe.get("theta", 0),
            atm_ce_vega=atm_ce.get("vega", 0),
            atm_pe_vega=atm_pe.get("vega", 0),
            spot=spot,
        )
        features.update(greek_feats)

    use = getattr(config, "use_structure_features", True)
    if use:
        struct_feats = compute_structure_features(
            spot=spot,
            levels=levels,
            full_chain=[],
            pdh=pdh,
            pdl=pdl,
        )
        features.update(struct_feats)

    use = getattr(config, "use_meta_features", True)
    if use:
        meta_feats = compute_meta_features(
            timestamp=timestamp,
            dte=dte,
            is_expiry=is_expiry,
        )
        features.update(meta_feats)

    return features
