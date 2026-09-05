import json
import os
from datetime import datetime, timezone

import pytest
from scripts.replay_task073 import _parse_replay_timestamp, run_replay


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


def test_18_trade_replay_fixture_coverage():
    fixture_path = "tests/fixtures/task073_18_trades_replay.json"
    assert os.path.exists(fixture_path), f"Fixture not found: {fixture_path}"

    with open(fixture_path) as f:
        trades = json.load(f)

    assert len(trades) == 18, f"Expected 18 trades, got {len(trades)}"

    for t in trades:
        assert len(t["trajectory"]) > 0
        assert t["direction"] in ("BULLISH", "BEARISH")
        assert t["baseline_result"] is not None

def test_18_trade_replay_execution():
    run_replay()
    report_path = "reports/replays/task073_18_trades_replay_report.md"
    assert os.path.exists(report_path)
    with open(report_path) as f:
        content = f.read()
    assert "TASK-073 18-Trade Production Replay Report" in content
    assert "Fixed stop policy strictly enforced" in content
