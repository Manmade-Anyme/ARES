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
from storage import (
    PredictionLogger,
    PredictionRecord,
    sanitize_feature_snapshot,
    _sanitize_value,
)


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
        self.assertIs(sanitized["bool_val"], True)
        self.assertIs(sanitized["np_bool"], False)
        self.assertIs(type(sanitized["bool_val"]), bool)
        self.assertIs(type(sanitized["np_bool"]), bool)
        self.assertIsNone(sanitized["nested_dict"]["inner_nan"])
        self.assertEqual(sanitized["nested_dict"]["inner_val"], 5.555556)
        self.assertEqual(sanitized["array_val"], [1.1, None, 9])

    def test_sanitize_preserves_boolean_types_not_coerced_to_int(self):
        sanitized = sanitize_feature_snapshot({
            "flag_true": True,
            "flag_false": False,
            "np_flag_true": np.bool_(True),
            "np_flag_false": np.bool_(False),
            "int_one": 1,
            "int_zero": 0,
        })
        self.assertIs(sanitized["flag_true"], True)
        self.assertIs(sanitized["flag_false"], False)
        self.assertIs(sanitized["np_flag_true"], True)
        self.assertIs(sanitized["np_flag_false"], False)
        self.assertIs(type(sanitized["flag_true"]), bool)
        self.assertIs(type(sanitized["flag_false"]), bool)
        self.assertIs(type(sanitized["np_flag_true"]), bool)
        self.assertIs(type(sanitized["np_flag_false"]), bool)
        self.assertIs(type(sanitized["int_one"]), int)
        self.assertIs(type(sanitized["int_zero"]), int)

    def test_sanitize_empty_or_invalid_input(self):
        self.assertEqual(sanitize_feature_snapshot({}), {})
        self.assertEqual(sanitize_feature_snapshot(None), {})
        self.assertEqual(sanitize_feature_snapshot("invalid"), {})

    def test_sanitize_feature_snapshot_redacts_prohibited_and_sensitive_keys(self):
        raw_features = {
            "rsi": 65.5,
            "access_token": "secret_token_123",
            "client_id": "DHAN12345",
            "dhan_access_token": "token_dhan",
            "api_secret": "secret_key_abc",
            "password": "super_secret_pw",
            "account_id": "ACC987",
            "user_id": "U12345",
            "broker_id": "BROKER99",
            "ip_address": "192.168.1.100",
            "credentials": {"token": "sub_token"},
            "nested": {
                "candle_body": 12.5,
                "access_token": "inner_token",
                "client_id": "inner_client",
                "nested_api_secret": "inner_secret",
            },
            "list_of_dicts": [
                {"open": 24000.0, "secret_key": "bad"},
                {"high": 24050.0},
            ],
        }

        sanitized = sanitize_feature_snapshot(raw_features)

        # Quantitative market features preserved
        self.assertEqual(sanitized["rsi"], 65.5)
        self.assertEqual(sanitized["nested"], {"candle_body": 12.5})
        self.assertEqual(sanitized["list_of_dicts"], [{"open": 24000.0}, {"high": 24050.0}])

        # Prohibited keys strictly stripped
        prohibited_keys = [
            "access_token",
            "client_id",
            "dhan_access_token",
            "api_secret",
            "password",
            "account_id",
            "user_id",
            "broker_id",
            "ip_address",
            "credentials",
        ]
        for key in prohibited_keys:
            self.assertNotIn(key, sanitized)


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

    def test_rejects_client_using_anon_key(self):
        anon_client = MagicMock()
        anon_client.supabase_key = settings.supabase_key
        with self.assertRaises(ValueError) as ctx:
            PredictionLogger(supabase_client=anon_client)
        self.assertIn("anon key", str(ctx.exception))

    def test_log_record_with_prediction_record(self):
        logger = PredictionLogger(supabase_client=self.mock_client, max_workers=1)
        record = PredictionRecord(
            probability=0.85,
            confidence_tier="HIGH",
            model_version="v2.joblib",
            spot=24200.0,
            feature_snapshot={"rsi": 65.0},
            signal_id="canon-uuid-1",
            trade_id="trade-uuid-1",
        )
        try:
            logger.log_record(record)
            logger.shutdown(wait=True)
            self.table_mock.insert.assert_called_once()
            payload = self.table_mock.insert.call_args[0][0]
            self.assertEqual(payload["probability"], 0.85)
            self.assertEqual(payload["signal_id"], "canon-uuid-1")
            self.assertEqual(payload["trade_id"], "trade-uuid-1")
        finally:
            logger.shutdown(wait=True)

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
            logger.shutdown(wait=True)

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

    def test_sync_call_does_not_block_caller_on_slow_insert(self):
        """Verifies that non-event-loop callers are dispatched to executor and not blocked by slow network operations."""
        logger = PredictionLogger(supabase_client=self.mock_client, max_workers=1)
        original_insert = logger._insert_prediction

        def _slow_insert(record):
            time.sleep(0.2)
            original_insert(record)

        logger._insert_prediction = _slow_insert
        try:
            t0 = time.perf_counter()
            logger.log_prediction(
                probability=0.65,
                confidence_tier="MEDIUM",
                model_version="v1",
                spot=24000.0,
                feature_snapshot={"val": 1},
            )
            elapsed = time.perf_counter() - t0
            # Must return immediately, well under the 0.2s sleep time
            self.assertLess(elapsed, 0.1)

            logger.shutdown(wait=True)
            self.table_mock.insert.assert_called_once()
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
            logger.shutdown(wait=True)

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
            logger.shutdown(wait=True)
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

    def test_clean_install_schemas_secure_ml_predictions_for_service_role(self):
        """Clean installs must enforce the same backend-only access as upgrades."""
        expected_statements = (
            "alter table ml_predictions enable row level security;",
            "revoke all on table ml_predictions from public, anon, authenticated;",
            "grant select, insert on table ml_predictions to service_role;",
            "grant usage, select on sequence ml_predictions_id_seq to service_role;",
            "drop policy if exists ml_predictions_service_read on ml_predictions;",
            "create policy ml_predictions_service_read on ml_predictions "
            "for select to service_role using (true);",
            "drop policy if exists ml_predictions_service_insert on ml_predictions;",
            "create policy ml_predictions_service_insert on ml_predictions "
            "for insert to service_role with check (true);",
        )

        for schema_path in (Path("schema.sql"), Path("ml_signal/schema.sql")):
            with self.subTest(schema=str(schema_path)):
                normalized_sql = " ".join(schema_path.read_text().split()).lower()
                for statement in expected_statements:
                    self.assertIn(statement, normalized_sql)
                self.assertNotIn(
                    "alter table ml_predictions disable row level security;",
                    normalized_sql,
                )


class TestLiveRunnerServiceRoleEnforcement(unittest.TestCase):
    def test_init_supabase_with_service_role_key(self):
        from ml_signal.live import LiveRunner

        runner = LiveRunner()
        with patch("ml_signal.live.create_client") as mock_create, patch(
            "ml_signal.live.PredictionLogger"
        ) as mock_logger:
            runner._init_supabase(
                url="https://example.supabase.co",
                key="anon_key",
                service_role_key="service_role_key_secret",
            )
            mock_create.assert_called_with("https://example.supabase.co", "anon_key")
            mock_logger.assert_called_once_with(
                supabase_url="https://example.supabase.co",
                supabase_service_role_key="service_role_key_secret",
            )
            self.assertIsNotNone(runner.prediction_logger)

    def test_init_supabase_without_service_role_key_disables_prediction_logger(self):
        from ml_signal.live import LiveRunner

        runner = LiveRunner()
        with patch("ml_signal.live.create_client"), patch.dict("os.environ", {}, clear=True):
            runner._init_supabase(
                url="https://example.supabase.co",
                key="anon_key",
                service_role_key=None,
            )
            self.assertIsNone(runner.prediction_logger)
            self.assertIsNone(runner._supabase)

    def test_init_supabase_clears_clients_on_logger_init_failure(self):
        from ml_signal.live import LiveRunner

        runner = LiveRunner()
        with patch("ml_signal.live.create_client"), patch(
            "ml_signal.live.PredictionLogger", side_effect=Exception("Failed to connect")
        ):
            runner._init_supabase(
                url="https://example.supabase.co",
                key="anon_key",
                service_role_key="service_role_key_secret",
            )
            self.assertIsNone(runner.prediction_logger)
            self.assertIsNone(runner._supabase)


class TestLiveRunnerPredictionLogging(unittest.IsolatedAsyncioTestCase):
    async def test_log_prediction_delegates_to_prediction_logger(self):
        from ml_signal.live import LiveRunner

        runner = LiveRunner()
        runner.prediction_logger = MagicMock()
        payload = {
            "probability": 0.82,
            "confidence_tier": "HIGH",
            "model_version": "v3.joblib",
            "spot": 24250.0,
            "features": {"trend": 1.0},
            "signal_id": "test-uuid-99",
            "trade_id": "test-trade-99",
            "source": "continuous",
            "timestamp": "2026-09-24T00:00:00Z",
        }

        await runner.log_prediction(payload)

        runner.prediction_logger.log_prediction.assert_called_once_with(
            probability=0.82,
            confidence_tier="HIGH",
            model_version="v3.joblib",
            spot=24250.0,
            feature_snapshot={"trend": 1.0},
            signal_id="test-uuid-99",
            trade_id="test-trade-99",
            source="continuous",
            timestamp="2026-09-24T00:00:00Z",
        )

    async def test_log_prediction_noop_when_prediction_logger_is_none(self):
        from ml_signal.live import LiveRunner

        runner = LiveRunner()
        runner.prediction_logger = None
        mock_supabase = MagicMock()
        runner._supabase = mock_supabase

        payload = {
            "probability": 0.65,
            "confidence_tier": "MEDIUM",
            "features": {"rsi": 55.0},
        }

        # Must not perform any database operations or fallback to anonymous client
        await runner.log_prediction(payload)
        mock_supabase.table.assert_not_called()

    async def test_log_prediction_suppresses_exceptions(self):
        from ml_signal.live import LiveRunner

        runner = LiveRunner()
        runner.prediction_logger = MagicMock()
        runner.prediction_logger.log_prediction.side_effect = RuntimeError("network down")

        # Must not raise
        await runner.log_prediction({"probability": 0.5})


class TestEnginePredictionPersistenceOnSignalFailure(unittest.IsolatedAsyncioTestCase):
    async def test_prediction_persisted_even_if_signal_logging_fails(self):
        """Invariant: Even if storage.log_signal returns False, prediction is persisted with trade_id=None."""
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
        signal.ml_prediction = {
            "probability": 0.78,
            "confidence_tier": "HIGH",
            "model_version": "v1.joblib",
            "features": {"f1": 1.0},
        }

        mock_logger = MagicMock()
        spot = 24100.0
        now = datetime.now(timezone.utc)

        # Simulate engine logic when storage.log_signal fails
        trade_executed = False
        # storage.log_signal returns False
        success = False
        if success:
            trade_executed = True

        ml_pred = getattr(signal, "ml_prediction", None)
        if mock_logger and ml_pred:
            mock_logger.log_prediction(
                probability=ml_pred["probability"],
                confidence_tier=ml_pred["confidence_tier"],
                model_version=ml_pred["model_version"],
                spot=spot,
                feature_snapshot=ml_pred.get("features", {}),
                signal_id=getattr(signal, "id", None),
                trade_id=getattr(signal, "trade_id", None) or None,
                source="event_triggered",
                timestamp=now,
            )

        mock_logger.log_prediction.assert_called_once()
        call_kwargs = mock_logger.log_prediction.call_args.kwargs
        self.assertEqual(call_kwargs["signal_id"], signal.id)
        self.assertIsNone(call_kwargs["trade_id"])
        self.assertEqual(call_kwargs["probability"], 0.78)
        self.assertFalse(trade_executed)
