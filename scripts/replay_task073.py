"""Offline evidence validation for TASK-073 replay inputs.

Historical acceptance is deliberately withheld unless the fixture contains the
closed candles and strike-keyed chain observations required by the real engine.
Synthetic event ledgers can still exercise the metric contract deterministically.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import datetime
from pathlib import Path
from statistics import median
from typing import Any, Iterable, Mapping, Optional


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_FIXTURE_PATH = REPOSITORY_ROOT / "tests/fixtures/task073_18_trades_replay.json"
RESULT_FILENAME = "task073_replay_result.json"
METRIC_NAMES = (
    "entry_count",
    "sl_hit_rate",
    "t1_capture_rate",
    "median_time_to_sl",
    "average_rr",
    "pnl",
)


def _parse_replay_timestamp(value: str) -> datetime:
    """Parse an ISO timestamp on supported Python versions."""
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _metric(
    value: Optional[float],
    unit: str,
    *,
    numerator: Optional[int] = None,
    denominator: Optional[int] = None,
    sample_count: Optional[int] = None,
    unavailable_reason: Optional[str] = None,
) -> dict[str, Any]:
    metric = {"value": value, "unit": unit}
    if numerator is not None:
        metric["numerator"] = numerator
    if denominator is not None:
        metric["denominator"] = denominator
    if sample_count is not None:
        metric["sample_count"] = sample_count
    if unavailable_reason is not None:
        metric["unavailable_reason"] = unavailable_reason
    return metric


def unavailable_metrics(reason: str) -> dict[str, dict[str, Any]]:
    """Return the six required metrics without fabricating a historical result."""
    return {
        "entry_count": _metric(None, "entries", denominator=0, unavailable_reason=reason),
        "sl_hit_rate": _metric(None, "rate", denominator=0, unavailable_reason=reason),
        "t1_capture_rate": _metric(None, "rate", denominator=0, unavailable_reason=reason),
        "median_time_to_sl": _metric(None, "seconds", sample_count=0, unavailable_reason=reason),
        "average_rr": _metric(None, "ratio", sample_count=0, unavailable_reason=reason),
        "pnl": _metric(None, "spot_points", denominator=0, unavailable_reason=reason),
    }


def _finite_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _observation_reasons(trade: Mapping[str, Any]) -> list[str]:
    trajectory = trade.get("trajectory")
    if not isinstance(trajectory, list) or not trajectory:
        return ["missing observations"]

    reasons = []
    if not all(isinstance(point.get("source_id"), str) for point in trajectory if isinstance(point, Mapping)):
        reasons.append("missing source row identity")
    if not all(isinstance(point.get("candle"), Mapping) for point in trajectory if isinstance(point, Mapping)):
        reasons.append("missing actual closed OHLCV candle")
    if not all(isinstance(point.get("full_chain"), list) for point in trajectory if isinstance(point, Mapping)):
        reasons.append("missing strike-keyed CE/PE OI chain")
    if not all(isinstance(point.get("atm"), Mapping) for point in trajectory if isinstance(point, Mapping)):
        reasons.append("missing engine ATM/IV inputs")
    if not all(isinstance(point.get("levels"), list) for point in trajectory if isinstance(point, Mapping)):
        reasons.append("missing engine structural levels")
    return reasons


def _observation_errors(trade: Mapping[str, Any]) -> list[str]:
    trajectory = trade.get("trajectory")
    if not isinstance(trajectory, list):
        return []

    errors = []
    previous_timestamp = None
    timestamps: dict[str, str] = {}
    for index, observation in enumerate(trajectory):
        if not isinstance(observation, Mapping):
            errors.append(f"observation {index} must be an object")
            continue
        timestamp = observation.get("timestamp")
        try:
            observed_at = _parse_replay_timestamp(str(timestamp))
        except ValueError:
            errors.append(f"observation {index} has an invalid timestamp")
            continue
        if previous_timestamp is not None and observed_at < previous_timestamp:
            errors.append(f"observation {index} is out of timestamp order")
        previous_timestamp = observed_at

        fingerprint = json.dumps(observation, sort_keys=True, separators=(",", ":"))
        prior_fingerprint = timestamps.get(str(timestamp))
        if prior_fingerprint is not None and prior_fingerprint != fingerprint:
            errors.append(f"observation {index} conflicts at duplicate timestamp")
        timestamps[str(timestamp)] = fingerprint

        candle = observation.get("candle")
        if candle is None:
            continue
        if not isinstance(candle, Mapping):
            errors.append(f"observation {index} candle must be an object")
            continue
        required = ("open", "high", "low", "close", "volume")
        if not all(_finite_number(candle.get(field)) for field in required):
            errors.append(f"observation {index} candle has non-finite OHLCV")
            continue
        open_price = float(candle["open"])
        high = float(candle["high"])
        low = float(candle["low"])
        close = float(candle["close"])
        if low > min(open_price, close) or high < max(open_price, close) or low > high:
            errors.append(f"observation {index} candle has invalid OHLC geometry")
    return errors


def validate_fixture(payload: object) -> dict[str, Any]:
    """Validate replay provenance and required market inputs without live I/O."""
    if not isinstance(payload, Mapping):
        return {
            "status": "ERROR",
            "reasons": ["fixture root must be an object"],
            "cases": [],
            "cohort_ids": [],
            "provenance": None,
            "configuration": None,
        }

    required = ("schema_version", "evidence_kind", "provenance", "configuration", "cohort_trade_ids", "trades")
    missing = [field for field in required if field not in payload]
    trades = payload.get("trades")
    cohort_ids = payload.get("cohort_trade_ids")
    if missing or not isinstance(trades, list) or not isinstance(cohort_ids, list):
        return {
            "status": "ERROR",
            "reasons": missing or ["cohort_trade_ids and trades must be arrays"],
            "cases": [],
            "cohort_ids": cohort_ids if isinstance(cohort_ids, list) else [],
            "provenance": payload.get("provenance"),
            "configuration": payload.get("configuration"),
        }

    configuration = payload["configuration"]
    configuration_reasons = []
    if not isinstance(configuration, Mapping) or not configuration.get("profile"):
        configuration_reasons.append("missing configuration profile")
    if not isinstance(configuration, Mapping) or not configuration.get("expiry_selection"):
        configuration_reasons.append("missing expiry selection")

    cases = []
    fixture_ids = []
    for trade in trades:
        if not isinstance(trade, Mapping):
            cases.append({"trade_id": None, "status": "ERROR", "reasons": ["trade must be an object"]})
            continue
        trade_id = trade.get("trade_id")
        fixture_ids.append(trade_id)
        missing_trade_fields = [field for field in ("trade_id", "entry_timestamp", "trajectory") if field not in trade]
        if missing_trade_fields:
            cases.append({"trade_id": trade_id, "status": "ERROR", "reasons": missing_trade_fields})
            continue
        observation_errors = _observation_errors(trade)
        if observation_errors:
            cases.append({"trade_id": trade_id, "status": "ERROR", "reasons": observation_errors})
            continue
        cases.append({
            "trade_id": trade_id,
            "status": "INCOMPLETE_INPUT",
            "reasons": configuration_reasons + _observation_reasons(trade),
        })

    if set(cohort_ids) != set(fixture_ids) or len(cohort_ids) != len(set(cohort_ids)):
        return {
            "status": "ERROR",
            "reasons": ["cohort_trade_ids must match unique trade_id values"],
            "cases": cases,
            "cohort_ids": cohort_ids,
            "provenance": payload.get("provenance"),
            "configuration": configuration,
        }
    if any(case["status"] == "ERROR" for case in cases):
        return {
            "status": "ERROR",
            "reasons": ["invalid trade records"],
            "cases": cases,
            "cohort_ids": cohort_ids,
            "provenance": payload.get("provenance"),
            "configuration": configuration,
        }
    if any(case["reasons"] for case in cases):
        return {
            "status": "INCOMPLETE_INPUT",
            "reasons": [],
            "cases": cases,
            "cohort_ids": cohort_ids,
            "provenance": payload.get("provenance"),
            "configuration": configuration,
        }
    return {
        "status": "READY",
        "reasons": [],
        "cases": cases,
        "cohort_ids": cohort_ids,
        "provenance": payload.get("provenance"),
        "configuration": configuration,
    }


def compute_metrics(events: Iterable[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    """Calculate the approved six metrics from an already-emitted event ledger."""
    entries = list(events)
    entry_count = len(entries)
    if not entries:
        return {
            "entry_count": _metric(0, "entries", numerator=0, denominator=0),
            "sl_hit_rate": _metric(None, "rate", denominator=0, unavailable_reason="no emitted entries"),
            "t1_capture_rate": _metric(None, "rate", denominator=0, unavailable_reason="no emitted entries"),
            "median_time_to_sl": _metric(None, "seconds", sample_count=0, unavailable_reason="no SL events"),
            "average_rr": _metric(None, "ratio", sample_count=0, unavailable_reason="no emitted entries"),
            "pnl": _metric(0.0, "spot_points", denominator=0),
        }

    sl_events = [event for event in entries if event.get("final_state") == "SL_HIT"]
    t1_events = [
        event
        for event in entries
        if event.get("t1_reached") or event.get("final_state") in {"T1_HIT", "T2_HIT"}
    ]
    sl_elapsed = [
        (_parse_replay_timestamp(str(event["initial_sl_timestamp"])) - _parse_replay_timestamp(str(event["entry_timestamp"]))).total_seconds()
        for event in sl_events
    ]
    if any(seconds < 0 for seconds in sl_elapsed):
        raise ValueError("initial SL event cannot precede entry")

    ratios = []
    pnl = 0.0
    for event in entries:
        trigger = float(event["trigger_price"])
        stop = float(event["initial_stop"])
        target = float(event["target_1"])
        event_pnl = float(event["pnl_points"])
        if not all(_finite_number(value) for value in (trigger, stop, target, event_pnl)):
            raise ValueError("event ledger contains a non-finite numeric value")
        risk = abs(stop - trigger)
        if risk == 0:
            raise ValueError("event ledger contains a zero initial stop distance")
        ratios.append(abs(target - trigger) / risk)
        pnl += event_pnl

    return {
        "entry_count": _metric(entry_count, "entries", numerator=entry_count, denominator=entry_count),
        "sl_hit_rate": _metric(len(sl_events) / entry_count, "rate", numerator=len(sl_events), denominator=entry_count),
        "t1_capture_rate": _metric(len(t1_events) / entry_count, "rate", numerator=len(t1_events), denominator=entry_count),
        "median_time_to_sl": _metric(median(sl_elapsed) if sl_elapsed else None, "seconds", sample_count=len(sl_elapsed), unavailable_reason=None if sl_elapsed else "no SL events"),
        "average_rr": _metric(sum(ratios) / len(ratios), "ratio", sample_count=len(ratios)),
        "pnl": _metric(pnl, "spot_points", denominator=entry_count),
    }


def _incomplete_result(payload: Mapping[str, Any], validation: Mapping[str, Any]) -> dict[str, Any]:
    reason = "historical input lacks required closed-candle and strike-chain evidence"
    return {
        "schema_version": "task073.replay_result.v2",
        "evidence_kind": payload.get("evidence_kind"),
        "input_status": "INCOMPLETE_INPUT",
        "provenance": payload.get("provenance"),
        "configuration": payload.get("configuration"),
        "cohort_dispositions": [
            {"trade_id": case["trade_id"], "status": "INCOMPLETE_INPUT", "reasons": case["reasons"]}
            for case in validation["cases"]
        ],
        "events": [],
        "metrics": {
            "controlled_baseline": unavailable_metrics(reason),
            "phase_1": unavailable_metrics(reason),
        },
        "coverage": {
            "cohort_count": len(validation["cohort_ids"]),
            "complete_cases": 0,
            "incomplete_cases": len(validation["cohort_ids"]),
        },
    }


def _write_result(result: Mapping[str, Any], output_dir: Optional[Path]) -> None:
    if output_dir is None:
        return
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / RESULT_FILENAME).write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")


def run_replay(fixture_path: Optional[Path] = None, output_dir: Optional[Path] = None) -> dict[str, Any]:
    """Validate a replay fixture and return structured evidence without live I/O."""
    path = Path(fixture_path) if fixture_path is not None else DEFAULT_FIXTURE_PATH
    try:
        payload = json.loads(path.read_text())
    except FileNotFoundError as error:
        raise ValueError(f"fixture not found: {path}") from error
    except json.JSONDecodeError as error:
        raise ValueError(f"fixture contains invalid JSON: {error.msg}") from error

    validation = validate_fixture(payload)
    if validation["status"] == "ERROR":
        raise ValueError("invalid fixture: " + "; ".join(validation["reasons"]))
    if validation["status"] == "READY":
        raise ValueError("complete offline engine replay requires an approved full-chain fixture")

    result = _incomplete_result(payload, validation)
    _write_result(result, Path(output_dir) if output_dir is not None else None)
    return result


def main(argv: Optional[list[str]] = None) -> int:
    """Run the replay validator CLI and return a process exit code."""
    parser = argparse.ArgumentParser(description="Validate TASK-073 replay evidence offline.")
    parser.add_argument("--fixture", type=Path, default=DEFAULT_FIXTURE_PATH)
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args(argv)
    try:
        result = run_replay(args.fixture, args.output_dir)
    except ValueError as error:
        print(f"TASK-073 replay ERROR: {error}", file=sys.stderr)
        return 2

    print(f"TASK-073 replay {result['input_status']}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
