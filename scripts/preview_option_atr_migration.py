"""Offline, read-only review of carried trades; never applies a migration.

Input is an exported JSON object containing active_trades and trade_analytics.
Evidence belongs to analytics.market_context.option_atr_migration_evidence:
option_candles, milestone_events, history_verified_through, parity_signoff and
schema_verification. Evidence references are declarations requiring human
verification, not proof that this program has contacted an exchange/database.
"""
import argparse
from datetime import datetime, timedelta, timezone
import json
import math
from pathlib import Path

POLICY_VERSION = "option_atr_v1"
MIGRATION = "20261023_option_lifecycle_transition.sql"
IST = timezone(timedelta(hours=5, minutes=30))
TERMINAL = {"CLOSED", "STOPPED_OUT", "SL_HIT", "STOPPED_OUT_AT_BE", "ATR_SL_HIT", "OPTION_SL_FLOOR_HIT", "T2_HIT"}


def _timestamp(value):
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed.astimezone(IST) if parsed.tzinfo is not None else None
    except (TypeError, ValueError):
        return None


def _positive(value):
    try:
        return not isinstance(value, bool) and math.isfinite(float(value)) and float(value) > 0
    except (TypeError, ValueError, OverflowError):
        return False


def _contract(sizing):
    expiry = sizing.get("expiry_date")
    try:
        datetime.strptime(str(expiry), "%Y-%m-%d")
    except ValueError:
        return None
    security_id = str(sizing.get("security_id") or "").strip()
    if (not security_id.isdigit() or not _positive(sizing.get("strike"))
            or sizing.get("option_type") not in {"CE", "PE"}
            or not all(_positive(sizing.get(key)) for key in ("entry_limit", "option_sl"))
            or float(sizing["option_sl"]) >= float(sizing["entry_limit"])):
        return None
    return {"security_id": security_id, "expiry": expiry, "strike": float(sizing["strike"]),
            "option_type": sizing["option_type"], "exchange_segment": "NSE_FNO"}


def _history_errors(evidence, contract, registered):
    """Check declared completed source history, without inventing missing bars."""
    errors = []
    candles = evidence.get("option_candles")
    through = _timestamp(evidence.get("history_verified_through"))
    if not isinstance(candles, list) or not candles or through is None:
        return ["completed_contract_history_and_verified_through_required"]
    if registered and through < registered:
        errors.append("history_verified_through_precedes_registration")
    starts = []
    for candle in candles:
        if not isinstance(candle, dict):
            errors.append("invalid_option_history")
            break
        start = _timestamp(candle.get("start_time") or candle.get("timestamp"))
        end = _timestamp(candle.get("end_time"))
        prices = [candle.get(key) for key in ("open", "high", "low", "close")]
        identity = str(candle.get("security_id") or "")
        if (start is None or end != start + timedelta(minutes=5) or end > through
                or start.second or start.microsecond or start.minute % 5
                or not (datetime.strptime("09:15", "%H:%M").time() <= start.time()
                        < datetime.strptime("15:30", "%H:%M").time())
                or candle.get("complete") is not True or identity != contract["security_id"]
                or not all(_positive(price) for price in prices)):
            errors.append("invalid_or_incomplete_pinned_option_history")
            break
        op, high, low, close = map(float, prices)
        if high < max(op, low, close) or low > min(op, close):
            errors.append("invalid_option_history_ohlc")
            break
        starts.append(start)
    if starts:
        ordered = sorted(set(starts))
        if len(ordered) != len(starts):
            errors.append("duplicate_option_history_requires_source_resolution")
        for prior, current in zip(ordered, ordered[1:]):
            overnight = (current.date() > prior.date() and prior.time().isoformat() == "15:25:00"
                         and current.time().isoformat() == "09:15:00")
            if current != prior + timedelta(minutes=5) and not overnight:
                errors.append("option_history_gap_requires_backfill")
                break
        if registered and (ordered[0].date() >= registered.date()
                           or ordered[-1] + timedelta(minutes=5) < through):
            errors.append("continuous_prior_session_and_current_history_required")
        if len(ordered) < 14:
            errors.append("indicator_warmup_history_insufficient")
    return errors


def preview_option_atr_migration(export):
    """Return proposals and missing evidence; input and database stay unchanged.

    eligible means eligible for human proposal review, never permission to apply.
    No historical Discord message is replayed. Candidate policy state must be
    reconstructed/validated separately before an approved production correction.
    """
    if not isinstance(export, dict) or not isinstance(export.get("active_trades"), list) or not isinstance(export.get("trade_analytics"), list):
        raise ValueError("Export must contain active_trades and trade_analytics arrays")
    by_id = {}
    duplicate_ids = set()
    for row in export["trade_analytics"]:
        if not isinstance(row, dict) or row.get("id") is None:
            raise ValueError("Every analytics row requires an id")
        key = str(row["id"])
        if key in by_id:
            duplicate_ids.add(key)
        by_id[key] = row
    proposals = []
    for active in export["active_trades"]:
        if not isinstance(active, dict) or active.get("id") is None:
            raise ValueError("Every active trade row requires an id")
        trade_id = str(active["id"])
        analytics = by_id.get(trade_id) or {}
        context = analytics.get("market_context") or {}
        context = context if isinstance(context, dict) else {}
        opt = context.get("option_trailing") or {}
        result = {"trade_id": trade_id, "eligible": False, "requires_approval": True,
                  "activation_allowed": False, "replay_historical_alerts": False,
                  "write_operations": [], "requirements": []}
        if active.get("state") in TERMINAL or analytics.get("result_state") in TERMINAL:
            result.update(status="CLOSED_HISTORY_UNCHANGED", requires_approval=False)
        elif isinstance(opt, dict) and opt.get("policy_version") == POLICY_VERSION:
            result.update(status="ALREADY_VERSIONED_UNCHANGED", requires_approval=False)
        else:
            missing = result["requirements"]
            if trade_id in duplicate_ids:
                missing.append("duplicate_analytics_rows_require_resolution")
            if not analytics:
                missing.append("matching_analytics_row_required")
            if active.get("state") not in {"OPEN", "T1_HIT", "T2_REACHED"} or analytics.get("result_state") != "OPEN":
                missing.append("unambiguous_open_lifecycle_required")
            evidence = context.get("option_atr_migration_evidence") or {}
            evidence = evidence if isinstance(evidence, dict) else {}
            # entry_timestamp may be the signal source bar, preceding actual
            # registration. Only explicitly sourced registration is proof.
            registered = _timestamp(evidence.get("registered_at") or active.get("registered_at"))
            if registered is None or not evidence.get("registration_source"):
                missing.append("known_timezone_aware_registration_timestamp_required")
            sizing = context.get("options_sizing") or {}
            contract = _contract(sizing) if isinstance(sizing, dict) else None
            if contract is None:
                missing.append("complete_original_pinned_contract_and_option_bracket_required")
            if not all(_positive(active.get(key)) for key in ("entry_price", "target_1", "target_2")) or not _positive(sizing.get("synthetic_spot_sl")):
                missing.append("original_index_entry_sl_t1_t2_required")
            if contract:
                missing.extend(_history_errors(evidence, contract, registered))
            else:
                missing.append("completed_pinned_contract_history_required")
            events = evidence.get("milestone_events")
            milestone = 0
            previous_time = registered
            if not isinstance(events, list) or not evidence.get("milestone_history_source"):
                missing.append("explicit_complete_milestone_history_with_source_required")
            else:
                for event in events:
                    if not isinstance(event, dict):
                        missing.append("invalid_milestone_history")
                        break
                    event_time = _timestamp(event.get("event_time"))
                    expected = "T1_HIT" if milestone == 0 else "T2_HIT" if milestone == 1 else None
                    if (expected is None or event.get("type") != expected or not event.get("source")
                            or event_time is None or previous_time is None or event_time < previous_time
                            or (_timestamp(evidence.get("history_verified_through")) is not None
                                and event_time > _timestamp(evidence["history_verified_through"]))):
                        missing.append("chronological_sourced_t1_t2_history_required")
                        break
                    milestone += 1
                    previous_time = event_time
                expected_state = {0: "OPEN", 1: "T1_HIT", 2: "T2_REACHED"}[milestone]
                if active.get("state") != expected_state:
                    missing.append("milestone_history_does_not_match_carried_state")
            parity = evidence.get("parity_signoff") or {}
            if not isinstance(parity, dict) or parity.get("policy_version") != POLICY_VERSION or not parity.get("evidence_ref"):
                missing.append("verified_chart_parity_signoff_required")
            schema = evidence.get("schema_verification") or {}
            if not isinstance(schema, dict) or schema.get("migration") != MIGRATION or not schema.get("evidence_ref"):
                missing.append("backend_transaction_schema_verification_required")
            # A declared source history alone does not establish survival of all
            # index guards / option close-through rules under the new policy.
            replay = evidence.get("chronological_replay") or {}
            if (not isinstance(replay, dict) or replay.get("policy_version") != POLICY_VERSION
                    or replay.get("terminal") is not False or not replay.get("evidence_ref")
                    or replay.get("highest_milestone") != milestone):
                missing.append("reviewed_nonterminal_chronological_replay_required")
            result["requirements"] = sorted(set(missing))
            result.update(status="MISSING_EVIDENCE" if missing else "HUMAN_REVIEW_CANDIDATE",
                          eligible=not missing, contract=contract,
                          proposed_milestone=milestone,
                          proposed_target_booked_fraction={0: 0., 1: .5, 2: .8}[milestone],
                          proposed_remaining_fraction={0: 1., 1: .5, 2: .2}[milestone],
                          locked_stop="Requires approved chronological reconstruction; no current-price inference")
        proposals.append(result)
    return {"mode": "OFFLINE_PREVIEW_ONLY", "writes_performed": 0,
            "activation_allowed": False, "historical_alert_replay": False,
            "evidence_notice": "Exported evidence references require human verification; no live systems were queried.",
            "trades": proposals}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Preview carried option ATR migration from an offline JSON export; no writes/network.")
    parser.add_argument("export_json", type=Path)
    args = parser.parse_args(argv)
    try:
        report = preview_option_atr_migration(json.loads(args.export_json.read_text()))
    except (OSError, ValueError, TypeError) as exc:
        parser.error(str(exc))
    print(json.dumps(report, indent=2, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
