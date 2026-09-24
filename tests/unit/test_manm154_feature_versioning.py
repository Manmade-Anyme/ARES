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


class TestMANM154MissingnessAuditScript(unittest.TestCase):
    def test_audit_sentinel_distinguishes_real_100_from_legacy(self):
        from scripts.audit_ml_missingness import analyze_records

        rows = [
            # Legacy row (fv=1) with 100.0 support and resistance sentinel
            {
                "timestamp": "2026-07-20T10:00:00Z",
                "feature_version": 1,
                "structure_features": {"dist_to_nearest_support": 100.0, "dist_to_nearest_resistance": 100.0},
            },
            # Epoch 3 row written BEFORE TASK-195 cutoff (2026-07-31 14:00 UTC < 16:09:48Z cutoff)
            # Old collector was still generating 100.0 sentinels -> flagged as legacy_100_support
            {
                "timestamp": "2026-07-31T14:00:00Z",
                "feature_version": 3,
                "structure_features": {"dist_to_nearest_support": 100.0},
            },
            # Post-TASK-195 row (2026-07-31 17:00 UTC >= 16:09:48Z cutoff) with genuine 100.0 market distance
            {
                "timestamp": "2026-07-31T17:00:00Z",
                "feature_version": 3,
                "structure_features": {"dist_to_nearest_support": 100.0},
            },
            # Modern row (fv=4) with legitimate 100.0 market distances
            {
                "timestamp": "2026-09-10T10:00:00Z",
                "feature_version": 4,
                "structure_features": {"dist_to_nearest_support": 100.0, "dist_to_nearest_resistance": 100.0},
            },
            # Row with corrupted feature_version and unversioned row to exercise fallbacks
            {
                "timestamp": "2026-07-25T10:00:00Z",
                "feature_version": "corrupted_non_int",
                "structure_features": {"dist_to_nearest_support": 50.0},
            },
            {
                "timestamp": "2026-08-01T10:00:00Z",
                "feature_version": None,
                "structure_features": {"dist_to_nearest_support": 50.0},
            },
            # Corrupted row with negative distance
            {
                "timestamp": "2026-09-11T10:00:00Z",
                "feature_version": 4,
                "structure_features": {"dist_to_nearest_support": -15.0},
            },
            # Row with zero-injected option payload (MANM-49 defect)
            {
                "timestamp": "2026-09-12T10:00:00Z",
                "feature_version": 4,
                "raw_atm_oi": {
                    "ce": {"iv": 0, "oi": 0, "gamma": 0, "theta": 0, "vega": 0},
                    "pe": {"iv": 0, "oi": 0, "gamma": 0, "theta": 0, "vega": 0},
                },
                "greek_features": {"net_delta": 0.0},
                "oi_features": {
                    "strikes_with_ce_oi": 1, "max_ce_oi": 0, "p85_ce_oi": 0,
                    "strikes_with_pe_oi": 1, "max_pe_oi": 0, "p85_pe_oi": 0,
                },
            },
        ]

        res = analyze_records(rows)
        s = res["sentinels"]
        # Legacy rows (v1 row + pre-TASK-195 v3 row) increment legacy_100_support
        self.assertEqual(s["legacy_100_support"], 2)
        self.assertEqual(s["legacy_100_resistance"], 1)
        # Modern rows (post-TASK-195 v3 row + v4 row) increment real_100_support and real_100_resistance
        self.assertEqual(s["real_100_support"], 2)
        self.assertEqual(s["real_100_resistance"], 1)
        # Negative distance flagged
        self.assertEqual(s["negative_sentinels"], 1)
        # Zero-injected options flagged
        self.assertEqual(s["zero_injected_options"], 1)
        # Check that legacy 100.0 sentinels and negative distances are treated as missing observations:
        # Rows 0, 1 (legacy 100) and Row 6 (-15.0) and Row 7 (no support) are missing support -> 4 / 8 missing
        self.assertEqual(res["overall"]["missing_support"], 4)
        # Rows 0 (legacy 100) and Rows 1, 2, 4, 5, 6, 7 (no resistance) are missing resistance -> 7 / 8 missing
        self.assertEqual(res["overall"]["missing_resistance"], 7)
        # Check that zero-injected option payload causes net_delta and oi_shape to be treated as missing
        # In Row 7, net_delta and oi_shape are poisoned by zero-injected options, so missing = True
        self.assertEqual(res["overall"]["missing_net_delta"], 8)
        self.assertEqual(res["overall"]["missing_oi_shape"], 8)
        # Verify by_date aggregation exists and captures unique dates
        self.assertIn("by_date", res)
        dates_recorded = [d["date"] for d in res["by_date"]]
        self.assertIn("2026-07-20", dates_recorded)
        self.assertIn("2026-09-10", dates_recorded)

        # Verify prediction rows auditing
        pred_rows = [
            {"timestamp": "2026-09-12T10:00:00Z", "feature_snapshot": {"iv_features__iv_level": 0, "greek_features__total_vega": 0, "greek_features__gamma_theta_ratio": 0, "oi_features__atm_total_oi": 0}},
            {"timestamp": "2026-09-12T10:01:00Z", "feature_snapshot": {"iv_features__iv_level": 12.5, "greek_features__total_vega": 20.0, "greek_features__gamma_theta_ratio": 0.05, "oi_features__atm_total_oi": 50000}},
        ]
        res_with_preds = analyze_records(rows, prediction_rows=pred_rows)
        self.assertEqual(res_with_preds["sentinels"]["prediction_rows_evaluated"], 2)
        self.assertEqual(res_with_preds["sentinels"]["zero_injected_predictions"], 1)

    def test_is_zero_injected_option_payload(self):
        from scripts.audit_ml_missingness import is_zero_injected_option_payload

        # Normal options payload
        valid_raw = {
            "ce": {"iv": 11.4, "oi": 6035315, "gamma": 0.00116, "vega": 11.96},
            "pe": {"iv": 11.6, "oi": 5326295, "gamma": 0.00113, "vega": 11.96},
        }
        self.assertFalse(is_zero_injected_option_payload(valid_raw))

        # MANM-49 all-zero payload in raw_atm_oi
        zero_raw = {
            "ce": {"iv": 0, "oi": 0, "gamma": 0, "theta": 0, "vega": 0},
            "pe": {"iv": 0, "oi": 0, "gamma": 0, "theta": 0, "vega": 0},
        }
        self.assertTrue(is_zero_injected_option_payload(zero_raw))

        # Zero-injected derived features when raw_atm_oi is None
        zero_greek = {"total_vega": 0.0, "gamma_theta_ratio": 0.0}
        zero_oi = {"total_ce_oi": 0, "total_pe_oi": 0, "atm_ce_oi": 0, "atm_pe_oi": 0}
        self.assertTrue(is_zero_injected_option_payload(None, zero_greek, zero_oi))

        # Empty / non-zero derived features
        self.assertFalse(is_zero_injected_option_payload(None, {}, {}))
        self.assertFalse(is_zero_injected_option_payload(None, {"total_vega": 5.0}, {"total_ce_oi": 1000}))

    def test_is_zero_injected_prediction_snapshot(self):
        from scripts.audit_ml_missingness import is_zero_injected_prediction_snapshot

        # Clean snapshot (None for absent option features)
        clean = {
            "iv_features__iv_level": None,
            "greek_features__total_vega": None,
            "greek_features__gamma_theta_ratio": None,
            "oi_features__atm_total_oi": None,
        }
        self.assertFalse(is_zero_injected_prediction_snapshot(clean))

        # Zero-injected MANM-49 snapshot
        poisoned = {
            "iv_features__iv_level": 0.0,
            "greek_features__total_vega": 0.0,
            "greek_features__gamma_theta_ratio": 0.0,
            "oi_features__atm_total_oi": 0,
        }
        self.assertTrue(is_zero_injected_prediction_snapshot(poisoned))

        # Legitimate market snapshot
        market = {
            "iv_features__iv_level": 12.3,
            "greek_features__total_vega": 15.0,
            "greek_features__gamma_theta_ratio": 0.005,
            "oi_features__atm_total_oi": 1000000,
        }
        self.assertFalse(is_zero_injected_prediction_snapshot(market))

        # Edge cases: None, empty dict, non-dict, non-numeric values
        self.assertFalse(is_zero_injected_prediction_snapshot(None))
        self.assertFalse(is_zero_injected_prediction_snapshot({}))
        self.assertFalse(is_zero_injected_prediction_snapshot("invalid-json"))
        self.assertFalse(is_zero_injected_prediction_snapshot({
            "iv_features__iv_level": "not_a_number",
            "greek_features__total_vega": 0,
            "greek_features__gamma_theta_ratio": 0,
            "oi_features__atm_total_oi": 0,
        }))

    def test_generate_markdown_report_supports_filename_without_dir(self):
        from scripts.audit_ml_missingness import generate_markdown_report

        mock_audit_res = {
            "overall": {
                "total_rows": 10,
                "missing_net_delta": 2,
                "pct_missing_net_delta": 20.0,
                "missing_oi_shape": 1,
                "pct_missing_oi_shape": 10.0,
                "missing_support": 3,
                "pct_missing_support": 30.0,
                "missing_resistance": 1,
                "pct_missing_resistance": 10.0,
                "missing_trend_continuation": 0,
                "pct_missing_trend_continuation": 0.0,
            },
            "sentinels": {
                "legacy_100_support": 0,
                "legacy_100_resistance": 0,
                "real_100_support": 2,
                "real_100_resistance": 1,
                "negative_sentinels": 0,
                "zero_injected_options": 0,
                "zero_injected_predictions": 0,
                "prediction_rows_evaluated": 5,
            },
            "by_version": [
                {
                    "feature_version": 4,
                    "rows": 10,
                    "pct_of_total": 100.0,
                    "missing_net_delta_pct": 0.0,
                    "missing_oi_shape_pct": 0.0,
                    "missing_support_pct": 30.0,
                    "missing_resistance_pct": 10.0,
                    "missing_trend_continuation_pct": 0.0,
                }
            ],
            "by_session": [
                {
                    "session": "AFTERNOON_CLOSE (14:00-15:30)",
                    "rows": 5,
                    "pct_of_total": 50.0,
                    "missing_net_delta_pct": 0.0,
                    "missing_oi_shape_pct": 0.0,
                    "missing_support_pct": 40.0,
                    "missing_resistance_pct": 10.0,
                    "missing_trend_continuation_pct": 0.0,
                },
                {
                    "session": "MORNING_OPEN (09:15-10:15)",
                    "rows": 5,
                    "pct_of_total": 50.0,
                    "missing_net_delta_pct": 0.0,
                    "missing_oi_shape_pct": 0.0,
                    "missing_support_pct": 20.0,
                    "missing_resistance_pct": 25.0,
                    "missing_trend_continuation_pct": 0.0,
                },
            ],
            "by_date": [
                {
                    "date": "2026-09-10",
                    "rows": 10,
                    "feature_versions": [4],
                    "missing_net_delta_pct": 0.0,
                    "missing_oi_shape_pct": 0.0,
                    "missing_support_pct": 30.0,
                    "missing_resistance_pct": 10.0,
                    "missing_trend_continuation_pct": 0.0,
                }
            ],
            "by_week": [],
        }

        # Plain filename without directory component
        test_filename = "test_audit_plain_name.md"
        try:
            generate_markdown_report(mock_audit_res, test_filename)
            self.assertTrue(os.path.exists(test_filename))
            with open(test_filename, "r") as f:
                content = f.read()
            # Verify clean PASS for 0 legacy sentinels and zero-injected options
            self.assertIn("PASS. Zero legacy sentinels, negative distances, or zero-injected payloads detected across `ml_collection` and `ml_predictions`", content)
            self.assertIn("Zero-Injected Option Payloads (`ml_collection`):", content)
            self.assertIn("Zero-Injected Prediction Snapshots (`ml_predictions`):", content)
            self.assertIn("Legitimate 100.0 Market Distances (Post-TASK-195):", content)
            # Verify dynamically derived session observations
            self.assertIn("AFTERNOON_CLOSE (14:00-15:30)` (40.0%)", content)
            self.assertIn("MORNING_OPEN (09:15-10:15)` (25.0%)", content)
            # Verify dynamically derived v4 key finding
            self.assertIn("Key Finding (Version 4 Modern Suite):", content)
            self.assertIn("`net_delta`: 0.0%", content)
            # Verify Daily Missingness Breakdown table
            self.assertIn("## 4. Daily Missingness Breakdown", content)
            self.assertIn("`2026-09-10`", content)
            self.assertIn("## 5. Weekly Temporal Progression", content)
        finally:
            if os.path.exists(test_filename):
                os.remove(test_filename)

        # Also verify when v4 is absent
        mock_no_v4 = dict(mock_audit_res)
        mock_no_v4["by_version"] = [
            {
                "feature_version": 1,
                "rows": 10,
                "pct_of_total": 100.0,
                "missing_net_delta_pct": 100.0,
                "missing_oi_shape_pct": 100.0,
                "missing_support_pct": 50.0,
                "missing_resistance_pct": 50.0,
                "missing_trend_continuation_pct": 100.0,
            }
        ]
        test_no_v4 = "test_audit_no_v4.md"
        try:
            generate_markdown_report(mock_no_v4, test_no_v4)
            with open(test_no_v4, "r") as f:
                content_no_v4 = f.read()
            self.assertIn("Version 4 records are not present in the evaluated sample", content_no_v4)
        finally:
            if os.path.exists(test_no_v4):
                os.remove(test_no_v4)

    def test_get_market_session(self):
        from scripts.audit_ml_missingness import get_market_session

        self.assertEqual(get_market_session(None), "UNKNOWN")

        # 03:00 UTC = 08:30 IST -> PRE_MARKET (< 09:15)
        dt_pre = datetime(2026, 9, 10, 3, 0, tzinfo=timezone.utc)
        self.assertEqual(get_market_session(dt_pre), "PRE_MARKET")

        # 04:00 UTC = 09:30 IST -> MORNING_OPEN (09:15-10:15)
        dt_open = datetime(2026, 9, 10, 4, 0, tzinfo=timezone.utc)
        self.assertEqual(get_market_session(dt_open), "MORNING_OPEN (09:15-10:15)")

        # 06:00 UTC = 11:30 IST -> MID_DAY (10:15-14:00)
        dt_mid = datetime(2026, 9, 10, 6, 0, tzinfo=timezone.utc)
        self.assertEqual(get_market_session(dt_mid), "MID_DAY (10:15-14:00)")

        # 09:00 UTC = 14:30 IST -> AFTERNOON_CLOSE (14:00-15:30)
        dt_close = datetime(2026, 9, 10, 9, 0, tzinfo=timezone.utc)
        self.assertEqual(get_market_session(dt_close), "AFTERNOON_CLOSE (14:00-15:30)")

        # 11:00 UTC = 16:30 IST -> POST_MARKET (> 15:30)
        dt_post = datetime(2026, 9, 10, 11, 0, tzinfo=timezone.utc)
        self.assertEqual(get_market_session(dt_post), "POST_MARKET")

        # Naive datetime
        dt_naive = datetime(2026, 9, 10, 4, 0)
        self.assertEqual(get_market_session(dt_naive), "MORNING_OPEN (09:15-10:15)")

        # Exception handling fallback
        dt_err = MagicMock()
        dt_err.astimezone.side_effect = Exception("tz error")
        self.assertEqual(get_market_session(dt_err), "UNKNOWN")

    def test_load_json(self):
        from scripts.audit_ml_missingness import _load_json

        self.assertEqual(_load_json({"a": 1}), {"a": 1})
        self.assertEqual(_load_json('{"a": 2}'), {"a": 2})
        self.assertEqual(_load_json("not-json"), {})
        self.assertEqual(_load_json(12345), {})

    def test_fetch_all_ml_collection_pagination_and_probe(self):
        from scripts.audit_ml_missingness import fetch_all_ml_collection

        mock_sb = MagicMock()
        # Probe success
        mock_sb.table.return_value.select.return_value.limit.return_value.execute.return_value = MagicMock()
        # Batch return with multi-page pagination (page 1 returns 2, page 2 returns 0)
        page1 = [{"timestamp": "2026-09-10T10:00:00Z"}, {"timestamp": "2026-09-10T10:01:00Z"}]
        mock_sb.table.return_value.select.return_value.order.return_value.range.return_value.execute.side_effect = [
            MagicMock(data=page1),
            MagicMock(data=[]),
        ]

        rows = fetch_all_ml_collection(mock_sb, page_size=2)
        self.assertEqual(len(rows), 2)

        # Probe failure fallback
        mock_sb.table.return_value.select.return_value.limit.return_value.execute.side_effect = Exception("No col")
        mock_sb.table.return_value.select.return_value.order.return_value.range.return_value.execute.side_effect = [
            MagicMock(data=[{"timestamp": "2026-09-10T10:00:00Z"}])
        ]
        rows2 = fetch_all_ml_collection(mock_sb, limit=1, page_size=100)
        self.assertEqual(len(rows2), 1)

    def test_generate_markdown_report_with_dir_and_sentinel_warning(self):
        from scripts.audit_ml_missingness import generate_markdown_report
        import shutil

        test_dir = "tmp_qa_test_report_dir"
        test_file = os.path.join(test_dir, "report.md")

        mock_audit_res = {
            "overall": {
                "total_rows": 10,
                "missing_net_delta": 2,
                "pct_missing_net_delta": 20.0,
                "missing_oi_shape": 1,
                "pct_missing_oi_shape": 10.0,
                "missing_support": 3,
                "pct_missing_support": 30.0,
                "missing_resistance": 1,
                "pct_missing_resistance": 10.0,
                "missing_trend_continuation": 0,
                "pct_missing_trend_continuation": 0.0,
            },
            "sentinels": {
                "legacy_100_support": 1,
                "legacy_100_resistance": 1,
                "real_100_support": 0,
                "real_100_resistance": 0,
                "negative_sentinels": 1,
                "zero_injected_options": 1,
                "zero_injected_predictions": 1,
                "prediction_rows_evaluated": 10,
            },
            "by_version": [
                {
                    "feature_version": 1,
                    "rows": 5,
                    "pct_of_total": 50.0,
                    "missing_net_delta_pct": 100.0,
                    "missing_oi_shape_pct": 100.0,
                    "missing_support_pct": 20.0,
                    "missing_resistance_pct": 20.0,
                    "missing_trend_continuation_pct": 100.0,
                }
            ],
            "by_session": [],
            "by_week": [
                {
                    "week": "2026-W37",
                    "rows": 10,
                    "feature_versions": [1, 4],
                    "missing_net_delta_pct": 10.0,
                    "missing_oi_shape_pct": 5.0,
                    "missing_support_pct": 10.0,
                    "missing_resistance_pct": 10.0,
                    "missing_trend_continuation_pct": 0.0,
                }
            ],
        }

        try:
            generate_markdown_report(mock_audit_res, test_file)
            self.assertTrue(os.path.exists(test_file))
            with open(test_file, "r") as f:
                content = f.read()
            self.assertIn("WARNING. Detected 5 legacy artifact(s) remaining in historical rows (1 legacy support, 1 legacy resistance, 1 negative, 1 zero-injected collection payloads, 1 zero-injected prediction snapshots)", content)
            self.assertIn("2026-W37", content)
        finally:
            if os.path.exists(test_dir):
                shutil.rmtree(test_dir)

    def test_fetch_ml_predictions_pagination_and_probe(self):
        from scripts.audit_ml_missingness import fetch_ml_predictions

        mock_sb = MagicMock()
        # Probe success
        mock_sb.table.return_value.select.return_value.limit.return_value.execute.return_value = MagicMock()
        page1 = [{"timestamp": "2026-09-10T10:00:00Z", "feature_snapshot": {}}, {"timestamp": "2026-09-10T10:01:00Z", "feature_snapshot": {}}]
        mock_sb.table.return_value.select.return_value.order.return_value.range.return_value.execute.side_effect = [
            MagicMock(data=page1),
            MagicMock(data=[]),
        ]

        rows = fetch_ml_predictions(mock_sb, page_size=2)
        self.assertEqual(len(rows), 2)

        # Exercise limit branch
        mock_sb.table.return_value.select.return_value.order.return_value.range.return_value.execute.side_effect = [
            MagicMock(data=[page1[0]])
        ]
        rows_limited = fetch_ml_predictions(mock_sb, limit=1, page_size=100)
        self.assertEqual(len(rows_limited), 1)

        # Batch query exception during pagination
        mock_sb.table.return_value.select.return_value.order.return_value.range.return_value.execute.side_effect = Exception("Network err")
        rows_err = fetch_ml_predictions(mock_sb, page_size=100)
        self.assertEqual(len(rows_err), 0)

        # Probe failure fallback (table missing or RLS blocked)
        mock_sb.table.return_value.select.return_value.limit.return_value.execute.side_effect = Exception("No table")
        rows2 = fetch_ml_predictions(mock_sb, limit=1, page_size=100)
        self.assertEqual(len(rows2), 0)

    @patch("scripts.audit_ml_missingness.create_client")
    @patch("scripts.audit_ml_missingness.fetch_all_ml_collection")
    @patch("scripts.audit_ml_missingness.fetch_ml_predictions")
    @patch("scripts.audit_ml_missingness.generate_markdown_report")
    def test_main_cli(self, mock_gen, mock_fetch_preds, mock_fetch, mock_create):
        from scripts.audit_ml_missingness import main

        # Case 1: rows empty
        mock_fetch.return_value = []
        mock_fetch_preds.return_value = []
        with patch("sys.argv", ["audit_ml_missingness.py", "--limit", "10"]):
            main()
            mock_gen.assert_not_called()

        # Case 2: rows returned
        mock_fetch.return_value = [{"timestamp": "2026-09-10T10:00:00Z", "feature_version": 4}]
        mock_fetch_preds.return_value = []
        with patch("sys.argv", ["audit_ml_missingness.py", "--limit", "10", "--output", "test_main_report.md"]):
            main()
            mock_gen.assert_called_once()


if __name__ == "__main__":
    unittest.main()


