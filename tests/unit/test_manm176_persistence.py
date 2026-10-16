"""MANM-176 public persistence regressions using an isolated database boundary."""
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from models import AresSignal, Direction, SetupType
from position_manager import PositionManager
from storage import Storage


def signal():
    return AresSignal(
        setup_type=SetupType.EXHAUSTION_REVERSAL, direction=Direction.BULLISH,
        trigger_price=24000, entry_zone=(23995, 24005), stop_loss=23990,
        target_1=24018, target_2=24040, confidence="HIGH", reasons=[],
        timestamp=datetime(2026, 10, 15, 10), strike_to_trade=24000, option_type="CE",
    )


@pytest.mark.asyncio
async def test_rejected_option_data_does_not_prevent_signal_audit():
    candidate = signal()
    candidate.suggested_lots = 0
    candidate.capital = 0
    candidate.risk_pct = 4
    candidate.options_sizing_status = "NO_AVAILABLE_CAPITAL"
    client = MagicMock()
    client.table.return_value.insert.side_effect = lambda row: SimpleNamespace(
        execute=lambda: SimpleNamespace(data=[dict(row, id=176)])
    )
    with patch("storage.create_client", return_value=client):
        assert await Storage().log_signal(candidate, 24000)
    saved = client.table.return_value.insert.call_args.args[0]
    assert any("NO_AVAILABLE_CAPITAL" in reason for reason in saved["reasons"])


@pytest.mark.asyncio
async def test_position_persists_rich_sizing_without_overwriting_it():
    candidate = signal()
    candidate.suggested_lots = 2
    candidate.capital = 100000
    candidate.risk_pct = 4
    candidate.option_sl = 95
    candidate.option_target = 110
    candidate.option_premium = 100
    candidate.option_delta = .5
    candidate.market_context["options_sizing"] = {
        "final_lots": 2, "security_id": "123", "greeks": {"gamma": .002},
        "entry_limit": 100.5, "rejection_reason": None,
    }
    client = MagicMock()
    client.table.return_value.select.return_value.execute.return_value.data = []
    payloads = []
    def rpc(name, payload):
        payloads.append(payload)
        return SimpleNamespace(execute=lambda: SimpleNamespace(data=payload["p_trade_id"]))
    client.rpc.side_effect = rpc
    with patch("position_manager.create_client", return_value=client), patch("position_manager.AnalyticsLogger"):
        pm = PositionManager()
        trade_id, status = await pm.add_trade(candidate, 24000)
    assert status == "BOUND"
    sizing = payloads[0]["p_market_context"]["options_sizing"]
    assert sizing["security_id"] == "123"
    assert sizing["final_lots"] == 2
    assert pm.active_trades[0]["market_context"]["options_sizing"] == sizing

@pytest.mark.asyncio
@pytest.mark.parametrize("mode,column", [("bridge","signal_uuid"),("greenfield","signal_id")])
async def test_execution_sync_preserves_context_and_binds_delayed_snapshot(mode,column):
    from config import settings
    candidate = signal()
    candidate.trade_id = "trade-176"
    client = MagicMock()
    query = MagicMock()
    client.table.return_value = query
    query.select.return_value = query
    query.eq.return_value = query
    query.is_.return_value = query
    query.update.return_value = query
    query.execute.return_value.data = [{"market_context":{"options_sizing":{"final_lots":2},"audit":"keep"}}]
    with patch("position_manager.create_client",return_value=client), patch("position_manager.AnalyticsLogger"), patch.object(settings,"signal_schema_mode",mode):
        pm = PositionManager()
        await pm.sync_execution(candidate,{"mode":"PAPER","status":"CLOSED","realized_pnl":321})
    updates = [call.args[0] for call in query.update.call_args_list]
    assert updates[0]["market_context"] == {
        "options_sizing":{"final_lots":2},"audit":"keep",
        "execution":{"mode":"PAPER","status":"CLOSED","realized_pnl":321},
    }
    assert updates[1] == {"trade_id":"trade-176","trade_binding_status":"BOUND"}
    assert any(call.args == (column,str(candidate.id)) for call in query.eq.call_args_list)
    query.is_.assert_called_with("trade_id","null")


@pytest.mark.asyncio
async def test_pre_fill_candle_wick_cannot_close_new_paper_trade():
    from unittest.mock import AsyncMock
    client = MagicMock()
    client.table.return_value.select.return_value.execute.return_value.data = []
    with patch("position_manager.create_client",return_value=client), patch("position_manager.AnalyticsLogger"), patch("position_manager.send_trade_update",new=AsyncMock()):
        pm = PositionManager()
        pm.active_trades = [{
            "id":"t1","direction":"BULLISH","state":"OPEN","entry_price":24000,
            "stop_loss":23990,"target_1":24018,"target_2":24040,
            "entry_timestamp":"2026-10-16T04:30:30+00:00",
            "market_context":{"execution":{"mode":"PAPER"}},
        }]
        events = await pm.update_trades(24001,candle_high=24050,candle_low=23980,
                                       candle_timestamp=datetime(2026,10,16,10,0))
    assert events == []
    assert pm.active_trades[0]["state"] == "OPEN"
