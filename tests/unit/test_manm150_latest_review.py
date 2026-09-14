import asyncio
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from alerts import send_trade_update
from main import _persist_ml_snapshot_before_exit_checks
from ml_signal.collector import MLCollector
from ml_signal.config import MLConfig
from ml_signal.signal_consumer import SignalConsumer, _display_id_for_alert
from models import AresSignal, Direction, SetupType


def _signal() -> AresSignal:
    return AresSignal(
        setup_type=SetupType.OI_WALL_REJECTION,
        direction=Direction.BULLISH,
        trigger_price=24000.0,
        entry_zone=(23990.0, 24010.0),
        stop_loss=23975.0,
        target_1=24050.0,
        target_2=24100.0,
        confidence="HIGH",
        reasons=["review regression"],
        timestamp=datetime.now(),
        strike_to_trade=24000,
        option_type="CE",
    )


def _atm():
    option = SimpleNamespace(
        oi=1, oi_change_pct=1.0, iv=1.0, gamma=0.1,
        theta=-0.1, delta=0.5, vega=0.1,
    )
    return SimpleNamespace(ce=option, pe=option)


def _collector() -> MLCollector:
    collector = MLCollector.__new__(MLCollector)
    collector.config = MLConfig()
    collector.supabase = MagicMock()
    collector.volume_history = __import__("collections").deque(maxlen=20)
    collector.iv_history = __import__("collections").deque(maxlen=20)
    collector._total_snapshots = 0
    collector._signals_recorded = 0
    return collector


class TestSnapshotRetryIdentity(unittest.IsolatedAsyncioTestCase):
    def test_schema_and_bridge_migration_install_snapshot_identity(self):
        schema = Path("ml_signal/schema.sql").read_text()
        migration = Path(
            "migrations/2026-09-12-task150-canonical-signal-uuid.sql"
        ).read_text()

        self.assertIn("snapshot_uuid uuid UNIQUE", schema)
        self.assertIn("ADD COLUMN IF NOT EXISTS snapshot_uuid uuid", migration)
        self.assertIn("idx_ml_collection_snapshot_uuid", migration)

    async def test_ambiguous_retry_upserts_one_stable_snapshot_identity(self):
        collector = _collector()
        signal = _signal()
        trade_id = "7bf33f92-27a0-47f2-b3bd-50dd291ea23b"
        table = collector.supabase.table.return_value

        def execute():
            record = table.upsert.call_args.args[0]
            if table.upsert.call_count == 1:
                raise ConnectionError("response lost after commit")
            return SimpleNamespace(data=[record])

        table.upsert.return_value.execute.side_effect = execute

        with patch("ml_signal.collector.asyncio.sleep", new=AsyncMock()):
            await collector.snapshot(
                candle=SimpleNamespace(
                    open=1, high=2, low=0.5, close=1.5,
                    volume=100, vwap=1.2, timestamp=datetime.now(),
                ),
                atm=_atm(), full_chain=[], levels=[], spot=24000.0,
                signal=signal, trade_id=trade_id,
                trade_binding_status="BOUND",
            )

        self.assertEqual(table.upsert.call_count, 2)
        first, second = [call.args[0] for call in table.upsert.call_args_list]
        self.assertEqual(first["snapshot_uuid"], second["snapshot_uuid"])
        self.assertEqual(
            table.upsert.call_args.kwargs["on_conflict"], "snapshot_uuid"
        )


class TestExitChecksSurviveSnapshotFailure(unittest.IsolatedAsyncioTestCase):
    async def test_successful_snapshot_reports_persisted(self):
        collector = MagicMock()
        collector.snapshot = AsyncMock(return_value=None)

        persisted = await _persist_ml_snapshot_before_exit_checks(
            collector, marker="value"
        )

        self.assertTrue(persisted)

    async def test_snapshot_failure_is_recorded_without_propagating(self):
        collector = MagicMock()
        collector.snapshot = AsyncMock(side_effect=RuntimeError("db unavailable"))

        with patch("builtins.print") as print_mock:
            persisted = await _persist_ml_snapshot_before_exit_checks(
                collector, marker="value"
            )

        self.assertFalse(persisted)
        print_mock.assert_called_once()
        self.assertIn("db unavailable", print_mock.call_args.args[0])


class TestPresentationIdentity(unittest.IsolatedAsyncioTestCase):
    @patch("alerts.settings")
    @patch("alerts.httpx.AsyncClient")
    async def test_trade_update_falls_back_when_display_id_is_null(
        self, client_class, mock_settings
    ):
        mock_settings.discord_webhook_url = "https://example.test/webhook"
        client = AsyncMock()
        client.post.return_value = MagicMock()
        client_class.return_value.__aenter__.return_value = client
        trade = {
            "signal_id": "4829", "display_id": None,
            "setup_type": "OI_WALL_REJECTION", "direction": "BULLISH",
            "entry_price": 24000.0, "stop_loss": 23975.0, "state": "OPEN",
        }

        await send_trade_update(trade, 24010.0, "T1_HIT")

        title = client.post.call_args.kwargs["json"]["embeds"][0]["title"]
        self.assertIn("#4829 TRADE UPDATE", title)
        self.assertNotIn("#None", title)

    def test_event_alert_prefers_display_id_but_falls_back_to_primary_key(self):
        signal = {"id": "canonical-uuid", "display_id": "4829"}
        self.assertEqual(_display_id_for_alert(signal), "4829")
        self.assertEqual(
            _display_id_for_alert({"id": "legacy-id", "display_id": None}),
            "legacy-id",
        )

    @patch("ml_signal.signal_consumer.send_prediction_alert", new_callable=AsyncMock)
    async def test_event_consumer_persists_uuid_but_alerts_with_display_id(
        self, send_alert
    ):
        consumer = SignalConsumer()
        consumer._init_supabase = MagicMock()
        consumer.predictor.load_model = MagicMock()
        consumer.predictor.predict_from_raw = MagicMock(return_value={
            "probability": 0.75,
            "confidence_tier": "HIGH",
        })
        consumer.fetch_new_signals = AsyncMock(return_value=[{
            "id": "canonical-uuid",
            "display_id": "4829",
            "setup_type": "OI_WALL_REJECTION",
            "market_context": {},
            "spot_at_signal": 24000.0,
            "timestamp": datetime.now(),
        }])
        consumer.log_prediction = AsyncMock()

        with patch(
            "ml_signal.signal_consumer.asyncio.sleep",
            new=AsyncMock(side_effect=asyncio.CancelledError),
        ), self.assertRaises(asyncio.CancelledError):
            await consumer.run("https://example.test", "key")

        prediction = consumer.log_prediction.call_args.args[0]
        self.assertEqual(prediction["signal_id"], "canonical-uuid")
        self.assertEqual(prediction["signal_display_id"], "4829")
        self.assertEqual(send_alert.call_args.kwargs["signal_id"], "4829")
