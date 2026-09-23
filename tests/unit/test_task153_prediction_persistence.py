import asyncio
from datetime import datetime, timezone
import math
from pathlib import Path
import time
import unittest
from unittest.mock import MagicMock, patch
import uuid
import numpy as np

from config import settings
from models import AresSignal, SetupType, Direction
from position_manager import PositionManager
from storage import PredictionLogger, sanitize_feature_snapshot, _sanitize_value


class TestFeatureSanitization(unittest.TestCase):
    def test_sanitize_feature_snapshot_numpy_and_special_values(self):
        dt = datetime(2026, 9, 12, 10, 30, tzinfo=timezone.utc)
        raw_features = {
            "np_float32": np.float32(12.3456789),
            "np_float64": np.float64(98.7654321),
            "np_int32": np.int32(42),
            "np_int64": np.int64(100),
            "py_float": 1.23456789,
            "py_int": 7,
            "nan_val": float("nan"),
            "np_nan": np.nan,
            "inf_val": float("inf"),
            "neg_inf": float("-inf"),
            "np_inf": np.inf,
            "none_val": None,
            "dt_val": dt,
            "bool_val": True,
            "np_bool": np.bool_(False),
            "nested_dict": {
                "inner_nan": float("nan"),
                "inner_val": np.float64(5.5555556),
            },
            "array_val": [np.float32(1.1), float("nan"), np.int64(9)],
        }

        sanitized = sanitize_feature_snapshot(raw_features)

        self.assertEqual(sanitized["np_float32"], 12.345679)
        self.assertEqual(sanitized["np_float64"], 98.765432)
        self.assertIsInstance(sanitized["np_float32"], float)
        self.assertIsInstance(sanitized["np_int32"], int)
        self.assertEqual(sanitized["np_int32"], 42)
        self.assertEqual(sanitized["np_int64"], 100)
        self.assertEqual(sanitized["py_float"], 1.234568)
        self.assertIsNone(sanitized["nan_val"])
        self.assertIsNone(sanitized["np_nan"])
        self.assertIsNone(sanitized["inf_val"])
        self.assertIsNone(sanitized["neg_inf"])
        self.assertIsNone(sanitized["np_inf"])
        self.assertIsNone(sanitized["none_val"])
        self.assertIn("2026-09-12T10:30:00", sanitized["dt_val"])
        self.assertTrue(sanitized["bool_val"])
        self.assertFalse(sanitized["np_bool"])
        self.assertIsNone(sanitized["nested_dict"]["inner_nan"])
        self.assertEqual(sanitized["nested_dict"]["inner_val"], 5.555556)
        self.assertEqual(sanitized["array_val"], [1.1, None, 9])

    def test_sanitize_empty_or_invalid_input(self):
        self.assertEqual(sanitize_feature_snapshot({}), {})
        self.assertEqual(sanitize_feature_snapshot(None), {})
        self.assertEqual(sanitize_feature_snapshot("invalid"), {})


class TestPredictionLogger(unittest.TestCase):
    def setUp(self):
        self.mock_client = MagicMock()
        self.table_mock = MagicMock()
        self.insert_mock = MagicMock()
        self.execute_mock = MagicMock()

        self.mock_client.table.return_value = self.table_mock
        self.table_mock.insert.return_value = self.insert_mock
        self.insert_mock.execute.return_value = self.execute_mock

    def test_requires_service_role_key_when_no_client(self):
        with patch.object(settings, "supabase_service_role_key", ""):
            with self.assertRaises(ValueError) as ctx:
                PredictionLogger()
            self.assertIn("supabase_service_role_key", str(ctx.exception))

    def test_sync_fallback_when_no_event_loop(self):
        logger = PredictionLogger(supabase_client=self.mock_client, max_workers=1)
        try:
            logger.log_prediction(
                probability=0.72,
                confidence_tier="HIGH",
                model_version="v9.joblib",
                spot=24500.5,
                feature_snapshot={"trend_score": 3.0, "nan_test": float("nan")},
                signal_id="1234",
                trade_id="3fa85f64-5717-4562-b3fc-2c963f66afa6",
                source="event_triggered",
            )

            self.mock_client.table.assert_called_with("ml_predictions")
            self.table_mock.insert.assert_called_once()
            record = self.table_mock.insert.call_args[0][0]

            self.assertEqual(record["probability"], 0.72)
            self.assertEqual(record["confidence_tier"], "HIGH")
            self.assertEqual(record["model_version"], "v9.joblib")
            self.assertEqual(record["spot"], 24500.5)
            self.assertEqual(record["signal_id"], "1234")
            self.assertEqual(record["trade_id"], "3fa85f64-5717-4562-b3fc-2c963f66afa6")
            self.assertEqual(record["source"], "event_triggered")
            self.assertEqual(record["feature_snapshot"]["trend_score"], 3.0)
            self.assertIsNone(record["feature_snapshot"]["nan_test"])
        finally:
            logger.shutdown(wait=True)

    def test_missing_optional_fields_persist_safely(self):
        logger = PredictionLogger(supabase_client=self.mock_client, max_workers=1)
        try:
            logger.log_prediction(
                probability=0.45,
                confidence_tier="MEDIUM",
                model_version="v2",
                spot=24000.0,
                feature_snapshot={},
                signal_id=None,
                trade_id=None,
            )

            record = self.table_mock.insert.call_args[0][0]
            self.assertIsNone(record["signal_id"])
            self.assertIsNone(record["trade_id"])
            self.assertEqual(record["source"], "event_triggered")
        finally:
            logger.shutdown(wait=True)

    def test_exception_suppression_on_insert_failure(self):
        self.insert_mock.execute.side_effect = RuntimeError("PostgREST connection reset")
        logger = PredictionLogger(supabase_client=self.mock_client, max_workers=1)
        try:
            # Must not raise any exception
            logger.log_prediction(
                probability=0.55,
                confidence_tier="MEDIUM",
                model_version="v1",
                spot=24200.0,
                feature_snapshot={"x": 1},
            )
            self.table_mock.insert.assert_called_once()
        finally:
            logger.shutdown(wait=True)

    @patch("storage.create_client")
    def test_initialization_with_service_role_key(self, mock_create):
        mock_create.return_value = self.mock_client
        with patch.object(settings, "supabase_service_role_key", "valid-service-key"):
            logger = PredictionLogger(max_workers=1)
            mock_create.assert_called_with(settings.supabase_url, "valid-service-key")
            logger.shutdown(wait=True)

    def test_dispatch_error_suppression(self):
        logger = PredictionLogger(supabase_client=self.mock_client, max_workers=1)
        try:
            with patch("storage.to_utc_iso", side_effect=Exception("Timestamp formatting failure")):
                # Must not raise
                logger.log_prediction(
                    probability=0.5,
                    confidence_tier="LOW",
                    model_version="v1",
                    spot=24000.0,
                    feature_snapshot={},
                )
        finally:
            logger.shutdown(wait=True)

    def test_sanitize_value_custom_objects(self):
        class CustomObj:
            def __str__(self):
                return "custom_str"
        self.assertEqual(_sanitize_value(CustomObj()), "custom_str")
        self.assertEqual(_sanitize_value("plain_str"), "plain_str")


class TestPredictionLoggerAsync(unittest.IsolatedAsyncioTestCase):
    async def test_async_dispatch_non_blocking(self):
        mock_client = MagicMock()
        table_mock = MagicMock()
        insert_mock = MagicMock()

        mock_client.table.return_value = table_mock
        table_mock.insert.return_value = insert_mock

        logger = PredictionLogger(supabase_client=mock_client, max_workers=2)
        try:
            # Inside async loop, log_prediction should dispatch to worker thread
            logger.log_prediction(
                probability=0.88,
                confidence_tier="HIGH",
                model_version="v9.joblib",
                spot=24150.0,
                feature_snapshot={"gamma": 0.05},
                signal_id="0042",
            )

            # Wait briefly for thread execution
            for _ in range(50):
                if table_mock.insert.called:
                    break
                await asyncio.sleep(0.01)

            table_mock.insert.assert_called_once()
            record = table_mock.insert.call_args[0][0]
            self.assertEqual(record["probability"], 0.88)
            self.assertEqual(record["signal_id"], "0042")
        finally:
            logger.shutdown(wait=True)


class TestSignalTradeIdLinkage(unittest.IsolatedAsyncioTestCase):
    async def test_position_manager_populates_signal_trade_id(self):
        pm = PositionManager()
        def _rpc(name, payload):
            resp = MagicMock()
            resp.data = payload["p_trade_id"]
            m = MagicMock()
            m.execute.return_value = resp
            return m
        pm.supabase = MagicMock()
        pm.supabase.rpc.side_effect = _rpc
        pm.active_trades = []

        signal = AresSignal(
            setup_type=SetupType.FAILED_BREAKOUT,
            direction=Direction.BULLISH,
            trigger_price=24100.0,
            entry_zone=(24090.0, 24110.0),
            stop_loss=24050.0,
            target_1=24150.0,
            target_2=24200.0,
            confidence="HIGH",
            reasons=["Test Reason"],
            timestamp=datetime.now(timezone.utc),
            strike_to_trade=24100,
            option_type="CE",
        )

        self.assertIsNone(signal.trade_id)

        trade_id, status = await pm.add_trade(signal, 24100.0)

        self.assertTrue(bool(trade_id))
        self.assertEqual(signal.trade_id, trade_id)
        # Verify trade_id is a valid UUID
        parsed_uuid = uuid.UUID(trade_id)
        self.assertEqual(str(parsed_uuid), trade_id)

    async def test_position_manager_does_not_set_trade_id_on_failure(self):
        pm = PositionManager()
        pm.supabase = MagicMock()
        pm.supabase.rpc.side_effect = RuntimeError("RPC connection failed")
        pm.active_trades = []

        signal = AresSignal(
            setup_type=SetupType.FAILED_BREAKOUT,
            direction=Direction.BULLISH,
            trigger_price=24100.0,
            entry_zone=(24090.0, 24110.0),
            stop_loss=24050.0,
            target_1=24150.0,
            target_2=24200.0,
            confidence="HIGH",
            reasons=["Test Reason"],
            timestamp=datetime.now(timezone.utc),
            strike_to_trade=24100,
            option_type="CE",
        )

        with self.assertRaises(RuntimeError):
            await pm.add_trade(signal, 24100.0)

        self.assertIsNone(signal.trade_id)


class TestSchemaFilesIntegrity(unittest.TestCase):
    def test_migration_file_content(self):
        migration_path = Path("migrations/2026-09-12-task153-ml-predictions-schema.sql")
        self.assertTrue(migration_path.exists())
        sql = migration_path.read_text()

        self.assertIn("CREATE TABLE IF NOT EXISTS ml_predictions", sql)
        self.assertIn("feature_snapshot jsonb", sql)
        self.assertIn("trade_id uuid", sql)
        self.assertIn("ENABLE ROW LEVEL SECURITY", sql)
        self.assertIn("idx_ml_pred_timestamp", sql)
        self.assertIn("idx_ml_pred_signal", sql)
        self.assertIn("idx_ml_pred_trade", sql)

    def test_migration_backfills_canonical_id_from_legacy_signal_uuid(self):
        sql = Path(
            "migrations/2026-09-12-task153-ml-predictions-schema.sql"
        ).read_text()
        normalized_sql = " ".join(sql.split()).lower()

        self.assertIn("table_schema = current_schema()", normalized_sql)
        self.assertIn("column_name = 'signal_uuid'", normalized_sql)
        self.assertIn(
            "update ml_predictions set signal_id = signal_uuid::text "
            "where signal_uuid is not null "
            "and signal_id is distinct from signal_uuid::text",
            normalized_sql,
        )
        backfill_position = normalized_sql.index(
            "update ml_predictions set signal_id = signal_uuid::text"
        )
        self.assertLess(
            normalized_sql.index("add column if not exists signal_id text"),
            backfill_position,
        )
        self.assertLess(
            backfill_position,
            normalized_sql.index("create index if not exists idx_ml_pred_signal"),
        )

    def test_schema_sql_has_ml_predictions(self):
        schema_path = Path("schema.sql")
        sql = schema_path.read_text()
        self.assertIn("CREATE TABLE IF NOT EXISTS ml_predictions", sql)
        self.assertIn("feature_snapshot jsonb not null", sql)
        self.assertIn("trade_id uuid", sql)

    def test_ml_signal_schema_sql_has_ml_predictions(self):
        schema_path = Path("ml_signal/schema.sql")
        sql = schema_path.read_text()
        self.assertIn("CREATE TABLE IF NOT EXISTS ml_predictions", sql)
        self.assertIn("feature_snapshot jsonb not null", sql)
        self.assertIn("trade_id uuid", sql)
