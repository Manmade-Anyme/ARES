"""Review regressions exercise real order dispatch against an isolated database boundary."""
from copy import deepcopy
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from config import settings
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
               "state": "OPEN", "market_context": deepcopy(payload["p_market_context"])}
        for table in ("active_trades", "trade_analytics"):
            if not any(existing["id"] == trade_id for existing in self.rows[table]):
                self.rows[table].append(deepcopy(row))
        return SimpleNamespace(execute=lambda: SimpleNamespace(data=trade_id))


class Query:
    def __init__(self, rows):
        self.rows, self.filters, self.values = rows, [], None

    def select(self, fields):
        return self

    def eq(self, field, value):
        self.filters.append(lambda row: row.get(field) == value)
        return self

    def is_(self, field, value):
        assert value == "null"
        self.filters.append(lambda row: row.get(field) is None)
        return self

    def update(self, values):
        self.values = values
        return self

    def execute(self):
        selected = [row for row in self.rows if all(test(row) for test in self.filters)]
        if self.values is not None:
            for row in selected:
                row.update(deepcopy(self.values))
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
