"""Failed manual entries retain frozen identity and recover without duplicating trades."""
import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest

from config import settings
from position_manager import PositionManager
from tests.unit.test_manm176_review_persistence import MemoryDatabase, Query, make_signal


class RegistrationDatabase(MemoryDatabase):
    def __init__(self):
        super().__init__()
        self.blocked_signals = set()
        self.rpc_calls = []
        self.ml_unavailable = False
        self.ml_attempts = 0

    def rpc(self, name, payload):
        self.rpc_calls.append((name, deepcopy(payload)))
        if payload["p_signal_uuid"] in self.blocked_signals:
            raise RuntimeError("entry RPC unavailable")
        return super().rpc(name, payload)

    def table(self, name):
        if name == "ml_collection":
            self.ml_attempts += 1
            if self.ml_unavailable:
                raise RuntimeError("ML snapshot store unavailable")
        return Query(self.rows.setdefault(name, []))


@pytest.fixture
def registrations():
    database = RegistrationDatabase()
    with patch("position_manager.create_client", return_value=database), \
         patch("position_manager.AnalyticsLogger"), \
         patch("position_manager.send_trade_update", new_callable=AsyncMock), \
         patch("position_manager.asyncio.sleep", new_callable=AsyncMock):
        yield database, PositionManager()


def reference_signal():
    signal = make_signal()
    signal.market_context = {"tracking_mode":"SIGNAL_REFERENCE", "audit":{"source":["original"]}}
    return signal


async def fail_initial_registration(database, positions, signal, spot=24000):
    database.blocked_signals.add(str(signal.id))
    with pytest.raises(RuntimeError, match="after 3 attempts"):
        await positions.add_trade(signal, spot)
    assert len([payload for _, payload in database.rpc_calls if payload["p_signal_uuid"] == str(signal.id)]) == 3
    assert signal.trade_binding_status == "REGISTRATION_PENDING"
    assert not positions.active_trades
    return deepcopy(database.rpc_calls[-1])


@pytest.mark.asyncio
@pytest.mark.parametrize("mode,column", [("bridge", "signal_uuid"), ("greenfield", "signal_id")])
async def test_failed_manual_registration_recovers_frozen_payload_then_links_late_snapshot(registrations, mode, column):
    database, positions = registrations
    signal = reference_signal()
    with patch.object(settings, "signal_schema_mode", mode):
        original_name, original = await fail_initial_registration(database, positions, signal, 24002)
        assert original["p_added_time_ist"]
        assert original["p_entry_timestamp"]
        # Later market/config/clock and mutable signal fields cannot reprice or
        # relabel the retained manual entry when its original RPC is retried.
        signal.timestamp += timedelta(days=1)
        signal.trigger_price = 24500
        signal.stop_loss, signal.target_1, signal.target_2 = 24400, 24600, 24700
        signal.reasons.append("later mutation")
        signal.market_context["audit"]["source"].append("later mutation")
        database.blocked_signals.clear()
        with patch.object(settings, "trade_dedupe_tolerance_pts", 500), \
             patch("position_manager.datetime", wraps=datetime) as clock:
            clock.now.return_value = datetime(2099, 1, 1, 12, tzinfo=timezone.utc)
            await positions.retry_reference_registrations()
        assert database.rpc_calls[-1] == (original_name, original)
        assert len(database.rpc_calls) == 4
        assert signal.trade_binding_status == "BOUND"
        assert signal.trade_id == original["p_trade_id"]
        assert len(positions.active_trades) == 1
        active = positions.active_trades[0]
        assert active["entry_price"] == 24002
        assert active["entry_timestamp"] == original["p_entry_timestamp"]
        assert active["stop_loss"] == original["p_stop_loss"]
        assert active["market_context"] == original["p_market_context"]
        assert "execution" not in active["market_context"]
        await positions.retry_reference_registrations()  # No snapshot yet: no second RPC.
        assert len(database.rpc_calls) == 4
        database.rows["ml_collection"].extend([
            {column:"unrelated", "trade_id":None},
            {column:str(signal.id), "trade_id":None, "trade_binding_status":"REGISTRATION_PENDING"},
        ])
        database.ml_unavailable = True
        await positions.retry_reference_registrations()  # Snapshot outage remains retryable.
        assert database.rows["ml_collection"][1]["trade_id"] is None
        database.ml_unavailable = False
        await positions.retry_reference_registrations()
        assert database.rows["ml_collection"][1]["trade_id"] == signal.trade_id
        assert database.rows["ml_collection"][1]["trade_binding_status"] == "BOUND"
        assert database.rows["ml_collection"][0]["trade_id"] is None
        attempts_after_success = database.ml_attempts
        await positions.retry_reference_registrations()
        assert database.ml_attempts == attempts_after_success
        assert len(database.rpc_calls) == 4
        assert len(database.rows["trade_analytics"]) == 1
        assert database.rows["trade_analytics"][0]["market_context"] == original["p_market_context"]


@pytest.mark.asyncio
async def test_same_canonical_direct_retry_reuses_original_payload(registrations):
    database, positions = registrations
    original_signal = reference_signal()
    with patch.object(settings, "signal_schema_mode", "bridge"):
        original_rpc = await fail_initial_registration(database, positions, original_signal, 24001)
        retry_signal = deepcopy(original_signal)
        retry_signal.timestamp += timedelta(days=1)
        retry_signal.target_1 = 25000
        retry_signal.market_context["audit"] = {"replacement":"ignore"}
        database.blocked_signals.clear()
        trade_id, status = await positions.add_trade(retry_signal, 24900)
        assert status == "BOUND"
        assert database.rpc_calls[-1] == original_rpc
        assert retry_signal.trade_id == original_signal.trade_id == trade_id
        assert positions.active_trades[0]["entry_price"] == 24001
        assert len(positions.active_trades) == 1
        await positions.retry_reference_registrations()
        assert len(database.rpc_calls) == 4


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal", [False, True])
async def test_delayed_snapshot_binding_never_reopens_t1_or_closed_trade(registrations, terminal):
    database, positions = registrations
    signal = reference_signal()
    with patch.object(settings, "signal_schema_mode", "bridge"):
        await fail_initial_registration(database, positions, signal)
        database.blocked_signals.clear()
        await positions.retry_reference_registrations()
        await positions.update_trades(24020, candle_timestamp=datetime(2026, 10, 16, 10, 1))
        if terminal:
            start = datetime(2099, 1, 1, 9, 15, tzinfo=timezone(timedelta(hours=5, minutes=30)))
            candles = [{"timestamp":(start + timedelta(minutes=i)).isoformat(), "complete":True,
                        "open":24000, "high":24005, "low":23995, "close":23999}
                       for i in range(5)]
            await positions.update_atr_trailing(candles)
        tasks = tuple(positions._trade_writes.values())
        if tasks:
            await asyncio.gather(*tasks)
        expected_state = "CLOSED" if terminal else "T1_HIT"
        assert positions.active_trades[0]["state"] == expected_state
        database.rows["ml_collection"].append({"signal_uuid":str(signal.id), "trade_id":None})
        await positions.retry_reference_registrations()
        assert positions.active_trades[0]["state"] == expected_state
        assert len(positions.active_trades) == 1
        assert len(database.rows["active_trades"]) == 1
        assert database.rows["active_trades"][0]["state"] == expected_state
        assert database.rows["ml_collection"][0]["trade_id"] == signal.trade_id
        assert "execution" not in database.rows["trade_analytics"][0]["market_context"]
        assert len(database.rpc_calls) == 4


@pytest.mark.asyncio
async def test_one_failed_queued_entry_does_not_block_other_recovery(registrations):
    database, positions = registrations
    first, second = reference_signal(), reference_signal()
    with patch.object(settings, "signal_schema_mode", "bridge"):
        await fail_initial_registration(database, positions, first)
        await fail_initial_registration(database, positions, second)
        database.blocked_signals.remove(str(second.id))
        database.rows["ml_collection"].append({"signal_uuid":str(second.id), "trade_id":None})
        await positions.retry_reference_registrations()
        assert first.trade_binding_status == "REGISTRATION_PENDING"
        assert second.trade_binding_status == "BOUND"
        assert len(positions.active_trades) == 1
        assert positions.active_trades[0]["signal_uuid"] == str(second.id)
        assert len(database.rpc_calls) == 8  # Three initial attempts each, one per queued item.
        assert database.rows["ml_collection"][0]["trade_id"] == second.trade_id


@pytest.mark.asyncio
async def test_successful_initial_entry_is_not_queued_for_later_registration(registrations):
    database, positions = registrations
    signal = reference_signal()
    with patch.object(settings, "signal_schema_mode", "bridge"):
        trade_id, status = await positions.add_trade(signal, 24000)
        assert (trade_id, status) == (signal.trade_id, "BOUND")
        assert len(database.rpc_calls) == 1
        await positions.retry_reference_registrations()
        assert database.ml_attempts == 0
        assert len(database.rpc_calls) == 1
        assert len(positions.active_trades) == 1
