"""Review regressions exercise real order dispatch against an isolated database boundary."""
import asyncio
from copy import deepcopy
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from config import settings
from alerts import _option_lifecycle_estimate
from execution.order_manager import OrderManager
from execution.signal_dispatch import SignalDispatcher
from models import AresSignal, Direction, OptionGreeks, OptionOrderBracket, SetupType
from position_manager import PositionManager


class MemoryDatabase:
    def __init__(self):
        self.rows = {"active_trades": [], "trade_analytics": [], "ml_collection": []}

    def table(self, name):
        return Query(self.rows.setdefault(name, []))

    def rpc(self, name, payload):
        trade_id = payload["p_trade_id"]
        row = {"id": trade_id, "signal_uuid": payload["p_signal_uuid"],
               "state": "OPEN"}
        for field in ("setup_type", "direction", "entry_price", "stop_loss",
                      "target_1", "target_2", "entry_timestamp"):
            row[field] = payload[f"p_{field}"]
        for table in ("active_trades", "trade_analytics"):
            if not any(existing["id"] == trade_id for existing in self.rows[table]):
                self.rows[table].append(deepcopy(row))
                if table == "trade_analytics":
                    self.rows[table][-1]["market_context"] = deepcopy(payload["p_market_context"])
        return SimpleNamespace(execute=lambda: SimpleNamespace(data=trade_id))


class Query:
    def __init__(self, rows):
        self.rows, self.filters, self.values = rows, [], None
        self.fields = "*"

    def select(self, fields):
        self.fields = fields
        return self

    def eq(self, field, value):
        self.filters.append(lambda row: row.get(field) == value)
        return self

    def is_(self, field, value):
        assert value == "null"
        self.filters.append(lambda row: row.get(field) is None)
        return self

    def in_(self, field, values):
        self.filters.append(lambda row: row.get(field) in values)
        return self

    def update(self, values):
        self.values = values
        return self

    def execute(self):
        selected = [row for row in self.rows if all(test(row) for test in self.filters)]
        if self.values is not None:
            for row in selected:
                row.update(deepcopy(self.values))
        if self.fields != "*":
            selected = [{key: row[key] for key in self.fields.split(",") if key in row}
                        for row in selected]
        return SimpleNamespace(data=deepcopy(selected))


def make_signal():
    signal = AresSignal(
        setup_type=SetupType.EXHAUSTION_REVERSAL, direction=Direction.BULLISH,
        trigger_price=24000, entry_zone=(23995,24005), stop_loss=23990,
        target_1=24020, target_2=24040, confidence="HIGH", reasons=[],
        timestamp=datetime(2026,10,16,10), strike_to_trade=24000, option_type="CE",
    )
    signal.bracket = OptionOrderBracket(
        security_id="123", expiry_date="2099-01-01", strike=24000, option_type="CE",
        entry_limit=100, synthetic_spot_sl=23990, catastrophic_opt_sl=95,
        target_1=110, target_2=120, lots=2, greeks=OptionGreeks(.5,.001,-10,2,15),
        is_valid=True, signal_id=str(signal.id), spot_entry=24000,
        spot_target_1=24020, spot_target_2=24040,
    )
    return signal


@pytest.mark.asyncio
@pytest.mark.parametrize("mode,column",[("bridge","signal_uuid"),("greenfield","signal_id")])
async def test_same_quote_roundtrip_persists_loss_and_waits_for_later_ml_snapshot(mode,column):
    database = MemoryDatabase()
    with patch("position_manager.create_client",return_value=database), patch("position_manager.AnalyticsLogger"), patch.object(settings,"signal_schema_mode",mode):
        positions = PositionManager()
        orders = OrderManager()
        dispatcher = SignalDispatcher(orders,positions)
        signal = make_signal()
        chain = [{"strike":24000,"expiry_date":"2099-01-01","ce_security_id":"123",
                  "ce_ask":99,"ce_bid":94,"underlying_spot":24002}]
        assert await dispatcher.dispatch(signal,24000,chain)
        saved = database.table("trade_analytics").select("*").eq("id",signal.trade_id).execute().data[0]
        execution = saved["market_context"]["execution"]
        assert execution["status"] == "CLOSED"
        assert execution["filled_quantity"] == execution["exited_quantity"] == 130
        assert execution["realized_pnl"] == -650
        assert execution["spot_at_fill"] == 24002
        # Main writes the inference snapshot after initial dispatch. The closed
        # record must survive until that row can be linked to its actual trade.
        database.rows["ml_collection"].append({column:str(signal.id),"trade_id":None})
        await dispatcher.on_spot(24001)
        linked = database.table("ml_collection").select("*").eq(column,str(signal.id)).execute().data[0]
        assert linked["trade_id"] == signal.trade_id
        assert linked["trade_binding_status"] == "BOUND"
        await dispatcher.on_snapshot(chain,24000)
        assert len(database.table("trade_analytics").select("*").execute().data) == 1


@pytest.mark.asyncio
async def test_sync_reports_pending_until_snapshot_is_durable():
    database = MemoryDatabase()
    candidate = make_signal()
    candidate.trade_id = "t1"
    database.rows["trade_analytics"].append({"id":"t1","market_context":{"audit":"keep"}})
    with patch("position_manager.create_client",return_value=database), patch("position_manager.AnalyticsLogger"), patch.object(settings,"signal_schema_mode","bridge"):
        positions = PositionManager()
        execution = {"mode":"PAPER","status":"CLOSED","realized_pnl":-650}
        assert await positions.sync_execution(candidate,execution) is False
        database.rows["ml_collection"].append({"signal_uuid":str(candidate.id),"trade_id":None})
        assert await positions.sync_execution(candidate,execution) is True
        assert await positions.sync_execution(candidate,execution) is True
    saved = database.table("trade_analytics").select("*").eq("id","t1").execute().data[0]
    assert saved["market_context"]["audit"] == "keep"


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["bridge", "greenfield"])
async def test_carried_fill_rehydrates_latest_context_and_lifecycle_estimate(mode):
    database = MemoryDatabase()
    signal = make_signal()
    signal.market_context = {
        "options_sizing": {"entry_limit": 100, "target_1": 110, "target_2": 120,
                           "lots": 2, "lot_size": 65},
        "execution": {"mode": "PAPER", "status": "FILLED", "filled_quantity": 130},
    }
    with patch("position_manager.create_client", return_value=database), patch("position_manager.AnalyticsLogger"), patch.object(settings, "signal_schema_mode", mode):
        original = PositionManager()
        trade_id, _ = await original.add_trade(signal, 24000)
        latest_execution = {**signal.market_context["execution"], "status": "PARTIALLY_FILLED",
                            "exited_quantity": 65, "realized_pnl": 650}
        await original.sync_execution(signal, latest_execution)
        assert "market_context" not in database.rows["active_trades"][0]
        restored = PositionManager()
        context = restored.active_trades[0]["market_context"]
        assert context["options_sizing"] == signal.market_context["options_sizing"]
        assert context["execution"] == latest_execution
        with patch("position_manager.send_trade_update", new_callable=AsyncMock) as alert:
            await restored.update_trades(24020)
        alert.assert_awaited_once()
        trade, spot, event = alert.call_args.args
        assert trade["id"] == trade_id
        assert trade["market_context"] == context
        estimate = _option_lifecycle_estimate(trade, spot, event)
        assert "Estimated Option Exit" in estimate
        assert "110.00" in estimate
        await asyncio.gather(*restored._trade_writes.values())


@pytest.mark.parametrize("legacy_context", [None, [], "invalid", {}])
def test_startup_merges_matching_analytics_context_and_preserves_legacy(legacy_context):
    database = MemoryDatabase()
    database.rows["active_trades"] = [
        {"id": "first", "state": "OPEN", "market_context": {
            "active_only": "keep", "execution": {"status": "FILLED"}}},
        {"id": "second", "state": "T1_HIT", "market_context": legacy_context},
        {"id": "legacy", "state": "OPEN", "market_context": {"active_only": "legacy"}},
        {"id": "unmatched", "state": "OPEN"},
        {"id": "terminal", "state": "OPEN"},
    ]
    database.rows["trade_analytics"] = [
        {"id": "first", "market_context": {"execution": {"status": "CLOSED"}}},
        {"id": "second", "market_context": {"options_sizing": {"entry_limit": 200}}},
        {"id": "legacy", "market_context": legacy_context},
        {"id": "unrelated", "market_context": {"execution": {"status": "REJECTED"}}},
        {"id": "terminal", "result_state": "T2_HIT", "exit_price": 24040,
         "market_context": {"execution": {"status": "CLOSED"}}},
    ]
    with patch("position_manager.create_client", return_value=database), patch("position_manager.AnalyticsLogger"):
        restored = PositionManager()
    trades = {row["id"]: row for row in restored.active_trades}
    assert set(trades) == {"first", "second", "legacy", "unmatched"}
    assert trades["first"]["market_context"] == {
        "active_only": "keep", "execution": {"status": "CLOSED"}}
    assert trades["second"]["market_context"] == {"options_sizing": {"entry_limit": 200}}
    assert trades["legacy"]["market_context"] == {"active_only": "legacy"}
    assert "market_context" not in trades["unmatched"]
    assert database.rows["active_trades"][-1]["state"] == "CLOSED"


def test_analytics_outage_preserves_active_trade_monitoring():
    database = MemoryDatabase()
    database.rows["active_trades"] = [{"id": "carried", "state": "OPEN",
                                      "entry_price": 24000}]
    original_table = database.table
    def table(name):
        if name == "trade_analytics":
            raise RuntimeError("analytics unavailable")
        return original_table(name)
    database.table = table
    with patch("position_manager.create_client", return_value=database), patch("position_manager.AnalyticsLogger"):
        restored = PositionManager()
    assert restored.is_initialized
    assert restored.active_trades == database.rows["active_trades"]
