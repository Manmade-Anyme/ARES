import asyncio
import time
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from ml_signal.collector import MLCollector
from ml_signal.config import MLConfig
from models import AresSignal, Direction, SetupType
from position_manager import PositionManager
from storage import Storage


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
        reasons=["Reason 1"],
        timestamp=datetime.now(),
        strike_to_trade=24000,
        option_type="CE",
    )


def _atm():
    ce = SimpleNamespace(oi=1_000_000, oi_change_pct=5.25, iv=15.0,
                         gamma=0.05, theta=-0.8, delta=0.5, vega=0.3)
    pe = SimpleNamespace(oi=500_000, oi_change_pct=10.5, iv=16.0,
                         gamma=0.04, theta=-0.7, delta=-0.5, vega=0.25)
    return SimpleNamespace(spot_price=24000.0, ce=ce, pe=pe)


class _RpcClient:
    def __init__(self):
        self.calls = []

    def rpc(self, name, payload):
        self.calls.append((name, payload))
        return SimpleNamespace(
            execute=lambda: SimpleNamespace(data=payload["p_trade_id"])
        )


class _FlakyRpcClient(_RpcClient):
    def rpc(self, name, payload):
        self.calls.append((name, dict(payload)))

        def execute():
            if len(self.calls) == 1:
                raise ConnectionError("response lost after commit")
            return SimpleNamespace(data=payload["p_trade_id"])

        return SimpleNamespace(execute=execute)


class _MemoryTable:
    def __init__(self, database, name):
        self.database = database
        self.name = name
        self.payload = None

    def insert(self, payload):
        self.payload = dict(payload)
        return self

    def upsert(self, payload, on_conflict=None):
        self.payload = dict(payload)
        return self

    def execute(self):
        row = dict(self.payload)
        if self.name == "ares_signals":
            row["id"] = 321
        self.database.setdefault(self.name, []).append(row)
        return SimpleNamespace(data=[row])


class _MemoryClient:
    def __init__(self):
        self.database = {}

    def table(self, name):
        return _MemoryTable(self.database, name)

    def rpc(self, name, payload):
        def execute():
            active = {
                "id": payload["p_trade_id"],
                "signal_id": str(payload.get("p_legacy_signal_id")),
                "signal_uuid": payload["p_signal_uuid"],
                "display_id": payload["p_display_id"],
            }
            analytics = {
                "id": payload["p_trade_id"],
                "signal_id": payload.get("p_legacy_signal_id"),
                "signal_uuid": payload["p_signal_uuid"],
            }
            self.database.setdefault("active_trades", []).append(active)
            self.database.setdefault("trade_analytics", []).append(analytics)
            return SimpleNamespace(data=payload["p_trade_id"])

        return SimpleNamespace(execute=execute)


class TestSignalPersistence(unittest.IsolatedAsyncioTestCase):
    async def test_bridge_signal_write_returns_verified_success_and_uuid(self):
        signal = _signal()
        response = SimpleNamespace(data=[{
            "id": 321,
            "signal_uuid": signal.id,
            "display_id": signal.display_id,
        }])
        table = MagicMock()
        table.insert.return_value.execute.return_value = response
        storage = Storage.__new__(Storage)
        storage.supabase = MagicMock()
        storage.supabase.table.return_value = table

        with patch("storage.settings.signal_schema_mode", "bridge", create=True):
            self.assertTrue(await storage.log_signal(signal, 24001.0))

        payload = table.insert.call_args.args[0]
        self.assertEqual(payload["signal_uuid"], signal.id)
        self.assertEqual(payload["display_id"], signal.display_id)
        self.assertEqual(signal.db_id, 321)

    async def test_greenfield_signal_write_uses_uuid_primary_key(self):
        signal = _signal()
        table = MagicMock()
        table.insert.return_value.execute.return_value = SimpleNamespace(
            data=[{"id": signal.id, "display_id": signal.display_id}]
        )
        storage = Storage.__new__(Storage)
        storage.supabase = MagicMock()
        storage.supabase.table.return_value = table

        with patch("storage.settings.signal_schema_mode", "greenfield", create=True):
            self.assertTrue(await storage.log_signal(signal, 24001.0))

        payload = table.insert.call_args.args[0]
        self.assertEqual(payload["id"], signal.id)
        self.assertNotIn("signal_uuid", payload)


class TestAtomicTradeEntry(unittest.IsolatedAsyncioTestCase):
    async def test_bridge_rpc_preserves_atm_oi_context(self):
        signal = _signal()
        signal.db_id = 321
        pm = PositionManager.__new__(PositionManager)
        pm.active_trades = []
        pm.supabase = _RpcClient()

        with patch("position_manager.settings.signal_schema_mode", "bridge", create=True):
            trade_id, status = await pm.add_trade(signal, 24001.0, _atm())

        name, payload = pm.supabase.calls[0]
        self.assertEqual(name, "create_trade_entry_bridge")
        self.assertEqual(payload["p_trade_id"], trade_id)
        self.assertEqual(status, "BOUND")
        self.assertEqual(payload["p_oi_data"], {
            "pcr": 0.5,
            "atm_ce_oi": 1_000_000,
            "atm_pe_oi": 500_000,
            "ce_oi_change_pct": 5.25,
            "pe_oi_change_pct": 10.5,
        })

    async def test_greenfield_mode_selects_cutover_rpc(self):
        signal = _signal()
        pm = PositionManager.__new__(PositionManager)
        pm.active_trades = []
        pm.supabase = _RpcClient()

        with patch("position_manager.settings.signal_schema_mode", "greenfield", create=True):
            await pm.add_trade(signal, 24001.0)

        name, payload = pm.supabase.calls[0]
        self.assertEqual(name, "create_trade_entry_greenfield")
        self.assertNotIn("p_legacy_signal_id", payload)

    async def test_reentry_uses_new_trade_id_but_rpc_retry_reuses_one_id(self):
        signal = _signal()
        signal.db_id = 321
        pm = PositionManager.__new__(PositionManager)
        pm.active_trades = []
        client = _RpcClient()
        pm.supabase = client

        with patch("position_manager.settings.signal_schema_mode", "bridge", create=True):
            first_id, _ = await pm.add_trade(signal, 24001.0)
            pm.active_trades[0]["state"] = "CLOSED"
            second_id, _ = await pm.add_trade(signal, 24001.0)

        self.assertNotEqual(first_id, second_id)

    async def test_ambiguous_commit_retry_reuses_trade_id(self):
        signal = _signal()
        signal.db_id = 321
        pm = PositionManager.__new__(PositionManager)
        pm.active_trades = []
        pm.supabase = _FlakyRpcClient()

        with patch("position_manager.settings.signal_schema_mode", "bridge", create=True), \
                patch("position_manager.asyncio.sleep", return_value=None):
            trade_id, status = await pm.add_trade(signal, 24001.0)

        self.assertEqual(status, "BOUND")
        self.assertEqual(len(pm.supabase.calls), 2)
        self.assertEqual(
            {payload["p_trade_id"] for _, payload in pm.supabase.calls},
            {trade_id},
        )


class TestMLSnapshotOrdering(unittest.IsolatedAsyncioTestCase):
    def _collector(self):
        collector = MLCollector.__new__(MLCollector)
        collector.config = MLConfig()
        collector.supabase = MagicMock()
        collector.volume_history = __import__("collections").deque(maxlen=60)
        collector.iv_history = __import__("collections").deque(maxlen=60)
        collector._total_snapshots = 0
        collector._signals_recorded = 0
        return collector

    async def test_signal_snapshot_persists_canonical_binding_fields(self):
        collector = self._collector()
        captured = []
        collector._upsert_signal_snapshot = (
            lambda record: captured.append(record) or [record]
        )
        signal = _signal()

        await collector.snapshot(
            candle=SimpleNamespace(open=1, high=2, low=0.5, close=1.5,
                                   volume=100, vwap=1.2, timestamp=datetime.now()),
            atm=_atm(), full_chain=[], levels=[], spot=24000.0, signal=signal,
            trade_id="7bf33f92-27a0-47f2-b3bd-50dd291ea23b",
            trade_binding_status="BOUND",
        )

        self.assertEqual(captured[0]["signal_uuid"], signal.id)
        self.assertEqual(captured[0]["signal_display_id"], signal.display_id)
        self.assertEqual(captured[0]["trade_id"], "7bf33f92-27a0-47f2-b3bd-50dd291ea23b")
        self.assertEqual(captured[0]["trade_binding_status"], "BOUND")

    async def test_greenfield_snapshot_uses_canonical_signal_id(self):
        collector = self._collector()
        captured = []
        collector._upsert_signal_snapshot = (
            lambda record: captured.append(record) or [record]
        )
        signal = _signal()

        with patch("ml_signal.collector.settings.signal_schema_mode", "greenfield", create=True):
            await collector.snapshot(
                candle=SimpleNamespace(open=1, high=2, low=0.5, close=1.5,
                                       volume=100, vwap=1.2, timestamp=datetime.now()),
                atm=_atm(), full_chain=[], levels=[], spot=24000.0, signal=signal,
                trade_id="7bf33f92-27a0-47f2-b3bd-50dd291ea23b",
                trade_binding_status="BOUND",
            )

        self.assertEqual(captured[0]["signal_id"], signal.id)
        self.assertNotIn("signal_uuid", captured[0])

    async def test_ordinary_snapshot_does_not_wait_for_remote_insert(self):
        collector = self._collector()
        collector._insert = lambda record: time.sleep(0.25)

        started = time.monotonic()
        await collector.snapshot(
            candle=SimpleNamespace(open=1, high=2, low=0.5, close=1.5,
                                   volume=100, vwap=1.2, timestamp=datetime.now()),
            atm=_atm(), full_chain=[], levels=[], spot=24000.0,
        )
        elapsed = time.monotonic() - started

        self.assertLess(elapsed, 0.15)
        await asyncio.sleep(0.3)


class TestCutoverMigration(unittest.TestCase):
    def test_cutover_installs_greenfield_rpc_with_conflict_validation(self):
        sql = Path("migrations/2026-09-12-task150-cutover-signal-uuid.sql").read_text()
        self.assertIn("create_trade_entry_greenfield", sql)
        self.assertIn("conflicting trade entry", sql.lower())


class TestSignalJoinPipeline(unittest.IsolatedAsyncioTestCase):
    async def test_bridge_pipeline_uses_one_canonical_signal_and_trade_key(self):
        client = _MemoryClient()
        signal = _signal()

        storage = Storage.__new__(Storage)
        storage.supabase = client
        pm = PositionManager.__new__(PositionManager)
        pm.supabase = client
        pm.active_trades = []
        collector = TestMLSnapshotOrdering()._collector()
        collector.supabase = client

        with patch("storage.settings.signal_schema_mode", "bridge", create=True), \
                patch("position_manager.settings.signal_schema_mode", "bridge", create=True), \
                patch("ml_signal.collector.settings.signal_schema_mode", "bridge", create=True):
            self.assertTrue(await storage.log_signal(signal, 24001.0))
            trade_id, status = await pm.add_trade(signal, 24001.0, _atm())
            await collector.snapshot(
                candle=SimpleNamespace(open=1, high=2, low=0.5, close=1.5,
                                       volume=100, vwap=1.2, timestamp=datetime.now()),
                atm=_atm(), full_chain=[], levels=[], spot=24001.0,
                signal=signal, trade_id=trade_id, trade_binding_status=status,
            )

        signal_row = client.database["ares_signals"][0]
        active_row = client.database["active_trades"][0]
        analytics_row = client.database["trade_analytics"][0]
        ml_row = client.database["ml_collection"][0]
        self.assertEqual(
            {signal_row["signal_uuid"], active_row["signal_uuid"],
             analytics_row["signal_uuid"], ml_row["signal_uuid"]},
            {signal.id},
        )
        self.assertEqual(
            {active_row["id"], analytics_row["id"], ml_row["trade_id"]},
            {trade_id},
        )
