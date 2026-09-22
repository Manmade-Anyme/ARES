"""MANM-151 regressions for crash recovery ordering and display-ID reuse."""

from copy import deepcopy

import pytest

from ml_signal import backfill_labels
from tests.unit.test_task194_ml_labels_and_oi_distribution import _FakeSupabase


@pytest.mark.parametrize("apply", [False, True])
def test_cli_recovers_missing_analytics_before_repairing_display_key(monkeypatch, apply):
    rows = {
        "trade_analytics": [],
        "active_trades": [{
            "id": "t-missing", "signal_id": "0042",
            "setup_type": "FAILED_BREAKOUT", "direction": "BULLISH",
            "entry_price": 24000.0, "created_at": "2026-08-25T08:40:00+00:00",
            "state": "CLOSED", "exit_price": 24100.0, "exit_type": "T2_HIT",
            "exit_timestamp": "2026-08-25T08:42:00+00:00",
            "pnl_points_override": None,
        }],
        "ares_signals": [{
            "id": 205, "setup_type": "FAILED_BREAKOUT",
            "created_at": "2026-08-25T08:40:00+00:00",
            "timestamp": "2026-08-25T08:40:00+00:00",
        }],
        "ml_collection": [{
            "id": 1, "signal_id": "0042", "signal_setup_type": "FAILED_BREAKOUT",
            "timestamp": "2026-08-25T08:40:01+00:00",
            "created_at": "2026-08-25T08:40:01+00:00", "trade_id": None,
        }],
    }
    original = deepcopy(rows)
    sb = _FakeSupabase(rows)
    monkeypatch.setattr(backfill_labels, "_load_env", lambda _: {
        "SUPABASE_URL": "http://supabase.invalid", "SUPABASE_KEY": "test-key",
    })
    monkeypatch.setattr(backfill_labels, "create_client", lambda *_: sb)
    monkeypatch.setattr("sys.argv", ["backfill_labels"] + (["--apply"] if apply else []))

    assert backfill_labels.main() == 0

    if not apply:
        assert rows == original
        assert sb.inserts == sb.updates == []
        return

    assert rows["trade_analytics"][0]["signal_id"] == "0042"
    assert rows["trade_analytics"][0]["exit_timestamp"] == "2026-08-25T08:42:00+00:00"
    assert rows["ml_collection"][0] == {
        **original["ml_collection"][0],
        "trade_id": "t-missing", "trade_outcome": "T2_HIT",
        "trade_pnl": 100.0, "trade_score": 2,
    }
    repaired = deepcopy(rows)
    writes = (len(sb.inserts), len(sb.updates))
    assert backfill_labels.main() == 0
    assert rows == repaired
    assert (len(sb.inserts), len(sb.updates)) == writes


def test_dry_run_carries_reconstructed_analytics_into_key_repair(capsys):
    rows = {
        "trade_analytics": [],
        "active_trades": [{
            "id": "t-missing", "signal_id": "0042",
            "setup_type": "FAILED_BREAKOUT", "direction": "BULLISH",
            "entry_price": 24000.0, "created_at": "2026-08-25T08:40:00+00:00",
            "state": "CLOSED", "exit_price": 24100.0, "exit_type": "T2_HIT",
            "exit_timestamp": "2026-08-25T08:42:00+00:00",
            "pnl_points_override": None,
        }],
        "ares_signals": [{
            "id": 205, "setup_type": "FAILED_BREAKOUT",
            "created_at": "2026-08-25T08:40:00+00:00",
            "timestamp": "2026-08-25T08:40:00+00:00",
        }],
        "ml_collection": [{
            "id": 1, "signal_id": "0042", "signal_setup_type": "FAILED_BREAKOUT",
            "created_at": "2026-08-25T08:40:01+00:00", "trade_id": None,
        }],
    }
    sb = _FakeSupabase(rows)
    prospective = []

    assert backfill_labels.repair_stuck_open_trades(
        sb, apply=False, prospective_trades=prospective
    ) == 1
    assert prospective[0]["signal_id"] == "0042"
    assert backfill_labels.repair_join_key(
        sb, apply=False, prospective_trades=prospective
    ) == 0
    assert "already correct          : 1" in capsys.readouterr().out
    assert sb.inserts == sb.updates == []


def test_dry_run_carries_reconstructed_analytics_into_label_backfill(capsys):
    rows = {
        "trade_analytics": [],
        "active_trades": [{
            "id": "t-missing", "signal_id": "0042",
            "setup_type": "FAILED_BREAKOUT", "direction": "BULLISH",
            "entry_price": 24000.0, "created_at": "2026-08-25T08:40:00+00:00",
            "state": "CLOSED", "exit_price": 24100.0, "exit_type": "T2_HIT",
            "exit_timestamp": "2026-08-25T08:42:00+00:00",
            "pnl_points_override": None,
        }],
        "ml_collection": [{
            "id": 1, "signal_id": "0042", "signal_setup_type": "FAILED_BREAKOUT",
            "timestamp": "2026-08-25T08:40:01+00:00", "trade_id": None,
        }],
    }
    sb = _FakeSupabase(rows)
    prospective = []

    assert backfill_labels.repair_stuck_open_trades(
        sb, apply=False, prospective_trades=prospective
    ) == 1
    assert backfill_labels.backfill_labels(
        sb, apply=False, prospective_trades=prospective
    ) == 1
    assert "closed & attributable    : 1" in capsys.readouterr().out
    assert sb.inserts == sb.updates == []


def test_dry_run_carries_terminal_update_into_label_backfill(capsys):
    rows = {
        "trade_analytics": [{
            "id": "t-open", "signal_id": "0042", "setup_type": "FAILED_BREAKOUT",
            "result_state": "OPEN", "entry_timestamp": "2026-08-25T08:40:00+00:00",
            "entry_price": 24000.0, "direction": "BULLISH",
        }],
        "active_trades": [{
            "id": "t-open", "signal_id": "0042", "setup_type": "FAILED_BREAKOUT",
            "direction": "BULLISH", "entry_price": 24000.0,
            "created_at": "2026-08-25T08:40:00+00:00", "state": "CLOSED",
            "exit_price": 24100.0, "exit_type": "T2_HIT",
            "exit_timestamp": "2026-08-25T08:42:00+00:00", "pnl_points_override": None,
        }],
        "ml_collection": [{
            "id": 1, "signal_id": "0042", "signal_setup_type": "FAILED_BREAKOUT",
            "timestamp": "2026-08-25T08:40:01+00:00", "trade_id": None,
        }],
    }
    sb = _FakeSupabase(rows)
    prospective = []

    assert backfill_labels.repair_stuck_open_trades(
        sb, apply=False, prospective_trades=prospective
    ) == 1
    assert backfill_labels.backfill_labels(
        sb, apply=False, prospective_trades=prospective
    ) == 1
    assert "closed & attributable    : 1" in capsys.readouterr().out
    assert sb.inserts == sb.updates == []


@pytest.mark.parametrize("apply", [False, True])
@pytest.mark.parametrize("timestamp", [None, "2026-08-25T08:40:01+00:00"])
@pytest.mark.parametrize("setup,expected", [
    ("OI_WALL_REJECTION", 0),
    (None, 0),
    ("SetupType.FAILED_BREAKOUT", 1),
])
def test_singleton_fallback_requires_matching_setup(apply, timestamp, setup, expected):
    rows = {
        "trade_analytics": [{
            "id": "t-closed", "signal_id": "0042", "setup_type": "FAILED_BREAKOUT",
            "entry_timestamp": "2026-08-25T08:40:00+00:00",
            "result_state": "SL_HIT", "pnl_points": -10.0, "score": 0,
        }],
        "ml_collection": [{
            "id": 1, "signal_id": "0042", "signal_setup_type": setup,
            "timestamp": timestamp, "trade_id": None,
        }],
    }
    original = deepcopy(rows)
    sb = _FakeSupabase(rows)

    assert backfill_labels.backfill_labels(sb, apply=apply) == expected

    if expected and apply:
        assert rows["ml_collection"][0] == {
            **original["ml_collection"][0],
            "trade_id": "t-closed", "trade_outcome": "SL_HIT",
            "trade_pnl": -10.0, "trade_score": 0,
        }
    else:
        assert rows == original
        assert sb.updates == []
