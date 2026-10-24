"""Exercise offline CLI ingestion through the real outcome dataset pipeline."""

from types import SimpleNamespace

import pandas as pd

from ml_signal import train_offline
from ml_signal.pipeline_trade_outcomes import TradeOutcomePipeline


def test_training_cli_uses_canonical_option_outcomes_and_keeps_legacy_labels(
    monkeypatch, tmp_path,
):
    rows = []
    analytics = []
    cases = [
        # Canonical analytics must override missing or stale snapshot labels/P&L.
        ("option-sl", "T2_HIT", "SL_HIT", 0, 0, -25.0),
        ("option-t1", None, "ATR_SL_HIT", 1, 0, 12.5),
        ("option-t2", None, "ATR_SL_HIT", 2, 0, None),
        ("option-open", "T2_HIT", "OPEN", 2, 0.2, 40.0),
        ("legacy-win", "T1_HIT", "SL_HIT", None, None, -99.0),
        ("legacy-loss", "SL_HIT", "T2_HIT", None, None, 99.0),
    ]
    for trade_id, snapshot_outcome, state, milestone, remaining, pnl in cases:
        rows.append({
            "trade_id": trade_id,
            "timestamp": "2026-10-23T04:35:00Z",
            "trade_outcome": snapshot_outcome,
            "trade_pnl": 999.0 if milestone is not None else 3.0,
            "raw_candle": {"close": 22550.0},
            "candle_features": {"body_pct": 0.1},
        })
        analytics.append({
            "id": trade_id,
            "entry_timestamp": "2026-10-23T04:30:00Z",
            # Deliberately stale on the OPEN row: canonical lifecycle must win.
            "exit_timestamp": "2026-10-23T05:30:00Z",
            "time_metrics_excluded": False,
            "result_state": state,
            "pnl_points": pnl,
            "market_context": {"option_trailing": {
                "policy_version": "option_atr_v1",
                "highest_milestone": milestone,
                "remaining_fraction": remaining,
            }} if milestone is not None else {},
        })

    class ReadOnlyQuery:
        def __init__(self, records):
            self.records = records

        def select(self, fields):
            return self

        def order(self, field):
            return self

        def limit(self, count):
            return self

        def range(self, start, end):
            self.records = self.records[start:end + 1]
            return self

        def execute(self):
            return SimpleNamespace(data=[row.copy() for row in self.records])

    class ReadOnlyClient:
        def table(self, name):
            return ReadOnlyQuery({
                "ml_collection": rows, "trade_analytics": analytics,
            }[name])

    prepared = []

    def inspect_training_frame(self, frame, market_snapshots_df, n_splits):
        # Keep fetch, CLI join, labeling and dataset preparation real; stop at fit.
        prepared.append(frame.copy())
        self.oof_predictions = self.oof_mask = None
        return None, {"mean_auc": None, "leakage_guard_passed": True}

    monkeypatch.setenv("SUPABASE_URL", "https://example.invalid")
    monkeypatch.setenv("SUPABASE_KEY", "test-key")
    monkeypatch.setattr("dotenv.load_dotenv", lambda *args, **kwargs: False)
    monkeypatch.setattr("supabase.create_client", lambda url, key: ReadOnlyClient())
    monkeypatch.setattr(train_offline, "__file__", str(tmp_path / "ml_signal/train_offline.py"))
    monkeypatch.setattr(
        "ml_signal.predictor.get_next_model_version_and_path",
        lambda models_dir: (str(tmp_path / "v999.joblib"), "v999"),
    )
    monkeypatch.setattr(TradeOutcomePipeline, "run_walk_forward", inspect_training_frame)

    train_offline.main([
        "--pipeline", "trade_outcomes", "--no-hybrid", "--no-promote",
        "--metrics-path", str(tmp_path / "metrics.json"),
    ])

    frame = prepared[0].set_index("trade_id")
    assert frame["label"].to_dict() == {
        "option-sl": 0, "option-t1": 1, "option-t2": 1,
        "legacy-win": 1, "legacy-loss": 0,
    }
    assert frame.loc["option-sl", "pnl_points"] == -25.0
    assert frame.loc["option-t1", "pnl_points"] == 12.5
    assert pd.isna(frame.loc["option-t2", "pnl_points"])
    assert frame.loc["legacy-win", "pnl_points"] == 3.0
    assert frame.loc["legacy-loss", "pnl_points"] == 3.0
