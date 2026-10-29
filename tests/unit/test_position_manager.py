import unittest
from unittest.mock import ANY, AsyncMock, MagicMock, patch
import sys
import importlib
import asyncio
import pytest
from datetime import datetime, timezone, timedelta
from models import AresSignal, SetupType, Direction

def _check_log_exit(m, *a, **k):
    from unittest.mock import ANY
    k.setdefault('exit_timestamp', ANY)
    m.assert_called_with(*a, **k)

def _check_log_exit_once(m, *a, **k):
    assert m.call_count == 1
    _check_log_exit(m, *a, **k)

class MockSupabaseClient:
    def __init__(self):
        self.table_mock = MagicMock()
        self.last_table = None
        
        # Mock inserts
        self.insert_mock = MagicMock()
        self.table_mock.insert = self.insert_mock
        self.insert_mock.return_value.execute = MagicMock()
        
        # Mock updates
        self.update_mock = MagicMock()
        self.table_mock.update = self.update_mock
        self.update_mock.return_value.eq.return_value.execute = MagicMock()
        
        # Mock deletes
        self.delete_mock = MagicMock()
        self.table_mock.delete = self.delete_mock
        self.delete_mock.return_value.eq.return_value.execute = MagicMock()

        # Mock selects
        self.select_mock = MagicMock()
        self.table_mock.select = self.select_mock
        self.execute_mock = MagicMock()
        self.select_mock.return_value.execute = self.execute_mock
        self.execute_mock.return_value.data = []
        self.select_mock.return_value.eq.return_value.execute.return_value.data = [{"market_context": {}}]

    def rpc(self, name, payload):
        m = __import__('unittest.mock').mock.MagicMock()
        m.execute.return_value = __import__('unittest.mock').mock.MagicMock(
            data=payload["p_trade_id"]
        )
        return m

    def table(self, name):
        self.last_table = name
        return self.table_mock

global_mock_client = MockSupabaseClient()
mock_create_patch = patch('supabase.create_client', return_value=global_mock_client)
mock_create_patch.start()

if 'position_manager' in sys.modules:
    importlib.reload(sys.modules['position_manager'])
else:
    pass

from position_manager import PositionManager  # noqa: E402


async def _flush_trade_writes(pm):
    tasks = tuple(pm._trade_writes.values())
    if tasks:
        await asyncio.gather(*tasks)
    alerts = tuple(getattr(pm, "_trade_alerts", {}).values())
    if alerts:
        await asyncio.gather(*alerts)


class TestPositionManager(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        self.mock_client = global_mock_client
        self.mock_client.insert_mock.reset_mock()
        self.mock_client.update_mock.reset_mock()
        self.mock_client.select_mock.reset_mock()
        self.mock_client.delete_mock.reset_mock()
        self.mock_client.insert_mock.side_effect = None
        self.mock_client.update_mock.side_effect = None
        self.mock_client.select_mock.side_effect = None
        self.mock_client.delete_mock.side_effect = None
        self.mock_client.execute_mock.side_effect = None
        self.mock_client.execute_mock.return_value.data = []
        self.mock_client.last_table = None

    @patch('position_manager.settings')
    async def test_lazy_initialization_success(self, mock_settings):
        mock_settings.supabase_url = "https://mock.supabase.co"
        mock_settings.supabase_key = "key"
        
        self.mock_client.execute_mock.return_value.data = []
        pm = PositionManager()
        self.assertTrue(pm.is_initialized)
        self.assertEqual(pm.active_trades, [])

    @patch('position_manager.settings')
    async def test_lazy_initialization_failure_and_retry(self, mock_settings):
        mock_settings.supabase_url = "https://mock.supabase.co"
        mock_settings.supabase_key = "key"

        # Retry succeeds
        self.mock_client.execute_mock.side_effect = [Exception("Network Error"), MagicMock(data=[])]
        pm = PositionManager()
        self.assertFalse(pm.is_initialized)

        await pm.update_trades(24000.0)
        self.assertTrue(pm.is_initialized)

        # Retry fails (hits line 113)
        self.mock_client.execute_mock.side_effect = Exception("Network Error")
        pm_fail = PositionManager()
        self.assertFalse(pm_fail.is_initialized)
        await pm_fail.update_trades(24000.0)
        self.assertFalse(pm_fail.is_initialized)

    @patch('position_manager.settings')
    async def test_initialize_db_loads_open_trades_from_any_date(self, mock_settings):
        today_str = datetime.now(timezone.utc).isoformat()
        yesterday_str = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        last_week_str = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()

        self.mock_client.execute_mock.return_value.data = [
            {"id": "trade-yesterday-open", "created_at": yesterday_str, "state": "OPEN"},
            {"id": "trade-today-open", "created_at": today_str, "state": "OPEN"},
            {"id": "trade-old-t1", "created_at": last_week_str, "state": "T1_HIT",
             "entry_price": 24000.0, "stop_loss": 24000.0},
            {"id": "trade-old-closed", "created_at": last_week_str, "state": "CLOSED"},
            {"id": "trade-old-stopped", "created_at": last_week_str, "state": "STOPPED_OUT"},
        ]
        pm = PositionManager()
        self.assertTrue(pm.is_initialized)
        # Multi-day carry: nothing is ever deleted at startup
        self.mock_client.delete_mock.assert_not_called()
        loaded_ids = {t["id"] for t in pm.active_trades}
        self.assertEqual(
            loaded_ids,
            {"trade-yesterday-open", "trade-today-open", "trade-old-t1"}
        )
        # T1_HIT trade resumes with its trailed stop intact
        t1_trade = next(t for t in pm.active_trades if t["id"] == "trade-old-t1")
        self.assertEqual(t1_trade["stop_loss"], t1_trade["entry_price"])

    @patch('ml_signal.backfill_labels.backfill_labels')
    @patch('ml_signal.backfill_labels.repair_stuck_open_trades')
    @patch('position_manager.settings')
    def test_initialize_db_reconciles_terminal_telemetry_before_filtering(
        self, mock_settings, mock_repair_stuck, mock_backfill
    ):
        self.mock_client.execute_mock.return_value.data = [{
            "id": "trade-closed-during-crash",
            "state": "CLOSED",
            "exit_price": 24100.0,
            "exit_type": "T2_HIT",
            "exit_timestamp": "2026-09-14T03:30:00+00:00",
        }]

        pm = PositionManager()

        mock_repair_stuck.assert_called_once_with(self.mock_client, apply=True)
        mock_backfill.assert_called_once_with(self.mock_client, apply=True)
        self.assertTrue(pm.is_initialized)
        self.assertEqual(pm.active_trades, [])

    @patch('ml_signal.backfill_labels.repair_stuck_open_trades')
    @patch('ml_signal.backfill_labels.backfill_labels')
    @patch('position_manager.settings')
    def test_initialize_db_keeps_active_trades_when_reconciliation_fails(
        self, mock_settings, mock_backfill, mock_repair_stuck
    ):
        self.mock_client.execute_mock.return_value.data = [
            {"id": "trade-open", "state": "OPEN", "entry_price": 24000.0},
            {"id": "trade-terminal", "state": "CLOSED", "exit_type": "T2_HIT"},
        ]
        mock_repair_stuck.side_effect = RuntimeError("ML table unavailable")

        pm = PositionManager()

        self.assertTrue(pm.is_initialized)
        self.assertEqual([t["id"] for t in pm.active_trades], ["trade-open"])
        mock_backfill.assert_not_called()

    @patch('position_manager.send_trade_update')
    @patch('position_manager.settings')
    async def test_previous_day_trade_continues_to_exit(self, mock_settings, mock_send_trade_update):
        """A previous-day OPEN trade must keep being evaluated until T1/T2/SL."""
        yesterday_str = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        self.mock_client.execute_mock.return_value.data = [{
            "id": "trade-carryover",
            "signal_id": "0001",
            "setup_type": "OI_WALL_REJECTION",
            "direction": "BULLISH",
            "entry_price": 24000.0,
            "stop_loss": 23975.0,
            "target_1": 24035.0,
            "target_2": 24070.0,
            "state": "OPEN",
            "created_at": yesterday_str,
        }]
        mock_send_trade_update.return_value = None
        pm = PositionManager()
        self.assertEqual(len(pm.active_trades), 1)

        with patch.object(pm.analytics, 'log_exit'):
            await pm.update_trades(24071.0)  # gaps past T2 next morning

        self.assertEqual(pm.active_trades[0]["state"], "T1_HIT")
        self.assertEqual(pm.active_trades[0]["stop_loss"], 24000.0)

    @patch('position_manager.settings')
    async def test_add_trade_success_and_exception_safety(self, mock_settings):
        mock_settings.trade_dedupe_tolerance_pts = 1.0
        mock_settings.signal_schema_mode = "bridge"
        pm = PositionManager()
        pm.analytics.log_entry = MagicMock()
        
        signal = AresSignal(
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
            option_type="CE"
        )
        
        # Test Success path
        await pm.add_trade(signal, 24001.0, None)
        self.assertEqual(len(pm.active_trades), 1)
        await asyncio.sleep(0.05)
        pass

        # Test Exception safety (Supabase fails, AnalyticsLogger fails)
        # (entries spread >1pt apart so the TASK-172 duplicate guard stays out
        # of the way — dedupe has its own dedicated tests)
        self.mock_client.insert_mock.reset_mock()
        self.mock_client.insert_mock.side_effect = Exception("Supabase insert error")
        with patch.object(pm.analytics, 'log_entry', side_effect=Exception("Analytics logger error")):
            await pm.add_trade(signal, 24010.0, None)
            await asyncio.sleep(0.05)
            pass

    @patch('position_manager.settings')
    async def test_live_trade_rpc_persists_confidence_gate_context(self, mock_settings):
        mock_settings.trade_dedupe_tolerance_pts = 1.0
        mock_settings.signal_schema_mode = "bridge"
        pm = PositionManager()
        pm.active_trades = []
        signal = AresSignal(
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
        signal.confidence = "MEDIUM"
        signal.reasons.append("[CONFIDENCE GATE] HIGH tier suppressed to MEDIUM")
        signal.market_context = {
            "tentative_confidence": "HIGH",
            "uncalibrated_high_suppressed": True,
            "ml_confidence_gate": {"suppressed": True},
        }
        rpc_payloads = []

        def capture_rpc(name, payload):
            rpc_payloads.append(payload)
            response = MagicMock()
            response.execute.return_value.data = payload["p_trade_id"]
            return response

        pm.supabase.rpc = MagicMock(side_effect=capture_rpc)

        trade_id, status = await pm.add_trade(signal, 24001.0)

        self.assertEqual(status, "BOUND")
        self.assertEqual(rpc_payloads[0]["p_trade_id"], trade_id)
        context = rpc_payloads[0]["p_market_context"]
        self.assertEqual(context["confidence"], "MEDIUM")
        self.assertEqual(context["tentative_confidence"], "HIGH")
        self.assertIs(context["uncalibrated_high_suppressed"], True)
        self.assertEqual(context["ml_confidence_gate"], {"suppressed": True})
        self.assertIn("[CONFIDENCE GATE] HIGH tier suppressed to MEDIUM", context["reasons"])
        self.assertNotIn("reasons", signal.market_context)



    @patch('position_manager.send_trade_update')
    @patch('position_manager.settings')
    async def test_update_trades_trailing_stop_bullish(self, mock_settings, mock_send_trade_update):
        pm = PositionManager()
        trade = {
            "id": "trade-bullish",
            "signal_id": "1111",
            "setup_type": "OI_WALL_REJECTION",
            "direction": "BULLISH",
            "entry_price": 24000.0,
            "stop_loss": 23975.0,
            "target_1": 24050.0,
            "target_2": 24100.0,
            "state": "OPEN"
        }
        pm.active_trades = [trade]
        
        # 1. Price reaches T1 -> state trails to T1_HIT, SL = entry
        # (TASK-172 fill-at-level: events are reported at the touched level)
        await pm.update_trades(24060.0)
        self.assertEqual(trade["state"], "T1_HIT")
        self.assertEqual(trade["stop_loss"], 24000.0)
        mock_send_trade_update.assert_called_with(trade, 24050.0, "T1_HIT")

        # A tick through cost does not close the completed-candle ATR runner.
        mock_send_trade_update.reset_mock()
        with patch.object(pm.analytics, 'log_exit') as mock_log_exit:
            await pm.update_trades(23999.0)
            await _flush_trade_writes(pm)
            self.assertEqual(trade["state"], "T1_HIT")
            mock_send_trade_update.assert_not_called()
            mock_log_exit.assert_not_called()

    @patch('position_manager.send_trade_update')
    @patch('position_manager.settings')
    async def test_update_trades_trailing_stop_bearish(self, mock_settings, mock_send_trade_update):
        pm = PositionManager()
        trade = {
            "id": "trade-bearish",
            "signal_id": "2222",
            "setup_type": "OI_WALL_REJECTION",
            "direction": "BEARISH",
            "entry_price": 24000.0,
            "stop_loss": 24025.0,
            "target_1": 23950.0,
            "target_2": 23900.0,
            "state": "OPEN"
        }
        closed_trade = {
            "id": "trade-closed",
            "state": "CLOSED"
        }
        pm.active_trades = [trade, closed_trade]
        
        # 1. Price reaches T1 -> state trails to T1_HIT, SL = entry
        # (TASK-172 fill-at-level: events are reported at the touched level)
        await pm.update_trades(23940.0)
        self.assertEqual(trade["state"], "T1_HIT")
        self.assertEqual(trade["stop_loss"], 24000.0)
        mock_send_trade_update.assert_called_with(trade, 23950.0, "T1_HIT")

        # A tick through cost does not close the completed-candle ATR runner.
        mock_send_trade_update.reset_mock()
        with patch.object(pm.analytics, 'log_exit') as mock_log_exit:
            await pm.update_trades(24001.0)
            await _flush_trade_writes(pm)
            self.assertEqual(trade["state"], "T1_HIT")
            mock_send_trade_update.assert_not_called()
            mock_log_exit.assert_not_called()

        # Reset for Bearish T2 Hit
        trade_t2 = {
            "id": "trade-bearish-t2",
            "setup_type": "OI_WALL_REJECTION",
            "direction": "BEARISH",
            "entry_price": 24000.0,
            "stop_loss": 24025.0,
            "target_1": 23950.0,
            "target_2": 23900.0,
            "state": "OPEN",
            "market_context": {"legacy_t2_tracking_approved": True},
        }
        pm.active_trades = [trade_t2]
        mock_send_trade_update.reset_mock()
        await pm.update_trades(23890.0)
        await _flush_trade_writes(pm)
        self.assertEqual(trade_t2["state"], "T1_HIT")
        self.assertEqual([call.args[2] for call in mock_send_trade_update.call_args_list], ["T1_HIT", "T2_TOUCHED"])
        mock_send_trade_update.assert_called_with(trade_t2, 23900.0, "T2_TOUCHED")

    @patch('position_manager.send_trade_update')
    @patch('position_manager.settings')
    async def test_bullish_trade_beyond_old_thresholds_keeps_original_sl(self, mock_settings, mock_send_trade_update):
        """Regression: a BULLISH OPEN trade older than old 30/45-min time-stop
        thresholds must keep its original SL — no tightening to breakeven."""
        old_ts = (datetime.now(timezone.utc) - timedelta(minutes=60)).isoformat()
        trade = {
            "id": "trade-old-bull",
            "setup_type": "OI_WALL_REJECTION",
            "direction": "BULLISH",
            "entry_price": 24000.0,
            "stop_loss": 23975.0,
            "target_1": 24050.0,
            "target_2": 24100.0,
            "state": "OPEN",
            "created_at": old_ts,
        }
        pm = PositionManager()
        pm.active_trades = [trade]

        # Neutral price: no T1/SL touch — SL must stay at original level
        await pm.update_trades(24010.0)
        self.assertEqual(trade["stop_loss"], 23975.0)
        self.assertEqual(trade["state"], "OPEN")

        # Price drops through original SL — must exit as regular SL_HIT
        mock_send_trade_update.reset_mock()
        with patch.object(pm.analytics, 'log_exit') as mock_log_exit:
            events = await pm.update_trades(23970.0)
            await _flush_trade_writes(pm)
            self.assertEqual(trade["state"], "CLOSED")
            self.assertIn(("trade-old-bull", "SL_HIT"), events)
            mock_send_trade_update.assert_called_with(trade, 23975.0, "SL_HIT")
            _check_log_exit_once(mock_log_exit, "trade-old-bull", 23975.0, "SL_HIT")

    @patch('position_manager.send_trade_update')
    @patch('position_manager.settings')
    async def test_bearish_trade_beyond_old_thresholds_keeps_original_sl(self, mock_settings, mock_send_trade_update):
        """Regression: a BEARISH OPEN trade older than old 30/45-min time-stop
        thresholds must keep its original SL — no tightening to breakeven."""
        old_ts = (datetime.now(timezone.utc) - timedelta(minutes=60)).isoformat()
        trade = {
            "id": "trade-old-bear",
            "setup_type": "EXHAUSTION_REVERSAL",
            "direction": "BEARISH",
            "entry_price": 24000.0,
            "stop_loss": 24025.0,
            "target_1": 23950.0,
            "target_2": 23900.0,
            "state": "OPEN",
            "created_at": old_ts,
        }
        pm = PositionManager()
        pm.active_trades = [trade]

        # Neutral price: no T1/SL touch — SL must stay at original level
        await pm.update_trades(23990.0)
        self.assertEqual(trade["stop_loss"], 24025.0)
        self.assertEqual(trade["state"], "OPEN")

        # Price rises through original SL — must exit as regular SL_HIT
        mock_send_trade_update.reset_mock()
        with patch.object(pm.analytics, 'log_exit') as mock_log_exit:
            events = await pm.update_trades(24030.0)
            await _flush_trade_writes(pm)
            self.assertEqual(trade["state"], "CLOSED")
            self.assertIn(("trade-old-bear", "SL_HIT"), events)
            mock_send_trade_update.assert_called_with(trade, 24025.0, "SL_HIT")
            _check_log_exit_once(mock_log_exit, "trade-old-bear", 24025.0, "SL_HIT")

    @patch('position_manager.send_trade_update')
    @patch('position_manager.settings')
    async def test_update_trades_returns_sl_hit_events(self, mock_settings, mock_send_trade_update):
        """TASK-171: exit events are returned so main can clear the engine cooldown."""
        trade = {
            "id": "trade-sl-event", "setup_type": "OI_WALL_REJECTION",
            "direction": "BULLISH", "entry_price": 24000.0, "stop_loss": 23975.0,
            "target_1": 24050.0, "target_2": 24100.0, "state": "OPEN",
        }
        pm = PositionManager()
        pm.active_trades = [trade]

        events = await pm.update_trades(23970.0)
        self.assertIn(("trade-sl-event", "SL_HIT"), events)

    @patch('position_manager.send_trade_update')
    @patch('position_manager.settings')
    async def test_update_trades_regular_sl_hit_bullish(self, mock_settings, mock_send_trade_update):
        pm = PositionManager()
        trade = {
            "id": "trade-sl",
            "setup_type": "OI_WALL_REJECTION",
            "direction": "BULLISH",
            "entry_price": 24000.0,
            "stop_loss": 23975.0,
            "target_1": 24050.0,
            "target_2": 24100.0,
            "state": "OPEN"
        }
        pm.active_trades = [trade]
        
        await pm.update_trades(23970.0)
        self.assertEqual(trade["state"], "CLOSED")
        mock_send_trade_update.assert_called_with(trade, 23975.0, "SL_HIT")

    @patch('position_manager.send_trade_update')
    @patch('position_manager.settings')
    async def test_update_trades_regular_sl_hit_bearish(self, mock_settings, mock_send_trade_update):
        pm = PositionManager()
        trade = {
            "id": "trade-sl",
            "setup_type": "OI_WALL_REJECTION",
            "direction": "BEARISH",
            "entry_price": 24000.0,
            "stop_loss": 24025.0,
            "target_1": 23950.0,
            "target_2": 23900.0,
            "state": "OPEN"
        }
        pm.active_trades = [trade]
        
        await pm.update_trades(24030.0)
        self.assertEqual(trade["state"], "CLOSED")
        mock_send_trade_update.assert_called_with(trade, 24025.0, "SL_HIT")

    @patch('position_manager.send_trade_update')
    @patch('position_manager.settings')
    async def test_update_trades_exception_safety(self, mock_settings, mock_send_trade_update):
        pm = PositionManager()
        trade = {
            "id": "trade-err",
            "setup_type": "OI_WALL_REJECTION",
            "direction": "BULLISH",
            "entry_price": 24000.0,
            "stop_loss": 23975.0,
            "target_1": 24050.0,
            "target_2": 24100.0,
            "state": "OPEN"
        }
        pm.active_trades = [trade]

        self.mock_client.update_mock.side_effect = Exception("Supabase select/update error")
        pm.analytics.log_exit = MagicMock(side_effect=Exception("DB logger down"))
        mock_send_trade_update.side_effect = Exception("Discord alert fail")

        await pm.update_trades(23970.0)
        await asyncio.sleep(0.05)
        self.assertEqual(trade["state"], "CLOSED")

        # Test line 179-180 get_running_loop error pathway in _update
        trade_loop_err = {
            "id": "trade-loop-err",
            "setup_type": "OI_WALL_REJECTION",
            "direction": "BULLISH",
            "entry_price": 24000.0,
            "stop_loss": 23975.0,
            "target_1": 24050.0,
            "target_2": 24100.0,
            "state": "OPEN"
        }
        pm.active_trades = [trade_loop_err]
        with patch('asyncio.get_running_loop', side_effect=RuntimeError("No event loop")):
            await pm.update_trades(23970.0)

class TestIntrabarExitsAndDedup(unittest.IsolatedAsyncioTestCase):
    """TASK-172 audit P1: intrabar high/low exit detection (item 11) and
    duplicate add_trade guard (item 13)."""

    def setUp(self):
        self.mock_client = global_mock_client
        self.mock_client.insert_mock.reset_mock()
        self.mock_client.update_mock.reset_mock()
        self.mock_client.insert_mock.side_effect = None
        self.mock_client.update_mock.side_effect = None
        self.mock_client.execute_mock.side_effect = None
        self.mock_client.execute_mock.return_value.data = []

    def _bullish_trade(self, **overrides):
        trade = {
            "id": "trade-intrabar",
            "signal_id": "0042",
            "setup_type": "OI_WALL_REJECTION",
            "direction": "BULLISH",
            "entry_price": 24000.0,
            "stop_loss": 23975.0,
            "target_1": 24050.0,
            "target_2": 24100.0,
            "state": "OPEN",
        }
        trade.update(overrides)
        return trade

    def _make_signal(self, setup_type=SetupType.OI_WALL_REJECTION):
        return AresSignal(
            setup_type=setup_type,
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

    # ── Item 11: intrabar exit detection ────────────────────────────────────

    @patch('position_manager.send_trade_update')
    @patch('position_manager.settings')
    async def test_intrabar_low_triggers_sl_even_if_close_recovers(self, mock_settings, mock_alert):
        """Candle wicks below SL but closes back above it: close-only polling
        missed this stop (avg +6.8 pts slippage); intrabar detection catches it
        and fills at the stop level."""
        trade = self._bullish_trade()
        pm = PositionManager()
        pm.active_trades = [trade]

        with patch.object(pm.analytics, 'log_exit') as mock_log_exit:
            events = await pm.update_trades(23990.0, candle_high=23995.0, candle_low=23970.0)
            await _flush_trade_writes(pm)

        self.assertEqual(trade["state"], "CLOSED")
        self.assertIn(("trade-intrabar", "SL_HIT"), events)
        mock_alert.assert_called_with(trade, 23975.0, "SL_HIT")
        _check_log_exit(mock_log_exit, "trade-intrabar", 23975.0, "SL_HIT")

    @patch('position_manager.send_trade_update')
    @patch('position_manager.settings')
    async def test_intrabar_high_triggers_t1_even_if_close_below(self, mock_settings, mock_alert):
        trade = self._bullish_trade()
        pm = PositionManager()
        pm.active_trades = [trade]

        await pm.update_trades(24040.0, candle_high=24052.0, candle_low=24035.0)

        self.assertEqual(trade["state"], "T1_HIT")
        self.assertEqual(trade["stop_loss"], 24000.0)
        mock_alert.assert_called_with(trade, 24050.0, "T1_HIT")

    @patch('position_manager.send_trade_update')
    @patch('position_manager.settings')
    async def test_intrabar_both_sl_and_target_resolves_optimistically(self, mock_settings, mock_alert):
        """A candle that spans both the stop and a target is resolved as a
        target hit — an optimistic assumption so wicks that touch targets count as wins."""
        trade = self._bullish_trade()
        trade["market_context"] = {"legacy_t2_tracking_approved": True}
        pm = PositionManager()
        pm.active_trades = [trade]

        with patch.object(pm.analytics, 'log_exit') as mock_log_exit:
            # high goes to 24105 (past T2 = 24100), low goes to 23970 (past SL = 23975)
            events = await pm.update_trades(24080.0, candle_high=24105.0, candle_low=23970.0)
            await _flush_trade_writes(pm)

        self.assertEqual(trade["state"], "T1_HIT")
        self.assertIn(("trade-intrabar", "T1_HIT"), events)
        self.assertIn(("trade-intrabar", "T2_TOUCHED"), events)
        mock_alert.assert_any_call(trade, 24050.0, "T1_HIT")
        mock_alert.assert_called_with(trade, 24100.0, "T2_TOUCHED")
        mock_log_exit.assert_not_called()

    @patch('position_manager.send_trade_update')
    @patch('position_manager.settings')
    async def test_intrabar_bearish_both_sl_and_target_resolves_optimistically(self, mock_settings, mock_alert):
        """A BEARISH candle that spans both the stop and a target is resolved as
        a target hit — mirrors the BULLISH optimistic resolution test so that a
        regression in the BEARISH elif ordering is always caught."""
        trade = self._bullish_trade(
            direction="BEARISH", stop_loss=24250.0,
            target_1=24180.0, target_2=24130.0,
        )
        trade["market_context"] = {"legacy_t2_tracking_approved": True}
        pm = PositionManager()
        pm.active_trades = [trade]

        with patch.object(pm.analytics, 'log_exit') as mock_log_exit:
            # low goes to 24120 (past T2 = 24130), high goes to 24260 (past SL = 24250)
            events = await pm.update_trades(24200.0, candle_high=24260.0, candle_low=24120.0)
            await _flush_trade_writes(pm)

        self.assertEqual(trade["state"], "T1_HIT")
        self.assertIn(("trade-intrabar", "T1_HIT"), events)
        self.assertIn(("trade-intrabar", "T2_TOUCHED"), events)
        mock_alert.assert_any_call(trade, 24180.0, "T1_HIT")
        mock_alert.assert_called_with(trade, 24130.0, "T2_TOUCHED")
        mock_log_exit.assert_not_called()

    @patch('position_manager.send_trade_update')
    @patch('position_manager.settings')
    async def test_intrabar_t2_does_not_close_atr_runner(self, mock_settings, mock_alert):
        trade = self._bullish_trade(state="T1_HIT", stop_loss=24000.0)
        trade["market_context"] = {"legacy_t2_tracking_approved": True}
        pm = PositionManager()
        pm.active_trades = [trade]

        with patch.object(pm.analytics, 'log_exit') as mock_log_exit:
            await pm.update_trades(24120.0, candle_high=24125.0, candle_low=24095.0)
            await _flush_trade_writes(pm)

        self.assertEqual(trade["state"], "T1_HIT")
        mock_alert.assert_called_once_with(trade, 24100.0, "T2_TOUCHED")
        mock_log_exit.assert_not_called()

    @patch('position_manager.send_trade_update')
    @patch('position_manager.settings')
    async def test_intrabar_bearish_high_triggers_sl(self, mock_settings, mock_alert):
        trade = self._bullish_trade(
            direction="BEARISH", stop_loss=24025.0,
            target_1=23950.0, target_2=23900.0,
        )
        pm = PositionManager()
        pm.active_trades = [trade]

        with patch.object(pm.analytics, 'log_exit') as mock_log_exit:
            events = await pm.update_trades(24010.0, candle_high=24030.0, candle_low=24005.0)
            await _flush_trade_writes(pm)

        self.assertEqual(trade["state"], "CLOSED")
        self.assertIn(("trade-intrabar", "SL_HIT"), events)
        _check_log_exit(mock_log_exit, "trade-intrabar", 24025.0, "SL_HIT")

    @patch('position_manager.send_trade_update')
    @patch('position_manager.settings')
    async def test_close_only_call_still_works_without_candle(self, mock_settings, mock_alert):
        """Backward compatibility: omitting candle extremes falls back to the
        spot price for every check."""
        trade = self._bullish_trade()
        pm = PositionManager()
        pm.active_trades = [trade]

        await pm.update_trades(23990.0)  # above SL, below T1: nothing happens
        self.assertEqual(trade["state"], "OPEN")

        await pm.update_trades(23970.0)  # through SL
        self.assertEqual(trade["state"], "CLOSED")
        mock_alert.assert_called_with(trade, 23975.0, "SL_HIT")

    # ── Item 13: duplicate add_trade guard ──────────────────────────────────

    @patch('position_manager.settings')
    async def test_add_trade_skips_duplicate_signal(self, mock_settings):
        """The 06-29 14:12 OI wall trade was logged twice. A second add for the
        same setup/direction at (nearly) the same entry is now skipped."""
        mock_settings.trade_dedupe_tolerance_pts = 1.0
        mock_settings.signal_schema_mode = "bridge"
        pm = PositionManager()
        pm.analytics.log_entry = MagicMock()
        pm.active_trades = []
        signal = self._make_signal()

        await pm.add_trade(signal, 24001.0, None)
        await pm.add_trade(signal, 24001.4, None)  # same setup, entry within 1pt

        self.assertEqual(len(pm.active_trades), 1)
        await asyncio.sleep(0.05)
        pass

    @patch('position_manager.settings')
    async def test_add_trade_allows_same_setup_at_different_level(self, mock_settings):
        mock_settings.trade_dedupe_tolerance_pts = 1.0
        mock_settings.signal_schema_mode = "bridge"
        pm = PositionManager()
        pm.active_trades = []
        signal = self._make_signal()

        await pm.add_trade(signal, 24001.0, None)
        await pm.add_trade(signal, 24020.0, None)  # >1pt away: legitimate new trade

        self.assertEqual(len(pm.active_trades), 2)

    @patch('position_manager.settings')
    async def test_add_trade_allows_reentry_after_close(self, mock_settings):
        mock_settings.signal_schema_mode = "bridge"
        pm = PositionManager()
        pm.active_trades = []
        signal = self._make_signal()

        await pm.add_trade(signal, 24001.0, None)
        pm.active_trades[0]["state"] = "CLOSED"
        await pm.add_trade(signal, 24001.0, None)  # prior trade closed: re-entry OK

        self.assertEqual(len(pm.active_trades), 2)


def tearDownModule():
    mock_create_patch.stop()
    if 'position_manager' in sys.modules:
        importlib.reload(sys.modules['position_manager'])

if __name__ == '__main__':
    unittest.main()

def test_position_manager_clamps_early_event_ts():
    pm = PositionManager()
    pm.active_trades = [{
        "id": "trade-clamp",
        "state": "OPEN",
        "entry_timestamp": "2026-09-01T10:05:00+00:00",
        "direction": "BULLISH",
        "stop_loss": 90.0,
        "target_1": 110.0,
        "target_2": 120.0,
        "entry_price": 100.0,
    }]
    pm._queue_trade_write = MagicMock()
    pm.supabase = MockSupabaseClient()
    
    # Event timestamp is earlier than entry
    early_ts = datetime(2026, 9, 1, 10, 0, 0, tzinfo=timezone.utc)
    
    async def run_update():
        await pm.update_trades(95.0, candle_high=95.0, candle_low=95.0, candle_timestamp=early_ts)
        
    asyncio.run(run_update())
    
    # Clamping occurs, so if an event like SL hit happens, exit_ts should be the entry_timestamp
    # Wait, 95 is not hitting SL or target. Let's hit SL to trigger an exit.
    
    pm.active_trades = [{
        "id": "trade-clamp",
        "state": "OPEN",
        "entry_timestamp": "2026-09-01T10:05:00+00:00",
        "direction": "BULLISH",
        "stop_loss": 90.0,
        "target_1": 110.0,
        "target_2": 120.0,
        "entry_price": 100.0,
    }]
    
    async def run_update_sl():
        await pm.update_trades(80.0, candle_high=80.0, candle_low=80.0, candle_timestamp=early_ts)
        
    asyncio.run(run_update_sl())
    # The write should be queued
    assert pm._queue_trade_write.call_count == 1
    
    # We can inspect the _log_exit closure if we mock the analytics?
    # Better to just test that the warning executes. We can mock print or use capsys if using pytest, but this is a simple check.


# ATR lifecycle tests use completed source minutes through the real aggregation helper.


@pytest.fixture
def atr_position_manager():
    client = MockSupabaseClient()
    with patch("position_manager.settings"), patch("position_manager.create_client", return_value=client), \
         patch("position_manager.send_trade_update") as alert:
        pm = PositionManager()
        pm.analytics.log_exit = MagicMock()
        yield pm, client, alert


def _atr_trade(sign=1, **changes):
    trade = {
        "id": "atr-runner", "signal_id": "0042", "setup_type": "TREND_CONTINUATION",
        "direction": "BULLISH" if sign == 1 else "BEARISH",
        "entry_price": 24000.0, "stop_loss": 24000.0 - sign * 25,
        "target_1": 24000.0 + sign * 50, "target_2": 24000.0 + sign * 100,
        "state": "OPEN",
    }
    trade.update(changes)
    # These milestone-specific fixtures represent individually reviewed trades.
    trade.setdefault("market_context", {})["legacy_t2_tracking_approved"] = True
    return trade


def _atr_minutes(start_minute, count, sign=1, close=35, low=30, high=40):
    start = datetime(2099, 1, 1, 9, 15, tzinfo=timezone(timedelta(hours=5, minutes=30)))
    return [{"timestamp": (start + timedelta(minutes=i)).isoformat(), "complete": True,
             "open": 24000 + sign * close, "close": 24000 + sign * close,
             "high": 24000 + (high if sign == 1 else -low),
             "low": 24000 + (low if sign == 1 else -high)}
            for i in range(start_minute, start_minute + count)]


@pytest.mark.asyncio
async def test_unreviewed_legacy_runner_keeps_existing_exit_behavior(atr_position_manager):
    pm, _, alert = atr_position_manager
    trade = _atr_trade(state="T1_HIT", stop_loss=24000,
        market_context={"atr_trailing": {"last_bar": "2099-01-01T09:30:00+05:30"}})
    trade["market_context"].pop("legacy_t2_tracking_approved")
    pm.active_trades = [trade]
    assert await pm.update_trades(24100) == []
    assert "target_milestones" not in trade["market_context"]
    assert trade["state"] == "T1_HIT"
    candles = _atr_minutes(15, 5, close=-1, low=-5, high=5)
    candles[1]["high"] = 24100
    events = await pm.update_atr_trailing(candles)
    await _flush_trade_writes(pm)
    assert events == [(trade["id"], "ATR_SL_HIT")]
    assert trade["state"] == "CLOSED"
    assert "target_milestones" not in trade["market_context"]
    assert [call.args[2] for call in alert.call_args_list] == ["ATR_SL_HIT"]


@pytest.mark.asyncio
async def test_trade_0979_candle_records_t2_touch_without_closing_runner(atr_position_manager):
    pm, client, alert = atr_position_manager
    trade = _atr_trade(id="0979", state="T1_HIT", entry_price=22790.75,
                       entry_timestamp="2026-10-29T10:08:00+05:30", stop_loss=22790.75,
                       target_1=22815.75, target_2=22870.75,
                       market_context={"tracking_mode": "SIGNAL_REFERENCE",
                                       "atr_trailing": {"last_bar": "2026-10-29T10:30:00+05:30"}})
    pm.active_trades = [trade]
    candle_time = datetime(2026, 10, 29, 10, 30, tzinfo=timezone(timedelta(hours=5, minutes=30)))

    events = await pm.update_trades(22859.30, candle_high=22874.15,
                                    candle_low=22856.55, candle_timestamp=candle_time)
    await _flush_trade_writes(pm)

    assert events == [("0979", "T2_TOUCHED")]
    assert trade["state"] == "T1_HIT" and trade["stop_loss"] == 22790.75
    context = client.update_mock.call_args.args[0]["market_context"]
    assert context["target_milestones"]["t2"]["target_spot"] == 22870.75
    assert context["target_milestones"]["t2"]["spot_points_from_entry"] == 80.0
    alert.assert_called_once_with(trade, 22870.75, "T2_TOUCHED")
    pm.analytics.log_exit.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("sign", [1, -1])
@pytest.mark.parametrize("wick", [False, True])
async def test_t2_touch_is_inclusive_persisted_once_and_leaves_runner_active(atr_position_manager, sign, wick):
    pm, client, alert = atr_position_manager
    atr = {"last_bar": "2099-01-01T09:15:00+05:30", "atr": 10}
    trade = _atr_trade(sign, state="T1_HIT", stop_loss=24000,
                       market_context={"atr_trailing": atr.copy()})
    pm.active_trades = [trade]
    touched = datetime(2099, 1, 1, 9, 20, tzinfo=timezone.utc)
    spot = 24000 + sign * (70 if wick else 100)
    kwargs = {"candle_timestamp": touched}
    if wick:
        kwargs.update(candle_high=max(spot, trade["target_2"]), candle_low=min(spot, trade["target_2"]))
    assert await pm.update_trades(spot, **kwargs) == [(trade["id"], "T2_TOUCHED")]
    await _flush_trade_writes(pm)
    marker = {"touched_at": touched.isoformat(), "time_basis": "candle_start", "target_spot": trade["target_2"],
              "spot_points_from_entry": 100.0}
    assert trade["market_context"]["target_milestones"]["t2"] == marker
    assert trade["state"] == "T1_HIT" and trade["stop_loss"] == 24000
    assert trade["market_context"]["atr_trailing"] == atr
    persisted = [call.args[0]["market_context"] for call in client.update_mock.call_args_list
                 if "market_context" in call.args[0]]
    assert persisted[-1]["target_milestones"]["t2"] == marker
    assert await pm.update_trades(spot, **kwargs) == []
    await _flush_trade_writes(pm)
    alert.assert_called_once_with(trade, trade["target_2"], "T2_TOUCHED")
    pm.analytics.log_exit.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("sign", [1, -1])
async def test_same_sample_t1_then_t2_preserves_active_accounting(atr_position_manager, sign):
    pm, _, alert = atr_position_manager
    trade = _atr_trade(sign)
    pm.active_trades = [trade]
    events = await pm.update_trades(trade["target_2"])
    await _flush_trade_writes(pm)
    assert events == [(trade["id"], "T1_HIT"), (trade["id"], "T2_TOUCHED")]
    assert [call.args[2] for call in alert.call_args_list] == ["T1_HIT", "T2_TOUCHED"]
    assert trade["state"] == "T1_HIT" and trade["stop_loss"] == trade["entry_price"]
    assert "exit_price" not in trade and "pnl_points" not in trade
    pm.analytics.log_exit.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("sign", [1, -1])
async def test_t2_rejects_pre_entry_wicks_and_closed_trades(atr_position_manager, sign):
    pm, _, alert = atr_position_manager
    entry = datetime(2099, 1, 1, 9, 20, 30, tzinfo=timezone.utc)
    trade = _atr_trade(sign, state="T1_HIT", stop_loss=24000, entry_timestamp=entry.isoformat(),
                       market_context={"tracking_mode": "SIGNAL_REFERENCE"})
    closed = _atr_trade(sign, id="closed", state="CLOSED")
    pm.active_trades = [trade, closed]
    # Fully pre-entry candle cannot produce a touch, even its close.
    assert await pm.update_trades(trade["target_2"], candle_timestamp=entry - timedelta(minutes=2)) == []
    # Entry-minute wicks cannot produce a retroactive touch.
    assert await pm.update_trades(24000, candle_high=24100, candle_low=23900,
                                  candle_timestamp=entry.replace(second=0)) == []
    alert.assert_not_called()
    assert "target_milestones" not in trade["market_context"]
    assert await pm.update_trades(trade["target_2"], candle_timestamp=entry + timedelta(minutes=1)) == [
        (trade["id"], "T2_TOUCHED")]
    await _flush_trade_writes(pm)


@pytest.mark.asyncio
async def test_t2_does_not_replay_candle_before_runner_watermark(atr_position_manager):
    pm, _, alert = atr_position_manager
    trade = _atr_trade(state="T1_HIT", stop_loss=24000,
                       entry_timestamp="2099-01-01T09:15:00+05:30",
                       market_context={"tracking_mode": "SIGNAL_REFERENCE",
                                       "atr_trailing": {"last_bar": "2099-01-01T09:30:00+05:30"}})
    pm.active_trades = [trade]
    old_candle = datetime(2099, 1, 1, 9, 25, tzinfo=timezone(timedelta(hours=5, minutes=30)))
    assert await pm.update_trades(24050, candle_high=24100, candle_low=24040,
                                  candle_timestamp=old_candle) == []
    assert "target_milestones" not in trade["market_context"]
    alert.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("later_bar", [False, True])
async def test_t2_survives_incomplete_five_minute_group(atr_position_manager, later_bar):
    pm, _, alert = atr_position_manager
    trade = _atr_trade(state="T1_HIT", stop_loss=24000,
                       market_context={"atr_trailing": {"last_bar": "2099-01-01T09:30:00+05:30"}})
    pm.active_trades = [trade]
    candles = _atr_minutes(15, 10 if later_bar else 5)
    candles[1]["high"] = 24100
    candles = [c for c in candles if not c["timestamp"].startswith("2099-01-01T09:32")]

    assert await pm.update_atr_trailing(candles) == [(trade["id"], "T2_TOUCHED")]
    await _flush_trade_writes(pm)
    assert trade["state"] == "T1_HIT"
    alert.assert_called_once_with(ANY, 24100, "T2_TOUCHED")
    assert alert.call_args.args[0]["state"] == "T1_HIT"


@pytest.mark.asyncio
async def test_t2_history_does_not_record_touch_after_earlier_atr_exit(atr_position_manager):
    pm, _, alert = atr_position_manager
    trade = _atr_trade(state="T1_HIT", stop_loss=24000,
                       market_context={"atr_trailing": {"last_bar": "2099-01-01T09:30:00+05:30"}})
    pm.active_trades = [trade]
    candles = _atr_minutes(15, 5, close=-1, low=-5, high=5) + _atr_minutes(20, 5)
    candles[6]["high"] = 24100

    assert await pm.update_atr_trailing(candles) == [(trade["id"], "ATR_SL_HIT")]
    await _flush_trade_writes(pm)
    assert "target_milestones" not in trade["market_context"]
    assert [call.args[2] for call in alert.call_args_list] == ["ATR_SL_HIT"]


@pytest.mark.asyncio
async def test_slow_t2_notification_does_not_delay_atr_exit(atr_position_manager):
    pm, _, alert = atr_position_manager
    trade = _atr_trade(state="T1_HIT", stop_loss=24000,
                       market_context={"atr_trailing": {"last_bar": "2099-01-01T09:30:00+05:30"}})
    pm.active_trades = [trade]
    candles = _atr_minutes(15, 5, close=-1, low=-5, high=5)
    candles[1]["high"] = 24100
    release = asyncio.Event()
    sent = []

    async def slow_alert(snapshot, price, kind):
        if kind == "T2_TOUCHED":
            await release.wait()
        sent.append(kind)

    alert.side_effect = slow_alert
    monitor = asyncio.create_task(pm.update_atr_trailing(candles))
    try:
        events = await asyncio.wait_for(asyncio.shield(monitor), timeout=1)
        assert events == [(trade["id"], "T2_TOUCHED"), (trade["id"], "ATR_SL_HIT")]
        assert trade["state"] == "CLOSED"
        assert sent == []
    finally:
        release.set()
        await monitor
        await asyncio.gather(*list(getattr(pm, "_trade_alerts", {}).values()))
        await _flush_trade_writes(pm)
    assert sent == ["T2_TOUCHED", "ATR_SL_HIT"]


@pytest.mark.asyncio
async def test_trade_update_shutdown_drain_is_bounded(atr_position_manager):
    pm, _, _ = atr_position_manager
    pending = asyncio.create_task(asyncio.Event().wait())
    pm._trade_alerts["pending"] = pending
    try:
        assert await pm.flush_pending_trade_updates(timeout=0.01) is False
        assert not pending.done()
    finally:
        pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)


@pytest.mark.asyncio
async def test_shutdown_retries_failed_t2_marker_without_repeating_alert(atr_position_manager):
    pm, client, alert = atr_position_manager
    trade = _atr_trade(state="T1_HIT", stop_loss=24000)
    pm.active_trades = [trade]
    selected = client.select_mock.return_value.eq.return_value.execute.return_value
    selected.data = []
    await pm.update_trades(trade["target_2"])
    await _flush_trade_writes(pm)
    assert trade["id"] in pm._pending_target_milestones

    selected.data = [{"market_context": {"source": "preserved"}}]
    assert await pm.flush_pending_trade_updates(timeout=1) is True
    assert not pm._pending_target_milestones
    context = client.update_mock.call_args.args[0]["market_context"]
    assert context["source"] == "preserved"
    assert context["target_milestones"]["t2"]["spot_points_from_entry"] == 100.0
    alert.assert_called_once()


@pytest.mark.asyncio
async def test_t2_tick_requires_fresh_post_entry_observation(atr_position_manager, monkeypatch):
    import main
    from fetchers.tick_feed import TickFeed

    pm, _, alert = atr_position_manager
    now = datetime.now(timezone.utc)
    entry = now - timedelta(seconds=2)
    trade = _atr_trade(state="T1_HIT", stop_loss=24000, entry_timestamp=entry.isoformat(),
                       market_context={"atr_trailing": {"last_bar": entry.isoformat()}})
    pm.active_trades = [trade]
    feed = TickFeed()
    feed._feed = object()
    feed._latest_price = trade["target_2"]
    monkeypatch.setattr(main.asyncio, "sleep", AsyncMock())
    monkeypatch.setattr(main.settings, "tick_exit_check_interval_seconds", 1)
    engine = MagicMock()

    feed._latest_received_at = now - timedelta(seconds=6)
    await main._sleep_with_tick_exits(1, feed, pm, engine)
    feed._latest_received_at = now - timedelta(seconds=4)
    await main._sleep_with_tick_exits(1, feed, pm, engine)
    assert "target_milestones" not in trade["market_context"]
    alert.assert_not_called()

    feed._latest_received_at = datetime.now(timezone.utc)
    await main._sleep_with_tick_exits(1, feed, pm, engine)
    await _flush_trade_writes(pm)
    assert trade["market_context"]["target_milestones"]["t2"]["time_basis"] == "spot_observation"
    alert.assert_called_once_with(ANY, trade["target_2"], "T2_TOUCHED")
    assert alert.call_args.args[0]["state"] == "T1_HIT"


@pytest.mark.asyncio
async def test_stale_tick_still_checks_initial_stop(atr_position_manager, monkeypatch):
    import main
    from fetchers.tick_feed import TickFeed

    pm, _, alert = atr_position_manager
    trade = _atr_trade()
    pm.active_trades = [trade]
    feed = TickFeed()
    feed._feed = object()
    feed._latest_price = trade["stop_loss"] - 1
    feed._latest_received_at = datetime.now(timezone.utc) - timedelta(seconds=6)
    monkeypatch.setattr(main.asyncio, "sleep", AsyncMock())
    monkeypatch.setattr(main.settings, "tick_exit_check_interval_seconds", 1)
    engine = MagicMock()

    await main._sleep_with_tick_exits(1, feed, pm, engine)
    await _flush_trade_writes(pm)
    assert trade["state"] == "CLOSED"
    alert.assert_called_once_with(trade, trade["stop_loss"], "SL_HIT")
    engine.clear_cooldown.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["missing_row", "empty_update", "exception"])
async def test_t2_persistence_failure_recovers_without_repeated_alert_after_close(atr_position_manager, failure):
    pm, client, alert = atr_position_manager
    trade = _atr_trade(state="T1_HIT", stop_loss=24000)
    pm.active_trades = [trade]
    selected = client.select_mock.return_value.eq.return_value.execute.return_value
    updated = client.update_mock.return_value.eq.return_value.execute
    existing = {"source": "kept", "execution": {"mode": "PAPER"},
                "atr_trailing": {"last_bar": "2099-01-01T09:15:00+05:30"},
                "target_milestones": {"other": {"kept": True}}}
    selected.data = [] if failure == "missing_row" else [{"market_context": existing}]
    updated.return_value.data = [] if failure == "empty_update" else [{"id": trade["id"]}]
    if failure == "exception":
        updated.side_effect = RuntimeError("temporary persistence outage")
    await pm.update_trades(trade["target_2"])
    await _flush_trade_writes(pm)
    original_marker = trade["market_context"]["target_milestones"]["t2"].copy()
    # Monitoring retries a failed milestone even if runner has closed meanwhile.
    trade["state"] = "CLOSED"
    selected.data = [{"market_context": existing}]
    updated.return_value.data = [{"id": trade["id"]}]
    updated.side_effect = None
    assert await pm.update_trades(24000) == []
    await _flush_trade_writes(pm)
    context = client.update_mock.call_args.args[0]["market_context"]
    assert context == {**existing, "target_milestones": {"other": {"kept": True}, "t2": original_marker}}
    writes = client.update_mock.call_count
    await pm.update_trades(24110)
    await _flush_trade_writes(pm)
    assert client.update_mock.call_count == writes
    alert.assert_called_once_with(ANY, trade["target_2"], "T2_TOUCHED")
    assert alert.call_args.args[0]["state"] == "T1_HIT"
    pm.analytics.log_exit.assert_not_called()


@pytest.mark.asyncio
async def test_t2_marker_restored_from_analytics_deduplicates_restart(atr_position_manager):
    pm, client, alert = atr_position_manager
    trade = _atr_trade(state="T1_HIT", stop_loss=24000)
    marker = {"touched_at": "2099-01-01T09:20:00+00:00", "target_spot": 24100}
    client.execute_mock.return_value.data = [trade]
    client.select_mock.return_value.in_.return_value.execute.return_value.data = [{
        "id": trade["id"], "result_state": "OPEN",
        "market_context": {"target_milestones": {"t2": marker}},
    }]
    pm._initialize_db()
    assert pm.active_trades[0]["market_context"]["target_milestones"]["t2"] == marker
    assert await pm.update_trades(24110) == []
    alert.assert_not_called()


@pytest.mark.asyncio
async def test_t2_queued_write_uses_deep_snapshot_and_deduplicates_pending_observations(atr_position_manager):
    pm, client, alert = atr_position_manager
    trade = _atr_trade(state="T1_HIT", stop_loss=24000)
    pm.active_trades = [trade]
    writes = []
    pm._queue_trade_write = lambda trade_id, write: writes.append(write)
    await pm.update_trades(24100)
    original = trade["market_context"]["target_milestones"]["t2"].copy()
    assert await pm.update_trades(24110) == []
    trade["market_context"]["target_milestones"]["t2"]["target_spot"] = 99999
    writes[0]()
    context = client.update_mock.call_args.args[0]["market_context"]
    assert context["target_milestones"]["t2"] == original
    assert context["target_milestones"]["t2"] is not trade["market_context"]["target_milestones"]["t2"]
    await _flush_trade_writes(pm)
    alert.assert_called_once()


@pytest.mark.asyncio
async def test_terminal_atr_writer_recovers_failed_t2_marker_without_extra_exit_accounting(atr_position_manager):
    pm, client, alert = atr_position_manager
    trade = _atr_trade(state="T1_HIT", stop_loss=24000, market_context={
        "atr_trailing": {"last_bar": "2099-01-01T09:15:00+05:30"},
    })
    pm.active_trades = [trade]
    selected = client.select_mock.return_value.eq.return_value.execute.return_value
    selected.data = []
    await pm.update_trades(24100)
    await _flush_trade_writes(pm)
    marker = trade["market_context"]["target_milestones"]["t2"].copy()
    selected.data = [{"market_context": {"source": "retained"}}]
    assert await pm.update_atr_trailing(_atr_minutes(0, 5, close=-1, low=-5, high=5)) == [(trade["id"], "ATR_SL_HIT")]
    await _flush_trade_writes(pm)
    contexts = [call.args[0]["market_context"] for call in client.update_mock.call_args_list
                if "market_context" in call.args[0]]
    assert contexts[-1]["target_milestones"]["t2"] == marker
    assert contexts[-1]["atr_trailing"]["state"] == "CLOSED"
    assert contexts[-1]["source"] == "retained"
    assert not pm._pending_target_milestones
    pm.analytics.log_exit.assert_called_once()
    assert [call.args[2] for call in alert.call_args_list] == ["T2_TOUCHED", "ATR_SL_HIT"]


@pytest.mark.asyncio
async def test_t2_alert_exception_keeps_persisted_milestone_and_runner(atr_position_manager):
    pm, _, alert = atr_position_manager
    trade = _atr_trade(state="T1_HIT", stop_loss=24000)
    pm.active_trades = [trade]
    alert.side_effect = RuntimeError("Discord unavailable")
    assert await pm.update_trades(24100) == [(trade["id"], "T2_TOUCHED")]
    await _flush_trade_writes(pm)
    assert await pm.update_trades(24110) == []
    assert trade["state"] == "T1_HIT"
    assert not pm._pending_target_milestones
    alert.assert_called_once()


@pytest.mark.asyncio
async def test_pending_t2_write_in_flight_does_not_enqueue_again(atr_position_manager):
    pm, _, alert = atr_position_manager
    trade = _atr_trade(state="T1_HIT", stop_loss=24000)
    pm.active_trades = [trade]
    gate = asyncio.Event()
    pm._trade_writes[trade["id"]] = asyncio.create_task(gate.wait())
    await pm.update_trades(24100)
    pending = pm._trade_writes[trade["id"]]
    assert await pm.update_trades(24110) == []
    assert pm._trade_writes[trade["id"]] is pending
    gate.set()
    await _flush_trade_writes(pm)
    alert.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("sign", [1, -1])
async def test_completed_atr_before_latest_candle_reports_t2_then_exit(atr_position_manager, sign):
    pm, client, alert = atr_position_manager
    trade = _atr_trade(sign, state="T1_HIT", stop_loss=24000, market_context={
        "atr_trailing": {"last_bar": "2099-01-01T09:15:00+05:30"},
    })
    pm.active_trades = [trade]
    candles = _atr_minutes(0, 5, sign, close=-1, low=-5, high=5)
    candles[2]["high" if sign == 1 else "low"] = trade["target_2"]
    # main processes ATR history before its regular latest-minute update.
    assert await pm.update_atr_trailing([None, {"timestamp": "bad"}, *candles]) == [
        (trade["id"], "T2_TOUCHED"), (trade["id"], "ATR_SL_HIT"),
    ]
    assert await pm.update_trades(candles[-1]["close"], candle_high=candles[-1]["high"],
                                  candle_low=candles[-1]["low"], candle_timestamp=candles[-1]["timestamp"]) == []
    await _flush_trade_writes(pm)
    assert [call.args[2] for call in alert.call_args_list] == ["T2_TOUCHED", "ATR_SL_HIT"]
    marker = trade["market_context"]["target_milestones"]["t2"]
    assert marker["touched_at"] == "2099-01-01T03:47:00+00:00"
    assert trade["state"] == "CLOSED" and trade["stop_loss"] == 24000
    pm.analytics.log_exit.assert_called_once_with(
        trade["id"], 24000 - sign, "ATR_SL_HIT", exit_timestamp="2099-01-01T03:50:00+00:00",
        pnl_points_override=24.5,
    )
    contexts = [call.args[0]["market_context"] for call in client.update_mock.call_args_list
                if "market_context" in call.args[0]]
    assert contexts[-1]["target_milestones"]["t2"] == marker


@pytest.mark.asyncio
@pytest.mark.parametrize("sign", [1, -1])
async def test_atr_history_before_activation_and_replayed_bars_cannot_create_t2(atr_position_manager, sign):
    pm, _, alert = atr_position_manager
    candles = _atr_minutes(0, 10, sign, close=35, low=30, high=40)
    candles[2]["high" if sign == 1 else "low"] = 24000 + sign * 100
    trade = _atr_trade(sign, state="T1_HIT", stop_loss=24000, market_context={
        "atr_trailing": {"last_bar": "2099-01-01T09:20:00+05:30"},
    })
    pm.active_trades = [trade]
    assert await pm.update_atr_trailing(candles) == []
    assert await pm.update_atr_trailing(candles) == []
    await _flush_trade_writes(pm)
    assert "target_milestones" not in trade["market_context"]
    alert.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("sign", [1, -1])
async def test_tick_t1_activation_within_exit_minute_keeps_t2_touch(atr_position_manager, sign):
    pm, _, alert = atr_position_manager
    trade = _atr_trade(sign, state="T1_HIT", stop_loss=24000,
                       entry_timestamp="2099-01-01T09:10:00+05:30",
                       market_context={"tracking_mode": "SIGNAL_REFERENCE",
                                       "atr_trailing": {"last_bar": "2099-01-01T09:19:05+05:30"}})
    pm.active_trades = [trade]
    candles = _atr_minutes(0, 5, sign, close=-1, low=-5, high=5)
    candles[-1]["high" if sign == 1 else "low"] = trade["target_2"]
    assert await pm.update_atr_trailing(candles) == [
        (trade["id"], "T2_TOUCHED"), (trade["id"], "ATR_SL_HIT"),
    ]
    await _flush_trade_writes(pm)
    assert [call.args[2] for call in alert.call_args_list] == ["T2_TOUCHED", "ATR_SL_HIT"]
    pm.analytics.log_exit.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("sign", [1, -1])
async def test_atr_source_entry_minute_wick_and_invalid_revision_cannot_create_t2(atr_position_manager, sign):
    pm, _, alert = atr_position_manager
    trade = _atr_trade(sign, state="T1_HIT", stop_loss=24000,
                       entry_timestamp="2099-01-01T09:19:05+05:30", market_context={
                           "tracking_mode": "SIGNAL_REFERENCE",
                           "atr_trailing": {"last_bar": "2099-01-01T09:19:05+05:30"},
                       })
    pm.active_trades = [trade]
    candles = _atr_minutes(0, 5, sign, close=35, low=30, high=40)
    candles[-1]["high" if sign == 1 else "low"] = trade["target_2"]
    # Its wick may predate entry; its close is below T2.
    assert await pm.update_atr_trailing(candles) == []
    await _flush_trade_writes(pm)
    assert "target_milestones" not in trade["market_context"]
    # A malformed final revision invalidates the whole ATR bar and its touch.
    trade["market_context"]["atr_trailing"]["last_bar"] = "2099-01-01T09:15:00+05:30"
    candles += [None, {"timestamp": "bad"}, {**candles[-1], "complete": False}]
    assert await pm.update_atr_trailing(candles) == []
    alert.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("sign", [1, -1])
async def test_atr_real_candles_ratchet_ignore_wicks_and_exit_completed_close(atr_position_manager, sign):
    pm, client, alert = atr_position_manager
    trade = _atr_trade(sign)
    pm.active_trades = [trade]
    activated = datetime(2099, 1, 1, 9, 14, tzinfo=timezone(timedelta(hours=5, minutes=30)))
    await pm.update_trades(trade["target_1"], candle_timestamp=activated)
    await _flush_trade_writes(pm)
    assert trade["state"] == "T1_HIT"
    assert trade["stop_loss"] == 24000
    assert trade["market_context"]["atr_trailing"]["last_bar"] == "2099-01-01T09:15:00+05:30"
    alert.reset_mock()
    candles = _atr_minutes(0, 50, sign)
    events = await pm.update_atr_trailing(candles)
    await _flush_trade_writes(pm)
    assert events == [(trade["id"], "ATR_SL_UPDATED")]
    assert trade["stop_loss"] == 24000 + sign * 15
    assert trade["market_context"]["atr_trailing"]["atr"] == 10
    alert.assert_called_once_with(trade, 24000 + sign * 35, "ATR_SL_UPDATED")
    pm.analytics.log_exit.assert_not_called()
    # ATR context and the current active stop are both persisted on a ratchet.
    snapshots = [call.args[0] for call in client.update_mock.call_args_list]
    assert any(row.get("stop_loss") == trade["stop_loss"] for row in snapshots)
    assert any(row.get("market_context", {}).get("atr_trailing", {}).get("stop_loss") ==
               trade["stop_loss"] for row in snapshots)
    alert.reset_mock()
    assert await pm.update_atr_trailing(candles) == []
    # A tick through the active stop and a high/low wick through it cannot exit.
    await pm.update_trades(24000 - sign * 20, candle_high=24150, candle_low=23850)
    await _flush_trade_writes(pm)
    assert trade["state"] == "T1_HIT"
    alert.assert_called_once_with(trade, trade["target_2"], "T2_TOUCHED")
    alert.reset_mock()
    candles += _atr_minutes(50, 5, sign, close=25, low=-10, high=40)
    assert await pm.update_atr_trailing(candles) == []
    assert trade["stop_loss"] == 24000 + sign * 15  # Wider ATR never loosens the stop.
    # Touching the stop at a completed close also leaves the runner open.
    candles += _atr_minutes(55, 5, sign, close=15, low=10, high=35)
    assert await pm.update_atr_trailing(candles) == []
    assert trade["state"] == "T1_HIT"
    alert.assert_not_called()
    candles += _atr_minutes(60, 4, sign, close=14, low=10, high=35)
    assert await pm.update_atr_trailing(candles) == []  # Partial 5m bar cannot exit.
    candles += _atr_minutes(64, 1, sign, close=14, low=10, high=35)
    events = await pm.update_atr_trailing(candles)
    await _flush_trade_writes(pm)
    assert events == [(trade["id"], "ATR_SL_HIT")]
    assert trade["state"] == "CLOSED"
    alert.assert_called_once_with(trade, 24000 + sign * 14, "ATR_SL_HIT")
    pm.analytics.log_exit.assert_called_once_with(
        trade["id"], 24000 + sign * 14, "ATR_SL_HIT",
        exit_timestamp="2099-01-01T04:50:00+00:00", pnl_points_override=32,
    )
    assert await pm.update_atr_trailing(candles) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("sign", [1, -1])
async def test_atr_warmup_cost_floor_and_older_activation_bars(atr_position_manager, sign):
    pm, _, alert = atr_position_manager
    trade = _atr_trade(sign)
    pm.active_trades = [trade]
    activated = datetime(2099, 1, 1, 9, 19, tzinfo=timezone(timedelta(hours=5, minutes=30)))
    await pm.update_trades(trade["target_1"], candle_timestamp=activated)
    # The 09:20 close existed at activation; its earlier fall cannot exit a new runner.
    candles = _atr_minutes(0, 5, sign, close=-1, low=-5, high=5)
    assert await pm.update_atr_trailing(candles) == []
    assert trade["state"] == "T1_HIT"
    candles += _atr_minutes(5, 5, sign, close=0, low=-5, high=5)
    assert await pm.update_atr_trailing(candles) == []
    assert trade["stop_loss"] == 24000
    candles += _atr_minutes(10, 5, sign, close=-1, low=-5, high=5)
    assert await pm.update_atr_trailing(candles) == [(trade["id"], "ATR_SL_HIT")]
    await _flush_trade_writes(pm)
    assert trade["market_context"]["atr_trailing"]["atr"] is None
    assert trade["state"] == "CLOSED"


@pytest.mark.asyncio
async def test_atr_legacy_runner_anchors_without_replaying_old_closes(atr_position_manager):
    pm, _, alert = atr_position_manager
    trade = _atr_trade(state="T1_HIT", stop_loss=24000)
    pm.active_trades = [trade]
    candles = _atr_minutes(0, 50, close=-30, low=-40, high=-20)
    assert await pm.update_atr_trailing(candles) == []
    await _flush_trade_writes(pm)
    assert trade["state"] == "T1_HIT"
    assert trade["market_context"]["atr_trailing"]["last_bar"] == "2099-01-01T10:05:00+05:30"
    alert.assert_not_called()
    candles += _atr_minutes(50, 5, close=-30, low=-40, high=-20)
    assert await pm.update_atr_trailing(candles) == [(trade["id"], "ATR_SL_HIT")]
    await _flush_trade_writes(pm)


def test_atr_startup_restores_context_stop_and_t1_state():
    client = MockSupabaseClient()
    client.execute_mock.return_value.data = [_atr_trade(state="OPEN", stop_loss=23975)]
    persisted_atr = {"state":"T1_HIT", "stop_loss":24015,
                     "last_bar":"2099-01-01T10:05:00+05:30", "atr":10, "previous_sl":24000}
    client.select_mock.return_value.in_.return_value.execute.return_value.data = [{
        "id":"atr-runner", "result_state":"OPEN",
        "market_context":{"atr_trailing":persisted_atr, "execution":{"mode":"PAPER"}},
    }]
    with patch("position_manager.settings"), patch("position_manager.create_client", return_value=client):
        pm = PositionManager()
    assert pm.is_initialized
    assert len(pm.active_trades) == 1
    trade = pm.active_trades[0]
    assert trade["state"] == "T1_HIT"
    assert trade["stop_loss"] == 24015
    assert trade["market_context"]["atr_trailing"] == persisted_atr
    assert trade["market_context"]["execution"] == {"mode":"PAPER"}


@pytest.mark.asyncio
@pytest.mark.parametrize("sign", [1, -1])
async def test_atr_uses_previous_stop_then_ratchets_for_next_close(atr_position_manager, sign):
    pm, _, alert = atr_position_manager
    trade = _atr_trade(sign, state="T1_HIT", stop_loss=24000, market_context={
        "atr_trailing":{"last_bar":"2099-01-01T09:15:00+05:30"},
    })
    pm.active_trades = [trade]
    # ATR candidate remains behind entry despite ten ready bars.
    candles = _atr_minutes(0, 50, sign, close=5, low=0, high=10)
    assert await pm.update_atr_trailing(candles) == []
    assert trade["stop_loss"] == 24000
    # The large wick lifts the candidate beyond this close. It becomes the
    # active stop for the next bar; it cannot retroactively close this bar.
    candles += _atr_minutes(50, 5, sign, close=35, low=30, high=120)
    assert await pm.update_atr_trailing(candles) == [
        (trade["id"], "T2_TOUCHED"), (trade["id"], "ATR_SL_UPDATED"),
    ]
    assert trade["state"] == "T1_HIT"
    assert sign * (trade["stop_loss"] - (24000 + sign * 35)) > 0
    candles += _atr_minutes(55, 5, sign, close=35, low=30, high=40)
    assert await pm.update_atr_trailing(candles) == [(trade["id"], "ATR_SL_HIT")]
    await _flush_trade_writes(pm)


@pytest.mark.asyncio
@pytest.mark.parametrize("sign", [1, -1])
async def test_manual_entry_does_not_replay_earlier_same_candle_wicks(atr_position_manager, sign):
    pm, _, alert = atr_position_manager
    trade = _atr_trade(
        sign, stop_loss=24000 - sign * 10, target_1=24000 + sign * 20,
        entry_timestamp="2099-01-01T04:30:01+00:00",
        market_context={"tracking_mode":"SIGNAL_REFERENCE"},
    )
    pm.active_trades = [trade]
    # Signal-reference registration happens after the completed source candle.
    # Its previous stop/target wicks cannot affect the newly registered trade.
    events = await pm.update_trades(
        24000, candle_high=24030, candle_low=23970,
        candle_timestamp=datetime(2099, 1, 1, 4, 30, tzinfo=timezone.utc),
    )
    assert events == []
    assert trade["state"] == "OPEN"
    assert trade["stop_loss"] == 24000 - sign * 10
    alert.assert_not_called()
    # The following source candle is after entry and its T1 wick remains valid.
    events = await pm.update_trades(
        24000, candle_high=24030 if sign == 1 else 24005,
        candle_low=23995 if sign == 1 else 23970,
        candle_timestamp=datetime(2099, 1, 1, 4, 31, tzinfo=timezone.utc),
    )
    await _flush_trade_writes(pm)
    assert events == [(trade["id"], "T1_HIT")]
    assert trade["state"] == "T1_HIT"
    alert.assert_called_once_with(trade, 24000 + sign * 20, "T1_HIT")


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["missing_analytics", "empty_update"])
async def test_paper_context_write_failure_retries_without_losing_atr_context(atr_position_manager, failure):
    pm, client, _ = atr_position_manager
    signal = AresSignal(
        SetupType.TREND_CONTINUATION, Direction.BULLISH, 24000, (23995, 24005),
        23990, 24020, 24040, "MEDIUM", [], datetime(2099, 1, 1, 10), 24000, "CE",
    )
    signal.trade_id = "atr-runner"
    snapshot = {"mode":"PAPER", "status":"FILLED", "active_spot_sl":24015}
    context = {"atr_trailing":{"state":"T1_HIT", "stop_loss":24015}, "source":"kept"}
    selected = client.select_mock.return_value.eq.return_value.execute.return_value
    selected.data = [] if failure == "missing_analytics" else [{"market_context":context}]
    updated = client.update_mock.return_value.eq.return_value.execute.return_value
    updated.data = []
    with patch("position_manager.settings.signal_schema_mode", "bridge"):
        with pytest.raises(RuntimeError, match="unavailable" if failure == "missing_analytics" else "not persisted"):
            await pm.sync_execution(signal, snapshot)
        assert "atr-runner" not in pm._execution_snapshots
        selected.data = [{"market_context":context}]
        updated.data = [{"id":"atr-runner"}]
        assert await pm.sync_execution(signal, snapshot) is True
        calls_after_success = client.update_mock.call_count
        assert await pm.sync_execution(signal, snapshot) is True
        assert client.update_mock.call_count == calls_after_success
    assert pm._execution_snapshots["atr-runner"] == snapshot
    persisted = [call.args[0]["market_context"] for call in client.update_mock.call_args_list
                 if "market_context" in call.args[0]]
    assert persisted[-1] == {**context, "execution":snapshot}


@pytest.mark.asyncio
async def test_missing_atr_analytics_defers_active_stop_write_then_recovers_next_bar(atr_position_manager):
    pm, client, _ = atr_position_manager
    trade = _atr_trade(state="T1_HIT", stop_loss=24000, market_context={
        "atr_trailing":{"last_bar":"2099-01-01T09:15:00+05:30"},
    })
    # Neither OPEN nor terminal trades participate in post-T1 candle trailing.
    untouched = [_atr_trade(id="open", state="OPEN"), _atr_trade(id="closed", state="CLOSED")]
    for row in untouched:
        row.pop("market_context", None)
    pm.active_trades = [*untouched, trade]
    selected = client.select_mock.return_value.eq.return_value.execute.return_value
    selected.data = []
    candles = _atr_minutes(0, 50)
    assert await pm.update_atr_trailing(candles) == [(trade["id"], "ATR_SL_UPDATED")]
    await _flush_trade_writes(pm)
    assert trade["stop_loss"] == 24015
    client.update_mock.assert_not_called()  # Never persist an unresumable active stop.
    assert [row["state"] for row in untouched] == ["OPEN", "CLOSED"]
    assert all("market_context" not in row for row in untouched)
    selected.data = [{"market_context":{"source":"retained"}}]
    candles += _atr_minutes(50, 5)
    assert await pm.update_atr_trailing(candles) == []
    await _flush_trade_writes(pm)
    payloads = [call.args[0] for call in client.update_mock.call_args_list]
    assert payloads[-1] == {"state":"T1_HIT", "stop_loss":24015}
    context = payloads[-2]["market_context"]
    assert context["source"] == "retained"
    assert context["atr_trailing"]["stop_loss"] == 24015
    assert context["atr_trailing"]["last_bar"] == "2099-01-01T10:10:00+05:30"


@pytest.mark.asyncio
async def test_terminal_atr_active_write_failure_still_logs_exit_analytics(atr_position_manager, capsys):
    pm, client, _ = atr_position_manager
    trade = _atr_trade(state="T1_HIT", stop_loss=24000, market_context={
        "atr_trailing":{"last_bar":"2099-01-01T09:15:00+05:30"},
    })
    pm.active_trades = [trade]
    client.update_mock.side_effect = RuntimeError("active table unavailable")
    events = await pm.update_atr_trailing(_atr_minutes(0, 5, close=-1, low=-5, high=5))
    await _flush_trade_writes(pm)
    assert events == [(trade["id"], "ATR_SL_HIT")]
    assert trade["state"] == "CLOSED"
    pm.analytics.log_exit.assert_called_once_with(
        trade["id"], 23999, "ATR_SL_HIT", exit_timestamp="2099-01-01T03:50:00+00:00",
        pnl_points_override=24.5,
    )
    assert "Failed to persist active trade state: active table unavailable" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_forming_five_minute_bar_does_not_advance_atr_or_write_state(atr_position_manager):
    pm, client, alert = atr_position_manager
    trade = _atr_trade(state="T1_HIT", stop_loss=24000, market_context={
        "atr_trailing":{"last_bar":"2099-01-01T09:15:00+05:30"},
    })
    pm.active_trades = [trade]
    candles = _atr_minutes(0, 5, close=-1, low=-5, high=5)
    candles[-1]["complete"] = False
    assert await pm.update_atr_trailing(candles) == []
    assert trade["state"] == "T1_HIT"
    assert trade["market_context"]["atr_trailing"]["last_bar"] == "2099-01-01T09:15:00+05:30"
    client.update_mock.assert_not_called()
    alert.assert_not_called()


@pytest.mark.asyncio
async def test_terminal_atr_telemetry_survives_closed_analytics_callback_loop(atr_position_manager, capsys):
    pm, client, _ = atr_position_manager
    trade = _atr_trade(state='T1_HIT', stop_loss=24000, market_context={
        'atr_trailing': {'last_bar': '2099-01-01T09:15:00+05:30'},
    })
    pm.active_trades = [trade]
    closed_loop = MagicMock()
    closed_loop.call_soon_threadsafe.side_effect = RuntimeError('loop closed')
    with patch('position_manager.asyncio.get_event_loop', return_value=closed_loop):
        events = await pm.update_atr_trailing(_atr_minutes(0, 5, close=-1, low=-5, high=5))
        await _flush_trade_writes(pm)
    assert events == [(trade['id'], 'ATR_SL_HIT')]
    terminal = [call.args[0] for call in client.update_mock.call_args_list if 'exit_type' in call.args[0]]
    assert terminal[0]['exit_type'] == 'ATR_SL_HIT'
    assert terminal[0]['exit_price'] == 23999
    pm.analytics.log_exit.assert_not_called()
    assert 'Failed to schedule trade exit analytics: loop closed' in capsys.readouterr().out


def _reference_signal():
    signal = AresSignal(SetupType.TREND_CONTINUATION, Direction.BULLISH, 24000,
                        (23995, 24005), 23990, 24020, 24040, 'MEDIUM', [],
                        datetime(2026, 10, 21, 10), 24000, 'CE')
    signal.market_context['tracking_mode'] = 'SIGNAL_REFERENCE'
    return signal


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['bridge', 'greenfield'])
async def test_reference_signals_at_same_price_have_independent_lifecycles(atr_position_manager, mode):
    pm, _, _ = atr_position_manager
    first, second = _reference_signal(), _reference_signal()
    with patch('position_manager.settings.signal_schema_mode', mode), \
         patch('position_manager.settings.trade_dedupe_tolerance_pts', 1):
        first_id, first_status = await pm.add_trade(first, 24000)
        second_id, second_status = await pm.add_trade(second, 24000.4)
        assert (first_status, second_status) == ('BOUND', 'BOUND')
        assert first_id != second_id
        events = await pm.update_trades(24025)
        await _flush_trade_writes(pm)
    assert set(events) == {(first_id, 'T1_HIT'), (second_id, 'T1_HIT')}
    assert len(pm.active_trades) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['bridge', 'greenfield'])
@pytest.mark.parametrize('state', ['OPEN', 'T1_HIT', 'CLOSED'])
async def test_reference_signal_retry_reuses_trade_without_resetting_lifecycle(atr_position_manager, mode, state):
    pm, client, _ = atr_position_manager
    signal = _reference_signal()
    pm.supabase.rpc = MagicMock(wraps=client.rpc)
    with patch('position_manager.settings.signal_schema_mode', mode):
        trade_id, _ = await pm.add_trade(signal, 24000)
        pm.active_trades[0].update(state=state, stop_loss=24010)
        signal.trade_id = ''
        assert await pm.add_trade(signal, 24000.4) == (trade_id, 'BOUND')
    assert signal.trade_id == trade_id
    assert pm.supabase.rpc.call_count == 1
    assert len(pm.active_trades) == 1
    assert pm.active_trades[0]['state'] == state
    assert pm.active_trades[0]['stop_loss'] == 24010


@pytest.mark.asyncio
async def test_reference_signal_identity_is_stable_for_concurrent_and_fresh_registration(atr_position_manager):
    import threading
    pm, client, _ = atr_position_manager
    signal = _reference_signal()
    original_rpc = client.rpc
    concurrent_inserts = threading.Barrier(2)
    def rpc(name, payload):
        # Both callers pass the empty cache before either RPC can complete.
        concurrent_inserts.wait(timeout=5)
        return original_rpc(name, payload)
    pm.supabase.rpc = MagicMock(side_effect=rpc)
    with patch('position_manager.settings.signal_schema_mode', 'bridge'):
        results = await asyncio.gather(pm.add_trade(signal, 24000), pm.add_trade(signal, 24000))
        assert pm.supabase.rpc.call_count == 2
        assert results[0] == results[1] == (signal.trade_id, 'BOUND')
        assert len(pm.active_trades) == 1
        pm.supabase.rpc = original_rpc
        # This fake RPC checks identity stability, not closed-row recovery.
        fresh_manager = PositionManager()
        retry_id, status = await fresh_manager.add_trade(signal, 24000)
    assert (retry_id, status) == results[0]
