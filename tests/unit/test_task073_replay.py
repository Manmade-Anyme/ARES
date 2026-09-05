import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from scripts.replay_task073 import _parse_replay_timestamp, run_replay, validate_fixture


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
FIXTURE_PATH = REPOSITORY_ROOT / "tests/fixtures/task073_18_trades_replay.json"


def _event_ledger():
    entry = "2026-07-24T04:00:00+00:00"
    return [
        {
            "entry_id": "sl",
            "entry_timestamp": entry,
            "initial_sl_timestamp": "2026-07-24T04:00:30+00:00",
            "final_state": "SL_HIT",
            "trigger_price": 100.0,
            "initial_stop": 84.0,
            "target_1": 125.0,
            "pnl_points": -16.0,
        },
        {
            "entry_id": "be",
            "entry_timestamp": entry,
            "final_state": "STOPPED_OUT_AT_BE",
            "t1_reached": True,
            "trigger_price": 100.0,
            "initial_stop": 84.0,
            "target_1": 125.0,
            "pnl_points": 0.0,
        },
        {
            "entry_id": "t2",
            "entry_timestamp": entry,
            "final_state": "T2_HIT",
            "trigger_price": 100.0,
            "initial_stop": 84.0,
            "target_1": 125.0,
            "pnl_points": 40.0,
        },
        {
            "entry_id": "time",
            "entry_timestamp": entry,
            "final_state": "TIME_STOP",
            "trigger_price": 100.0,
            "initial_stop": 84.0,
            "target_1": 125.0,
            "pnl_points": 1.0,
        },
    ]


def _complete_fixture(observations):
    return {
        "schema_version": "task073.replay_fixture.v2",
        "evidence_kind": "synthetic_diagnostic",
        "provenance": {"source_export": "test", "source_hash": "test"},
        "configuration": {"profile": "NON_EXPIRY", "expiry_selection": "non_expiry"},
        "cohort_trade_ids": ["trade-1"],
        "trades": [
            {
                "trade_id": "trade-1",
                "entry_timestamp": "2026-07-24T04:00:00+00:00",
                "trajectory": observations,
            }
        ],
    }


def _complete_observation(timestamp="2026-07-24T04:00:00+00:00"):
    return {
        "source_id": "row-1",
        "timestamp": timestamp,
        "candle": {
            "timestamp": timestamp,
            "open": 100.0,
            "high": 110.0,
            "low": 99.0,
            "close": 105.0,
            "volume": 1.0,
            "vwap": None,
        },
        "full_chain": [{"strike": 100.0, "ce_oi": 1.0, "pe_oi": 1.0}],
        "atm": {"spot_price": 105.0},
        "levels": [],
    }


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (
            "2026-07-24T03:49:17.54375+00:00",
            datetime(2026, 7, 24, 3, 49, 17, 543750, tzinfo=timezone.utc),
        ),
        (
            "2026-07-24T03:54:18.19357Z",
            datetime(2026, 7, 24, 3, 54, 18, 193570, tzinfo=timezone.utc),
        ),
    ],
)
def test_replay_timestamp_parser_accepts_variable_microsecond_precision(value, expected):
    assert _parse_replay_timestamp(value) == expected


def test_fixture_keeps_the_original_18_trade_cohort():
    with FIXTURE_PATH.open() as fixture_file:
        payload = json.load(fixture_file)

    assert len(payload["cohort_trade_ids"]) == 18


def test_fixture_marks_unrecoverable_chain_evidence_as_incomplete():
    with FIXTURE_PATH.open() as fixture_file:
        payload = json.load(fixture_file)

    assert validate_fixture(payload)["status"] == "INCOMPLETE_INPUT"


def test_validation_returns_fixture_provenance_and_configuration():
    with FIXTURE_PATH.open() as fixture_file:
        payload = json.load(fixture_file)

    assert (validate_fixture(payload)["provenance"], validate_fixture(payload)["configuration"]) == (
        payload["provenance"],
        payload["configuration"],
    )


def test_fixture_never_defaults_an_unknown_risk_profile():
    with FIXTURE_PATH.open() as fixture_file:
        payload = json.load(fixture_file)

    assert "missing configuration profile" in validate_fixture(payload)["cases"][0]["reasons"]


def test_fixture_rejects_invalid_candle_geometry():
    observation = _complete_observation()
    observation["candle"]["low"] = 111.0

    assert validate_fixture(_complete_fixture([observation]))["status"] == "ERROR"


def test_historical_runner_never_counts_incomplete_input_as_filtered_trade():
    result = run_replay(FIXTURE_PATH)

    assert result["cohort_dispositions"][0]["status"] == "INCOMPLETE_INPUT"


def test_historical_runner_marks_primary_metrics_unavailable():
    result = run_replay(FIXTURE_PATH)

    assert result["metrics"]["phase_1"]["entry_count"]["value"] is None


@pytest.mark.parametrize(
    ("metric_name", "expected"),
    [
        ("entry_count", 4),
        ("sl_hit_rate", 0.25),
        ("t1_capture_rate", 0.5),
        ("median_time_to_sl", 30.0),
        ("average_rr", 1.5625),
        ("pnl", 25.0),
    ],
)
def test_synthetic_event_ledger_calculates_each_required_metric(metric_name, expected):
    from scripts.replay_task073 import compute_metrics

    assert compute_metrics(_event_ledger())[metric_name]["value"] == expected


def test_synthetic_metrics_retain_required_denominators():
    from scripts.replay_task073 import compute_metrics

    assert {
        name: values.get("denominator", values.get("sample_count"))
        for name, values in compute_metrics(_event_ledger()).items()
    } == {
        "entry_count": 4,
        "sl_hit_rate": 4,
        "t1_capture_rate": 4,
        "median_time_to_sl": 1,
        "average_rr": 4,
        "pnl": 4,
    }


def test_empty_synthetic_ledger_distinguishes_zero_entries_from_zero_sl_events():
    from scripts.replay_task073 import compute_metrics

    assert (
        compute_metrics([])["entry_count"]["value"],
        compute_metrics([])["sl_hit_rate"]["value"],
        compute_metrics([])["median_time_to_sl"]["value"],
    ) == (0, None, None)


@pytest.mark.parametrize(
    ("invocation", "working_directory"),
    [
        ([str(REPOSITORY_ROOT / "scripts/replay_task073.py")], REPOSITORY_ROOT),
        ([str(REPOSITORY_ROOT / "scripts/replay_task073.py")], None),
        (["-m", "scripts.replay_task073"], REPOSITORY_ROOT),
    ],
)
def test_cli_is_path_independent_and_rejects_incomplete_historical_evidence(tmp_path, invocation, working_directory):
    command = [sys.executable, *invocation, "--output-dir", str(tmp_path)]
    environment = dict(os.environ)
    environment.pop("PYTHONPATH", None)
    completed = subprocess.run(
        command,
        cwd=working_directory or tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert (completed.returncode, "INCOMPLETE_INPUT" in completed.stderr) == (2, True)


def test_runner_writes_requested_json_result_only(tmp_path):
    run_replay(FIXTURE_PATH, tmp_path)

    assert (tmp_path / "task073_replay_result.json").exists()
