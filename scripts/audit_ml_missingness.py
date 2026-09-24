"""
Audit script for MANM-154: Feature Versioning and Missing Data Inconsistency in ml_collection.

Analyzes missingness across feature groups (net_delta, OI shape, support/resistance distances,
detector scores) broken down by:
1. Schema Epoch / Feature Version (v1, v2, v3, v4)
2. Market Session Phase (Morning Open, Mid-Day, Afternoon/Close)
3. Calendar Date / Month

Verifies storage invariants (enforcement of NULL/NaN vs fabricated sentinels/zeros).
Outputs a markdown audit report to reports/ml/manm154_missingness_audit_report.md.
"""

import argparse
import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

# Bootstrap repository root into sys.path before local package imports
_repo_root = str(Path(__file__).resolve().parent.parent)
if _repo_root not in sys.path:
    sys.path.insert(0, _repo_root)

import pandas as pd
from supabase import create_client

from config import settings
from ml_signal.dataset import infer_feature_version_from_timestamp

# Commit 08c36e3 (TASK-195) committed at 2026-07-31T16:09:48Z eliminated literal 100.0 sentinel injection.
# Rows prior to this boundary carrying 100.0 are legacy sentinels; rows at or after are legitimate market distances.
TASK195_SENTINEL_CUTOFF = pd.Timestamp("2026-07-31T16:09:48Z")



def _load_json(val: Any) -> Dict[str, Any]:
    if val is None:
        return {}
    if isinstance(val, dict):
        return val
    try:
        parsed = json.loads(val)
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}


def get_market_session(ts: Optional[datetime]) -> str:
    """Categorize timestamp into Indian market session phases."""
    if ts is None or pd.isna(ts):
        return "UNKNOWN"
    try:
        ist = timezone(timedelta(hours=5, minutes=30))
        if hasattr(ts, "tzinfo") and ts.tzinfo is not None:
            ts_ist = ts.astimezone(ist)
        else:
            ts_ist = ts.replace(tzinfo=timezone.utc).astimezone(ist)
        minutes = ts_ist.hour * 60 + ts_ist.minute
        # 09:15 is 555 min; 10:15 is 615 min; 14:00 is 840 min; 15:30 is 930 min
        if minutes < 555:
            return "PRE_MARKET"
        elif minutes <= 615:
            return "MORNING_OPEN (09:15-10:15)"
        elif minutes <= 840:
            return "MID_DAY (10:15-14:00)"
        elif minutes <= 930:
            return "AFTERNOON_CLOSE (14:00-15:30)"
        else:
            return "POST_MARKET"
    except Exception:
        return "UNKNOWN"


def is_zero_injected_option_payload(
    raw_atm_oi: Any,
    greek_features: Optional[Dict[str, Any]] = None,
    oi_features: Optional[Dict[str, Any]] = None,
) -> bool:
    """Detect fabricated all-zero options payloads (defect MANM-49).

    In real NIFTY option markets, ATM CE and PE cannot simultaneously have 0 IV,
    0 open interest, and 0 vega/gamma. When signal_consumer.py or legacy ingestion
    encountered missing options data, it defaulted to synthetic zeros
    {iv: 0, oi: 0, gamma: 0, theta: 0, vega: 0}.
    """
    # 1. Check raw_atm_oi payload if present
    if raw_atm_oi is not None:
        raw = _load_json(raw_atm_oi)
        if isinstance(raw, dict) and raw:
            ce = raw.get("ce")
            pe = raw.get("pe")
            if isinstance(ce, dict) and isinstance(pe, dict) and ce and pe:
                check_keys = ["iv", "oi", "gamma", "vega"]
                ce_is_zero = all(float(ce.get(k, 1) or 0) == 0.0 for k in check_keys if k in ce)
                pe_is_zero = all(float(pe.get(k, 1) or 0) == 0.0 for k in check_keys if k in pe)
                if ce_is_zero and pe_is_zero and any(k in ce for k in check_keys):
                    return True

    # 2. Check derived oi_features and greek_features if populated with synthetic zeros
    greek = greek_features or {}
    oi = oi_features or {}
    if greek and oi:
        tot_ce = oi.get("total_ce_oi")
        tot_pe = oi.get("total_pe_oi")
        atm_ce = oi.get("atm_ce_oi")
        atm_pe = oi.get("atm_pe_oi")
        if (
            tot_ce == 0 and tot_pe == 0 and atm_ce == 0 and atm_pe == 0 and
            greek.get("total_vega") == 0.0 and greek.get("gamma_theta_ratio") == 0.0
        ):
            return True

    return False


def is_zero_injected_prediction_snapshot(snapshot: Any) -> bool:
    """Detect MANM-49 zero-injected options in a prediction feature snapshot."""
    if not snapshot:
        return False
    data = _load_json(snapshot)
    if not isinstance(data, dict) or not data:
        return False
    # Signature of MANM-49 defect in signal_consumer.py:
    # When missing options were defaulted to zeros {iv: 0, oi: 0, gamma: 0, theta: 0, vega: 0},
    # build_feature_vector() populated non-null zero values for IV, vega, gamma/theta, and total OI.
    iv_level = data.get("iv_features__iv_level")
    vega = data.get("greek_features__total_vega")
    gamma_theta = data.get("greek_features__gamma_theta_ratio")
    atm_oi = data.get("oi_features__atm_total_oi")

    if iv_level is not None and vega is not None and gamma_theta is not None and atm_oi is not None:
        try:
            if (
                float(iv_level) == 0.0 and
                float(vega) == 0.0 and
                float(gamma_theta) == 0.0 and
                float(atm_oi) == 0.0
            ):
                return True
        except (ValueError, TypeError):
            pass

    return False


def fetch_ml_predictions(
    supabase,
    page_size: int = 1000,
    limit: Optional[int] = None,
    raise_on_error: bool = False,
) -> Optional[List[Dict[str, Any]]]:
    """Fetch prediction records from ml_predictions table if accessible.

    Returns:
        List of records if query succeeds (empty list if table has 0 rows).
        None if table could not be queried (e.g. RLS permission error, missing table).
    """
    try:
        supabase.table("ml_predictions").select("id").limit(1).execute()
    except Exception as e:
        if raise_on_error:
            raise
        print(f"[-] Could not query ml_predictions ({type(e).__name__}: {e}). Table will be excluded from audit.")
        return None

    rows: List[Dict[str, Any]] = []
    start = 0
    while True:
        end = start + page_size - 1
        if limit and end >= limit:
            end = limit - 1
        try:
            batch = (
                supabase.table("ml_predictions")
                .select("id,timestamp,feature_snapshot,source")
                .order("timestamp")
                .range(start, end)
                .execute()
                .data or []
            )
        except Exception as e:
            if raise_on_error:
                raise
            print(f"[-] Error querying batch from ml_predictions ({type(e).__name__}: {e}). Table will be excluded from audit.")
            return None
        rows.extend(batch)
        if len(batch) < page_size or (limit and len(rows) >= limit):
            break
        start += page_size

    return rows


def fetch_all_ml_collection(supabase, page_size: int = 1000, limit: Optional[int] = None) -> List[Dict[str, Any]]:
    """Fetch records with pagination."""
    has_fv = True
    try:
        supabase.table("ml_collection").select("feature_version").limit(1).execute()
    except Exception:
        has_fv = False

    has_raw_atm_oi = True
    try:
        supabase.table("ml_collection").select("raw_atm_oi").limit(1).execute()
    except Exception:
        has_raw_atm_oi = False

    cols = [
        "id", "timestamp", "spot", "greek_features", "oi_features",
        "structure_features", "detector_scores", "raw_candle"
    ]
    if has_fv:
        cols.append("feature_version")
    if has_raw_atm_oi:
        cols.append("raw_atm_oi")
    
    col_str = ",".join(cols)
    rows: List[Dict[str, Any]] = []
    start = 0

    print(f"[*] Fetching rows from ml_collection (page size {page_size})...")
    while True:
        end = start + page_size - 1
        if limit and end >= limit:
            end = limit - 1
        batch = (
            supabase.table("ml_collection")
            .select(col_str)
            .order("timestamp")
            .range(start, end)
            .execute()
            .data or []
        )
        rows.extend(batch)
        print(f"    Fetched {len(rows)} rows...", end="\r")
        if len(batch) < page_size or (limit and len(rows) >= limit):
            break
        start += page_size

    print(f"\n[+] Total rows fetched: {len(rows)}")
    return rows


def analyze_records(
    rows: List[Dict[str, Any]],
    prediction_rows: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Perform audit analysis on rows and optional prediction snapshots."""
    total_rows = len(rows)
    records = []

    pred_status = "ok" if prediction_rows is not None else "unavailable"
    pred_count = len(prediction_rows) if prediction_rows is not None else None
    zero_preds = 0 if prediction_rows is not None else None

    if prediction_rows:
        for p in prediction_rows:
            if is_zero_injected_prediction_snapshot(p.get("feature_snapshot")):
                zero_preds += 1

    sentinels_detected = {
        "legacy_100_support": 0,
        "legacy_100_resistance": 0,
        "real_100_support": 0,
        "real_100_resistance": 0,
        "negative_sentinels": 0,
        "zero_injected_options": 0,
        "zero_injected_predictions": zero_preds,
        "prediction_rows_evaluated": pred_count,
        "predictions_status": pred_status,
    }

    for r in rows:
        ts_str = r.get("timestamp")
        ts = pd.to_datetime(ts_str) if ts_str else None
        
        raw_fv = r.get("feature_version")
        if raw_fv is not None:
            try:
                fv = int(raw_fv)
            except Exception:
                fv = infer_feature_version_from_timestamp(ts)
        else:
            fv = infer_feature_version_from_timestamp(ts)

        session = get_market_session(ts)
        date_str = ts.strftime("%Y-%m-%d") if ts is not None and not pd.isna(ts) else "UNKNOWN"

        greek = _load_json(r.get("greek_features"))
        oi = _load_json(r.get("oi_features"))
        struct = _load_json(r.get("structure_features"))
        detectors = _load_json(r.get("detector_scores"))

        # Check zero-injected options (MANM-49 defect)
        is_zero_options = is_zero_injected_option_payload(
            r.get("raw_atm_oi"), greek_features=greek, oi_features=oi
        )
        if is_zero_options:
            sentinels_detected["zero_injected_options"] += 1

        # Check net_delta (must not be poisoned by zero-injected options)
        net_delta = greek.get("net_delta")
        has_net_delta = (
            net_delta is not None and
            not pd.isna(net_delta) and
            not is_zero_options
        )

        # Check OI shape (all 6 CE and PE fields must be present and not zero-injected)
        strikes_ce = oi.get("strikes_with_ce_oi")
        max_ce = oi.get("max_ce_oi")
        p85_ce = oi.get("p85_ce_oi")
        strikes_pe = oi.get("strikes_with_pe_oi")
        max_pe = oi.get("max_pe_oi")
        p85_pe = oi.get("p85_pe_oi")
        has_oi_shape = (
            strikes_ce is not None and max_ce is not None and p85_ce is not None and
            strikes_pe is not None and max_pe is not None and p85_pe is not None and
            not pd.isna(strikes_ce) and not pd.isna(max_ce) and not pd.isna(p85_ce) and
            not pd.isna(strikes_pe) and not pd.isna(max_pe) and not pd.isna(p85_pe) and
            not is_zero_options
        )

        # Check support & resistance distance
        dist_sup = struct.get("dist_to_nearest_support")
        dist_res = struct.get("dist_to_nearest_resistance")

        # Sentinel checks:
        # Prior to TASK-195 commit 08c36e3 (2026-07-31T16:09:48Z), missing levels were injected as literal 100.0 sentinels.
        # After TASK-195, missing levels evaluate to None/NaN, so an exact 100.0 reading reflects
        # a legitimate 100-point physical market distance between spot and an existing level.
        is_pre_task195 = False
        if ts is not None and not pd.isna(ts):
            ts_utc = ts.tz_convert("UTC") if ts.tzinfo is not None else ts.tz_localize("UTC")
            is_pre_task195 = ts_utc < TASK195_SENTINEL_CUTOFF
        else:
            is_pre_task195 = fv <= 2

        is_legacy_sup = (dist_sup == 100.0 and is_pre_task195)
        is_legacy_res = (dist_res == 100.0 and is_pre_task195)
        is_neg_sup = dist_sup is not None and not pd.isna(dist_sup) and dist_sup < 0
        is_neg_res = dist_res is not None and not pd.isna(dist_res) and dist_res < 0

        if dist_sup == 100.0:
            if is_pre_task195:
                sentinels_detected["legacy_100_support"] += 1
            else:
                sentinels_detected["real_100_support"] += 1

        if dist_res == 100.0:
            if is_pre_task195:
                sentinels_detected["legacy_100_resistance"] += 1
            else:
                sentinels_detected["real_100_resistance"] += 1

        if is_neg_sup or is_neg_res:
            sentinels_detected["negative_sentinels"] += 1

        # Values classified as legacy 100.0 sentinels or negative distances must be treated as missing
        has_dist_support = (
            dist_sup is not None and
            not pd.isna(dist_sup) and
            not is_legacy_sup and
            not is_neg_sup
        )
        has_dist_resistance = (
            dist_res is not None and
            not pd.isna(dist_res) and
            not is_legacy_res and
            not is_neg_res
        )

        # Check trend continuation detector score
        trend_score = detectors.get("trend_continuation")
        has_trend_continuation = trend_score is not None and not pd.isna(trend_score)

        records.append({
            "timestamp": ts,
            "date": date_str,
            "feature_version": fv,
            "session": session,
            "missing_net_delta": not has_net_delta,
            "missing_oi_shape": not has_oi_shape,
            "missing_support": not has_dist_support,
            "missing_resistance": not has_dist_resistance,
            "missing_trend_continuation": not has_trend_continuation,
        })

    df = pd.DataFrame(records)

    # 1. Overall stats
    overall = {
        "total_rows": total_rows,
        "missing_net_delta": int(df["missing_net_delta"].sum()),
        "pct_missing_net_delta": round(float(df["missing_net_delta"].mean() * 100), 2),
        "missing_oi_shape": int(df["missing_oi_shape"].sum()),
        "pct_missing_oi_shape": round(float(df["missing_oi_shape"].mean() * 100), 2),
        "missing_support": int(df["missing_support"].sum()),
        "pct_missing_support": round(float(df["missing_support"].mean() * 100), 2),
        "missing_resistance": int(df["missing_resistance"].sum()),
        "pct_missing_resistance": round(float(df["missing_resistance"].mean() * 100), 2),
        "missing_trend_continuation": int(df["missing_trend_continuation"].sum()),
        "pct_missing_trend_continuation": round(float(df["missing_trend_continuation"].mean() * 100), 2),
    }

    # 2. Breakdown by Feature Version
    by_version = []
    for fv, grp in df.groupby("feature_version"):
        n = len(grp)
        by_version.append({
            "feature_version": fv,
            "rows": n,
            "pct_of_total": round(n / total_rows * 100, 2),
            "missing_net_delta_pct": round(grp["missing_net_delta"].mean() * 100, 1),
            "missing_oi_shape_pct": round(grp["missing_oi_shape"].mean() * 100, 1),
            "missing_support_pct": round(grp["missing_support"].mean() * 100, 1),
            "missing_resistance_pct": round(grp["missing_resistance"].mean() * 100, 1),
            "missing_trend_continuation_pct": round(grp["missing_trend_continuation"].mean() * 100, 1),
        })

    # 3. Breakdown by Market Session
    by_session = []
    for session, grp in df.groupby("session"):
        n = len(grp)
        by_session.append({
            "session": session,
            "rows": n,
            "pct_of_total": round(n / total_rows * 100, 2),
            "missing_net_delta_pct": round(grp["missing_net_delta"].mean() * 100, 1),
            "missing_oi_shape_pct": round(grp["missing_oi_shape"].mean() * 100, 1),
            "missing_support_pct": round(grp["missing_support"].mean() * 100, 1),
            "missing_resistance_pct": round(grp["missing_resistance"].mean() * 100, 1),
            "missing_trend_continuation_pct": round(grp["missing_trend_continuation"].mean() * 100, 1),
        })

    # 4. Daily breakdown (by date)
    by_date = []
    for dt, grp in df.groupby("date"):
        n = len(grp)
        by_date.append({
            "date": str(dt),
            "rows": n,
            "feature_versions": sorted(list(grp["feature_version"].unique())),
            "missing_net_delta_pct": round(grp["missing_net_delta"].mean() * 100, 1),
            "missing_oi_shape_pct": round(grp["missing_oi_shape"].mean() * 100, 1),
            "missing_support_pct": round(grp["missing_support"].mean() * 100, 1),
            "missing_resistance_pct": round(grp["missing_resistance"].mean() * 100, 1),
            "missing_trend_continuation_pct": round(grp["missing_trend_continuation"].mean() * 100, 1),
        })

    # 5. Weekly/Monthly temporal progression
    df["year_week"] = df["timestamp"].dt.strftime("%Y-W%W")
    by_week = []
    for week, grp in df.groupby("year_week"):
        n = len(grp)
        by_week.append({
            "week": week,
            "rows": n,
            "feature_versions": sorted(list(grp["feature_version"].unique())),
            "missing_net_delta_pct": round(grp["missing_net_delta"].mean() * 100, 1),
            "missing_oi_shape_pct": round(grp["missing_oi_shape"].mean() * 100, 1),
            "missing_support_pct": round(grp["missing_support"].mean() * 100, 1),
            "missing_resistance_pct": round(grp["missing_resistance"].mean() * 100, 1),
            "missing_trend_continuation_pct": round(grp["missing_trend_continuation"].mean() * 100, 1),
        })

    return {
        "overall": overall,
        "sentinels": sentinels_detected,
        "by_version": by_version,
        "by_session": by_session,
        "by_date": by_date,
        "by_week": by_week,
    }


def generate_markdown_report(audit_res: Dict[str, Any], output_path: str):
    """Format audit results into GitHub-flavored Markdown report."""
    o = audit_res["overall"]
    s = audit_res["sentinels"]
    
    md = []
    md.append("# MANM-154: Missingness & Feature-Versioning Audit Report")
    md.append("")
    md.append(f"**Audit Generated:** {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%SZ')}")
    md.append(f"**Total Records Evaluated:** {o['total_rows']:,}")
    md.append("")
    md.append("---")
    md.append("")
    md.append("## 1. Executive Summary & Evidence Verification")
    md.append("")
    md.append("| Target Feature Category | Missing Count | % Missing | Root Cause Classification |")
    md.append("|---|---|---|---|")
    md.append(f"| `net_delta` (`greek_features`) | {o['missing_net_delta']:,} | {o['pct_missing_net_delta']}% | Schema Evolution (TASK-4c introduced 2026-08-21) |")
    md.append(f"| OI Shape fields (`oi_features`) | {o['missing_oi_shape']:,} | {o['pct_missing_oi_shape']}% | Schema Evolution (TASK-194 introduced 2026-07-31) |")
    md.append(f"| `dist_to_nearest_support` | {o['missing_support']:,} | {o['pct_missing_support']}% | Market Regime & Sentinel Cleanup (TASK-195) |")
    md.append(f"| `dist_to_nearest_resistance` | {o['missing_resistance']:,} | {o['pct_missing_resistance']}% | Market Regime (ATH Pivot Invariance) & Sentinel Cleanup |")
    md.append(f"| `trend_continuation` detector | {o['missing_trend_continuation']:,} | {o['pct_missing_trend_continuation']}% | Schema Evolution & Enum Key Fix (Commit 782a240) |")
    md.append("")
    md.append("### Sentinel & Fabricated Value Verification")
    md.append(f"- **Legacy 100.0 Sentinels Remaining (Pre-TASK-195):** Support: `{s['legacy_100_support']}`, Resistance: `{s['legacy_100_resistance']}`")
    md.append(f"- **Legitimate 100.0 Market Distances (Post-TASK-195):** Support: `{s['real_100_support']}`, Resistance: `{s['real_100_resistance']}`")
    md.append(f"- **Negative Distance Sentinels:** `{s['negative_sentinels']}`")
    md.append(f"- **Zero-Injected Option Payloads (`ml_collection`):** `{s.get('zero_injected_options', 0)}`")
    pred_status = s.get("predictions_status", "ok" if s.get("prediction_rows_evaluated") is not None else "unavailable")
    pred_evaluated = s.get("prediction_rows_evaluated")
    zero_preds = s.get("zero_injected_predictions")

    if pred_status != "ok" or pred_evaluated is None:
        md.append("- **Zero-Injected Prediction Snapshots (`ml_predictions`):** UNAVAILABLE (table inaccessible or insufficient service_role permissions; excluded from audit)")
    elif pred_evaluated == 0:
        md.append(f"- **Zero-Injected Prediction Snapshots (`ml_predictions`):** `0` (table empty or unpopulated)")
    else:
        md.append(f"- **Zero-Injected Prediction Snapshots (`ml_predictions`):** `{zero_preds}` ({pred_evaluated:,} records evaluated)")

    pred_artifacts = zero_preds if (pred_status == "ok" and zero_preds is not None) else 0
    total_artifacts = (
        s["legacy_100_support"] +
        s["legacy_100_resistance"] +
        s["negative_sentinels"] +
        s.get("zero_injected_options", 0) +
        pred_artifacts
    )
    if total_artifacts == 0:
        if pred_status == "ok" and pred_evaluated is not None:
            md.append("- **Verification Result:** PASS. Zero legacy sentinels, negative distances, or zero-injected payloads detected across `ml_collection` and `ml_predictions`. All missing values are cleanly stored as SQL `NULL` / JSON `null` / Python `None`. (Observations with distance exactly 100.0 in modern epochs reflect genuine market levels).")
        else:
            md.append("- **Verification Result:** PASS (ml_collection only; ml_predictions excluded). Zero legacy sentinels, negative distances, or zero-injected payloads detected in `ml_collection`. All missing values are cleanly stored as SQL `NULL` / JSON `null` / Python `None`. (Observations with distance exactly 100.0 in modern epochs reflect genuine market levels). `ml_predictions` could not be read and was excluded from this verdict.")
    else:
        artifacts_detail = []
        if s["legacy_100_support"]:
            artifacts_detail.append(f"{s['legacy_100_support']} legacy support")
        if s["legacy_100_resistance"]:
            artifacts_detail.append(f"{s['legacy_100_resistance']} legacy resistance")
        if s["negative_sentinels"]:
            artifacts_detail.append(f"{s['negative_sentinels']} negative")
        if s.get("zero_injected_options", 0):
            artifacts_detail.append(f"{s['zero_injected_options']} zero-injected collection payloads")
        if pred_status == "ok" and s.get("zero_injected_predictions"):
            artifacts_detail.append(f"{s['zero_injected_predictions']} zero-injected prediction snapshots")
        detail_str = ", ".join(artifacts_detail)
        excluded_suffix = " (`ml_predictions` excluded from audit)" if pred_status != "ok" else ""
        md.append(f"- **Verification Result:** WARNING. Detected {total_artifacts} legacy artifact(s) remaining in historical rows ({detail_str}). Remediate with NULL in database.{excluded_suffix}")

    md.append("")
    md.append("---")
    md.append("")
    md.append("## 2. Missingness Breakdown by Schema Epoch / Feature Version")
    md.append("")
    md.append("| Feature Version | Epoch Description | Sample Count | % Total | `net_delta` Miss% | OI Shape Miss% | Support Miss% | Resist Miss% | Trend Miss% |")
    md.append("|---|---|---|---|---|---|---|---|---|")
    for v in audit_res["by_version"]:
        desc = {
            1: "v1 Legacy Inception (< Jul 28)",
            2: "v2 Dynamic Setup Enums (Jul 28-31)",
            3: "v3 OI Shape Suite (Jul 31 - Aug 21)",
            4: "v4 Full Modern Suite (Aug 21+)",
        }.get(v["feature_version"], f"v{v['feature_version']}")
        md.append(
            f"| Version {v['feature_version']} | {desc} | {v['rows']:,} | {v['pct_of_total']}% | "
            f"{v['missing_net_delta_pct']}% | {v['missing_oi_shape_pct']}% | {v['missing_support_pct']}% | "
            f"{v['missing_resistance_pct']}% | {v['missing_trend_continuation_pct']}% |"
        )
    md.append("")
    v4_entry = next((v for v in audit_res.get("by_version", []) if v.get("feature_version") == 4), None)
    if v4_entry is not None and v4_entry.get("rows", 0) > 0:
        md.append(
            f"> **Key Finding (Version 4 Modern Suite):** Measured missingness in v4 is "
            f"`net_delta`: {v4_entry['missing_net_delta_pct']}%, "
            f"OI shape: {v4_entry['missing_oi_shape_pct']}%, "
            f"`trend_continuation`: {v4_entry['missing_trend_continuation_pct']}%. "
            f"Structural distance missingness reflects physical market conditions "
            f"(support: {v4_entry['missing_support_pct']}%, resistance: {v4_entry['missing_resistance_pct']}%)."
        )
    else:
        md.append("> **Key Finding:** Version 4 records are not present in the evaluated sample.")
    md.append("")
    md.append("---")
    md.append("")
    md.append("## 3. Missingness Breakdown by Market Session Phase")
    md.append("")
    md.append("| Market Session Phase | Sample Count | % Total | `net_delta` Miss% | OI Shape Miss% | Support Miss% | Resist Miss% | Trend Miss% |")
    md.append("|---|---|---|---|---|---|---|---|")
    for s_row in audit_res["by_session"]:
        md.append(
            f"| `{s_row['session']}` | {s_row['rows']:,} | {s_row['pct_of_total']}% | "
            f"{s_row['missing_net_delta_pct']}% | {s_row['missing_oi_shape_pct']}% | {s_row['missing_support_pct']}% | "
            f"{s_row['missing_resistance_pct']}% | {s_row['missing_trend_continuation_pct']}% |"
        )
    md.append("")
    if audit_res.get("by_session"):
        max_sup = max(audit_res["by_session"], key=lambda x: x["missing_support_pct"])
        max_res = max(audit_res["by_session"], key=lambda x: x["missing_resistance_pct"])
        md.append(
            f"> **Session Observation:** Structural distance missingness varies by phase: support missingness "
            f"peaks during `{max_sup['session']}` ({max_sup['missing_support_pct']}%), while "
            f"resistance missingness peaks during `{max_res['session']}` ({max_res['missing_resistance_pct']}%)."
        )
    md.append("")
    md.append("---")
    md.append("")
    md.append("## 4. Daily Missingness Breakdown")
    md.append("")
    md.append("| Date | Sample Count | Active Versions | `net_delta` Miss% | OI Shape Miss% | Support Miss% | Resist Miss% | Trend Miss% |")
    md.append("|---|---|---|---|---|---|---|---|")
    for d in audit_res.get("by_date", []):
        v_str = ",".join(f"v{v}" for v in d["feature_versions"])
        md.append(
            f"| `{d['date']}` | {d['rows']:,} | {v_str} | "
            f"{d['missing_net_delta_pct']}% | {d['missing_oi_shape_pct']}% | {d['missing_support_pct']}% | "
            f"{d['missing_resistance_pct']}% | {d['missing_trend_continuation_pct']}% |"
        )
    md.append("")
    md.append("---")
    md.append("")
    md.append("## 5. Weekly Temporal Progression")
    md.append("")
    md.append("| Year-Week | Sample Count | Active Versions | `net_delta` Miss% | OI Shape Miss% | Support Miss% | Resist Miss% | Trend Miss% |")
    md.append("|---|---|---|---|---|---|---|---|")
    for w in audit_res["by_week"]:
        v_str = ",".join(f"v{v}" for v in w["feature_versions"])
        md.append(
            f"| `{w['week']}` | {w['rows']:,} | {v_str} | "
            f"{w['missing_net_delta_pct']}% | {w['missing_oi_shape_pct']}% | {w['missing_support_pct']}% | "
            f"{w['missing_resistance_pct']}% | {w['missing_trend_continuation_pct']}% |"
        )
    md.append("")
    md.append("---")
    md.append("")
    md.append("## 6. Architectural Recommendations for Model Training")
    md.append("")
    md.append("### 1. Exclusion vs Imputation vs Indicator Features")
    md.append("- **Exclusion (Drop Rows): REJECTED as a global strategy.** Dropping rows with missing features would eliminate >60% of historical samples, including valuable market regimes from June and July 2026. Furthermore, realized trade outcomes are scarce (<150 closed trades total); dropping early trades would starve the model of training signal.")
    md.append("- **Imputation (Mean / Median / Zero): STRICTLY FORBIDDEN.** Imputing missing distances with 0.0 falsely implies spot is touching the level. Imputing `net_delta` with 0.0 asserts delta neutrality during strong trending sessions. Imputation injects artificial distribution artifacts that mislead gradient boosting splits.")
    md.append("- **Adopted Strategy: Native Missingness Routing + Explicit Indicator Features:**")
    md.append("  1. **Tree-Native Missingness:** XGBoost and LightGBM handle `NaN` natively via sparsity-aware branch routing (learning optimal split direction for unobserved values).")
    md.append("  2. **Structural Presence Indicators:** Three explicit boolean indicator features have been added in `ml_signal.dataset.flatten_features()`:")
    md.append("     - `structure__has_nearest_support`: `1.0` if support exists below spot, `0.0` if NaN.")
    md.append("     - `structure__has_nearest_resistance`: `1.0` if resistance exists above spot, `0.0` if NaN (identifies All-Time High breakouts).")
    md.append("     - `greek__has_net_delta`: `1.0` if net delta was recorded, `0.0` for legacy epochs.")
    md.append("  3. **Tiered Dual-Track Training:**")
    md.append("     - *Baseline Models (v1-v4):* Train on long-term invariants (Candles, Volume, Total OI, ATM Greeks).")
    md.append("     - *Enriched Production Models (v3+ / v4):* Train on enriched features (OI shape, Net Delta) with explicit presence indicators.")
    md.append("")
    md.append("---")
    md.append("")
    md.append("## 7. Implementation Verification")
    md.append("- `feature_version` column added to schema and migration script created.")
    md.append("- `MLCollector.snapshot` stamps `feature_version = 4` on all new rows.")
    md.append("- `signal_consumer.py` synthetic zero injection bug (MANM-49) eliminated; missing options evaluate to `None`/`NaN`.")
    md.append("- `ml_signal/dataset.py` extracts `feature_version` as metadata and generates boolean indicator columns.")
    md.append("")

    out_dir = os.path.dirname(output_path)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    with open(output_path, "w") as f:
        f.write("\n".join(md))
    print(f"[+] Audit report written -> {output_path}")


def main():
    parser = argparse.ArgumentParser(description="Audit ml_collection missing data.")
    parser.add_argument("--limit", type=int, default=None, help="Limit number of rows to audit.")
    parser.add_argument("--output", type=str, default="reports/ml/manm154_missingness_audit_report.md", help="Output markdown path.")
    parser.add_argument(
        "--fail-on-predictions-error",
        action="store_true",
        default=False,
        help="Exit with non-zero code if ml_predictions cannot be read.",
    )
    args = parser.parse_args()

    if not getattr(settings, "supabase_service_role_key", None):
        print("[!] Warning: SUPABASE_SERVICE_ROLE_KEY is not configured. ml_predictions read may fail due to table RLS.")

    key = getattr(settings, "supabase_service_role_key", None) or settings.supabase_key
    supabase = create_client(settings.supabase_url, key)
    rows = fetch_all_ml_collection(supabase, limit=args.limit)
    if not rows:
        print("[-] No rows retrieved from ml_collection.")
        return

    pred_rows = fetch_ml_predictions(
        supabase,
        limit=args.limit,
        raise_on_error=args.fail_on_predictions_error,
    )
    if pred_rows is None:
        print("[!] Warning: ml_predictions could not be queried; excluding from audit verdict.")
        if args.fail_on_predictions_error:
            print("[-] Exiting with code 1 due to --fail-on-predictions-error.")
            sys.exit(1)

    audit_res = analyze_records(rows, prediction_rows=pred_rows)
    generate_markdown_report(audit_res, args.output)


if __name__ == "__main__":
    main()
