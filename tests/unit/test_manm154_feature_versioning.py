"""
Unit tests for MANM-154: Feature Versioning, Missing Data Remediation, and Storage Contracts.
"""

import os
import unittest
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd

from ml_signal.config import MLConfig, CURRENT_FEATURE_VERSION
from ml_signal.dataset import (
    flatten_features,
    feature_columns,
    infer_feature_version_from_timestamp,
    _META_COLS,
)
from ml_signal.features import build_feature_vector
from ml_signal.collector import MLCollector
from ml_signal.predictor import SignalPredictor


class TestMANM154FeatureVersioningConfigAndCollector(unittest.TestCase):
    def test_default_config_feature_version(self):
        config = MLConfig()
        self.assertEqual(CURRENT_FEATURE_VERSION, 4)
        self.assertEqual(config.feature_version, 4)

    @patch("ml_signal.collector.create_client")
    def test_collector_snapshot_stamps_feature_version(self, mock_create_client):
        mock_supabase = MagicMock()
        mock_create_client.return_value = mock_supabase

        collector = MLCollector("https://example.supabase.co", "anon-key")
        
        # Mock candle, atm, full_chain, levels
        candle = MagicMock()
        candle.open = 24000.0
        candle.high = 24050.0
        candle.low = 23980.0
        candle.close = 24020.0
        candle.volume = 50000
        candle.vwap = 24010.0
        candle.timestamp = datetime(2026, 9, 12, 10, 0, 0, tzinfo=timezone.utc)

        atm = MagicMock()
        atm.ce.iv = 12.5
        atm.ce.oi = 100000
        atm.ce.oi_change_pct = 2.5
        atm.ce.gamma = 0.001
        atm.ce.theta = -15.0
        atm.ce.vega = 20.0
        atm.ce.delta = 0.52

        atm.pe.iv = 13.0
        atm.pe.oi = 95000
        atm.pe.oi_change_pct = -1.2
        atm.pe.gamma = 0.001
        atm.pe.theta = -14.5
        atm.pe.vega = 19.5
        atm.pe.delta = -0.48

        chain = [
            {"strike": 24000, "ce_oi": 100000, "pe_oi": 95000},
            {"strike": 24100, "ce_oi": 150000, "pe_oi": 40000},
        ]
        levels = [23900.0, 24100.0]

        inserted_records = []
        mock_supabase.table.return_value.insert.side_effect = lambda rec: MagicMock(
            execute=lambda: inserted_records.append(rec)
        )

        import asyncio
        asyncio.run(
            collector.snapshot(
                candle=candle,
                atm=atm,
                full_chain=chain,
                levels=levels,
                spot=24020.0,
                signal=None,
            )
        )

        self.assertEqual(len(inserted_records), 1)
        record = inserted_records[0]
        self.assertIn("feature_version", record)
        self.assertEqual(record["feature_version"], 4)


class TestMANM154ZeroInjectionPrevention(unittest.TestCase):
    def test_build_feature_vector_with_none_options_does_not_raise(self):
        """Verify that passing None for options context does not crash and yields None instead of synthetic zeros."""
        candle = {
            "open": 24000.0,
            "high": 24050.0,
            "low": 23980.0,
            "close": 24020.0,
            "volume": 1000,
        }
        feats = build_feature_vector(
            candle=candle,
            volume_history=[1000, 1100],
            iv_history=None,
            atm_ce=None,
            atm_pe=None,
            total_ce_oi=None,
            total_pe_oi=None,
            all_ce_oi=None,
            all_pe_oi=None,
            levels=[24100.0],
            timestamp=datetime.now(timezone.utc),
            spot=24020.0,
        )

        # Candle features are present
        self.assertEqual(feats["candle_features__is_bullish"], 1)
        
        # Option & Greek features evaluate to None (not synthetic 0.0 or 0)
        self.assertIsNone(feats.get("greek_features__net_delta"))
        self.assertIsNone(feats.get("greek_features__total_vega"))
        self.assertIsNone(feats.get("greek_features__gamma_theta_ratio"))
        self.assertIsNone(feats.get("oi_features__pcr_oi"))
        self.assertIsNone(feats.get("oi_features__max_ce_oi"))
        self.assertIsNone(feats.get("iv_features__iv_level"))

    def test_build_feature_vector_generates_presence_indicators(self):
        """Verify serving path generates structure and greek presence indicators to prevent training-serving skew."""
        candle = {
            "open": 24000.0,
            "high": 24050.0,
            "low": 23980.0,
            "close": 24020.0,
            "volume": 1000,
        }
        atm_ce = {"iv": 12.0, "oi": 1000, "oi_change_pct": 1.0, "gamma": 0.001, "theta": -10, "vega": 5, "delta": 0.5}
        atm_pe = {"iv": 12.0, "oi": 1000, "oi_change_pct": 1.0, "gamma": 0.001, "theta": -10, "vega": 5, "delta": -0.5}

        feats_with_levels = build_feature_vector(
            candle=candle,
            volume_history=[1000],
            iv_history=None,
            atm_ce=atm_ce,
            atm_pe=atm_pe,
            total_ce_oi=1000,
            total_pe_oi=1000,
            all_ce_oi=[1000],
            all_pe_oi=[1000],
            levels=[24000.0, 24100.0],  # 24000 is support (< spot), 24100 is resistance (> spot)
            timestamp=datetime.now(timezone.utc),
            spot=24020.0,
        )
        self.assertEqual(feats_with_levels["structure__has_nearest_support"], 1.0)
        self.assertEqual(feats_with_levels["structure__has_nearest_resistance"], 1.0)
        self.assertEqual(feats_with_levels["greek__has_net_delta"], 1.0)

        # Without levels and without option delta
        feats_no_levels = build_feature_vector(
            candle=candle,
            volume_history=[1000],
            iv_history=None,
            atm_ce=None,
            atm_pe=None,
            total_ce_oi=None,
            total_pe_oi=None,
            all_ce_oi=None,
            all_pe_oi=None,
            levels=[],
            timestamp=datetime.now(timezone.utc),
            spot=24020.0,
        )
        self.assertEqual(feats_no_levels["structure__has_nearest_support"], 0.0)
        self.assertEqual(feats_no_levels["structure__has_nearest_resistance"], 0.0)
        self.assertEqual(feats_no_levels["greek__has_net_delta"], 0.0)

    def test_signal_predictor_predict_proba_converts_none_to_nan(self):

        """Verify SignalPredictor correctly handles None in feature vectors by casting to NaN."""
        predictor = SignalPredictor()
        predictor.model = MagicMock()
        predictor.model.predict_proba.return_value = np.array([[0.35, 0.65]])
        predictor.feature_names = ["structure_features__dist_to_nearest_support", "greek_features__net_delta"]

        features = {
            "structure_features__dist_to_nearest_support": None,
            "greek_features__net_delta": None,
        }

        proba = predictor.predict_proba(features)
        self.assertEqual(proba, 0.65)

        # Inspect the DataFrame passed to model.predict_proba
        args, _ = predictor.model.predict_proba.call_args
        df_passed = args[0]
        self.assertTrue(pd.isna(df_passed.iloc[0]["structure_features__dist_to_nearest_support"]))
        self.assertTrue(pd.isna(df_passed.iloc[0]["greek_features__net_delta"]))


class TestMANM154SchemaEpochsAndDataset(unittest.TestCase):
    def test_infer_feature_version_from_timestamp(self):
        # Epoch 1: before 2026-07-28T06:32:37Z
        v1_ts = "2026-07-20T10:00:00Z"
        self.assertEqual(infer_feature_version_from_timestamp(v1_ts), 1)

        # Epoch 2: 2026-07-28T06:32:37Z to 2026-07-31T13:14:34Z
        v2_ts = "2026-07-29T09:15:00Z"
        self.assertEqual(infer_feature_version_from_timestamp(v2_ts), 2)

        # Epoch 3: 2026-07-31T13:14:34Z to 2026-08-21T05:46:35Z
        v3_ts = "2026-08-10T11:30:00Z"
        self.assertEqual(infer_feature_version_from_timestamp(v3_ts), 3)

        # Epoch 4: >= 2026-08-21T05:46:35Z
        v4_ts = "2026-09-01T14:00:00Z"
        self.assertEqual(infer_feature_version_from_timestamp(v4_ts), 4)

    def test_flatten_features_preserves_version_and_adds_indicators(self):
        rows = [
            # Row 1: v1 legacy row without feature_version column
            {
                "timestamp": "2026-07-20T10:00:00Z",
                "raw_candle": {"close": 24000.0},
                "structure_features": {"dist_to_nearest_support": 50.0},  # has support, no resistance
                "greek_features": {},  # no net_delta
            },
            # Row 2: v4 modern row with explicit feature_version
            {
                "timestamp": "2026-09-10T10:00:00Z",
                "feature_version": 4,
                "raw_candle": {"close": 25000.0},
                "structure_features": {"dist_to_nearest_resistance": 35.0}, # has resistance, no support
                "greek_features": {"net_delta": 0.15}, # has net_delta
            },
        ]

        df = flatten_features(rows)

        # Check feature_version column exists and inferred correctly
        self.assertIn("feature_version", df.columns)
        self.assertEqual(df.iloc[0]["feature_version"], 1)
        self.assertEqual(df.iloc[1]["feature_version"], 4)

        # Check feature_version is excluded from feature_columns (in _META_COLS)
        fcols = feature_columns(df)
        self.assertNotIn("feature_version", fcols)
        self.assertIn("feature_version", _META_COLS)

        # Check structural presence indicators
        self.assertIn("structure__has_nearest_support", df.columns)
        self.assertIn("structure__has_nearest_resistance", df.columns)
        self.assertIn("greek__has_net_delta", df.columns)

        # Row 1 has support (1.0), no resistance (0.0), no delta (0.0)
        self.assertEqual(df.iloc[0]["structure__has_nearest_support"], 1.0)
        self.assertEqual(df.iloc[0]["structure__has_nearest_resistance"], 0.0)
        self.assertEqual(df.iloc[0]["greek__has_net_delta"], 0.0)

        # Row 2 has no support (0.0), has resistance (1.0), has delta (1.0)
        self.assertEqual(df.iloc[1]["structure__has_nearest_support"], 0.0)
        self.assertEqual(df.iloc[1]["structure__has_nearest_resistance"], 1.0)
        self.assertEqual(df.iloc[1]["greek__has_net_delta"], 1.0)

        # Verify indicators ARE included in feature_columns
        self.assertIn("structure__has_nearest_support", fcols)
        self.assertIn("structure__has_nearest_resistance", fcols)
        self.assertIn("greek__has_net_delta", fcols)

        # Verify missing values are NaN
        self.assertTrue(pd.isna(df.iloc[0]["structure_features__dist_to_nearest_resistance"]))
        self.assertTrue(pd.isna(df.iloc[1]["structure_features__dist_to_nearest_support"]))
        self.assertTrue(pd.isna(df.iloc[0]["greek_features__net_delta"]))


class TestMANM154MigrationSQL(unittest.TestCase):
    def test_migration_file_exists_and_contains_expected_ddl(self):
        migration_path = "migrations/2026-09-24-manm154-feature-versioning.sql"
        self.assertTrue(os.path.exists(migration_path))
        with open(migration_path, "r") as f:
            content = f.read()

        self.assertIn("ALTER TABLE ml_collection ADD COLUMN IF NOT EXISTS feature_version integer NOT NULL DEFAULT 4;", content)
        self.assertIn("CREATE INDEX IF NOT EXISTS idx_ml_collection_feature_version ON ml_collection (feature_version);", content)
        self.assertIn("2026-07-28T06:32:37Z", content)
        self.assertIn("2026-07-31T13:14:34Z", content)
        self.assertIn("2026-08-21T05:46:35Z", content)


class TestMANM154EdgeCasesAndFallbacks(unittest.TestCase):
    def test_infer_feature_version_edge_cases(self):
        # None timestamp
        self.assertEqual(infer_feature_version_from_timestamp(None), 4)

        # Invalid string / unparseable timestamp
        self.assertEqual(infer_feature_version_from_timestamp("invalid-date-string"), 4)

        # Invalid object / non-string type that raises
        self.assertEqual(infer_feature_version_from_timestamp({"unsupported": 123}), 4)

        # Naive datetime (no tzinfo) localization across each epoch
        v1_naive = datetime(2026, 7, 20, 10, 0, 0)
        self.assertEqual(infer_feature_version_from_timestamp(v1_naive), 1)

        v2_naive = datetime(2026, 7, 29, 10, 0, 0)
        self.assertEqual(infer_feature_version_from_timestamp(v2_naive), 2)

        v3_naive = datetime(2026, 8, 10, 10, 0, 0)
        self.assertEqual(infer_feature_version_from_timestamp(v3_naive), 3)

        v4_naive = datetime(2026, 9, 1, 10, 0, 0)
        self.assertEqual(infer_feature_version_from_timestamp(v4_naive), 4)

    def test_flatten_features_empty_rows(self):
        df_empty = flatten_features([])
        self.assertTrue(df_empty.empty)
        self.assertIn("feature_version", df_empty.columns)
        self.assertIn("structure__has_nearest_support", df_empty.columns)
        self.assertIn("structure__has_nearest_resistance", df_empty.columns)
        self.assertIn("greek__has_net_delta", df_empty.columns)

    def test_flatten_features_corrupted_version_string(self):
        row = {
            "timestamp": "2026-07-20T10:00:00Z",
            "raw_candle": {"close": 24000.0},
            "feature_version": "corrupted_non_int",
        }
        df = flatten_features([row])
        self.assertEqual(len(df), 1)
        # Should catch ValueError and fall back to timestamp inference (epoch 1)
        self.assertEqual(df.iloc[0]["feature_version"], 1)

    def test_flatten_features_missing_feature_groups_indicators_default_zero(self):
        # Row with no structure_features and no greek_features
        row = {
            "timestamp": "2026-09-10T10:00:00Z",
            "raw_candle": {"close": 24500.0},
            "feature_version": 4,
        }
        df = flatten_features([row])
        self.assertEqual(len(df), 1)
        self.assertEqual(df.iloc[0]["structure__has_nearest_support"], 0.0)
        self.assertEqual(df.iloc[0]["structure__has_nearest_resistance"], 0.0)
        self.assertEqual(df.iloc[0]["greek__has_net_delta"], 0.0)


class TestMANM154OfflineTrainingAndFetch(unittest.TestCase):
    def test_fetch_ml_collection_with_feature_version(self):
        from ml_signal.train_offline import _fetch_ml_collection

        mock_supabase = MagicMock()
        # Mock probe success
        mock_supabase.table.return_value.select.return_value.limit.return_value.execute.return_value = MagicMock()

        # Mock paginated fetch
        mock_batch = [{"timestamp": "2026-09-10T10:00:00Z", "feature_version": 4}]
        mock_supabase.table.return_value.select.return_value.order.return_value.range.return_value.execute.return_value = MagicMock(
            data=mock_batch
        )

        rows = _fetch_ml_collection(mock_supabase, page=100)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["feature_version"], 4)

        # Verify feature_version was included in select cols
        call_args = mock_supabase.table.return_value.select.call_args_list
        select_cols = call_args[1][0][0]
        self.assertIn("feature_version", select_cols)

    def test_fetch_ml_collection_fallback_when_column_missing(self):
        from ml_signal.train_offline import _fetch_ml_collection

        mock_supabase = MagicMock()
        # Mock probe failure (pre-migration schema error)
        mock_supabase.table.return_value.select.return_value.limit.return_value.execute.side_effect = Exception("Column does not exist")

        mock_batch = [{"timestamp": "2026-09-10T10:00:00Z"}]
        mock_supabase.table.return_value.select.return_value.order.return_value.range.return_value.execute.return_value = MagicMock(
            data=mock_batch
        )

        rows = _fetch_ml_collection(mock_supabase, page=100)
        self.assertEqual(len(rows), 1)

        # Verify feature_version was omitted from select cols
        call_args = mock_supabase.table.return_value.select.call_args_list
        select_cols = call_args[1][0][0]
        self.assertNotIn("feature_version", select_cols)

    def test_missingness_by_feature_version_metric(self):
        # Construct sample df_train with feature_version and missing columns
        df_train = pd.DataFrame({
            "feature_version": [1, 1, 4, 4],
            "greek_features__net_delta": [np.nan, np.nan, 0.1, 0.2],
            "structure_features__dist_to_nearest_support": [np.nan, 10.0, 5.0, np.nan],
            "candle_features__body_size": [1.0, 2.0, 3.0, 4.0],
        })
        feature_cols = [
            "greek_features__net_delta",
            "structure_features__dist_to_nearest_support",
            "candle_features__body_size",
        ]

        metrics = {}
        if "feature_version" in df_train.columns:
            missingness_by_ver = {}
            for ver, vdf in df_train.groupby("feature_version"):
                v_missing = {
                    col: round(float(vdf[col].isna().mean()), 4)
                    for col in feature_cols
                    if vdf[col].isna().any()
                }
                missingness_by_ver[str(ver)] = {
                    "n_samples": int(len(vdf)),
                    "features_with_missing": v_missing,
                }
            metrics["missingness_by_feature_version"] = missingness_by_ver

        self.assertIn("missingness_by_feature_version", metrics)
        v1_metrics = metrics["missingness_by_feature_version"]["1"]
        self.assertEqual(v1_metrics["n_samples"], 2)
        self.assertEqual(v1_metrics["features_with_missing"]["greek_features__net_delta"], 1.0)
        self.assertEqual(v1_metrics["features_with_missing"]["structure_features__dist_to_nearest_support"], 0.5)
        self.assertNotIn("candle_features__body_size", v1_metrics["features_with_missing"])

        v4_metrics = metrics["missingness_by_feature_version"]["4"]
        self.assertEqual(v4_metrics["n_samples"], 2)
        self.assertEqual(v4_metrics["features_with_missing"]["structure_features__dist_to_nearest_support"], 0.5)
        self.assertNotIn("greek_features__net_delta", v4_metrics["features_with_missing"])


if __name__ == "__main__":
    unittest.main()

