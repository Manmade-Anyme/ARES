import unittest
from unittest.mock import MagicMock, patch
import asyncio
import sys
import importlib
from datetime import datetime
from models import AresSignal, SetupType, Direction

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
        
        # Mock selects
        self.select_mock = MagicMock()
        self.table_mock.select = self.select_mock
        self.eq_mock = MagicMock()
        self.select_mock.return_value.eq = self.eq_mock
        self.execute_mock = MagicMock()
        self.eq_mock.return_value.execute = self.execute_mock
        self.execute_mock.return_value.data = []

    def table(self, name):
        self.last_table = name
        return self.table_mock

# Instantiate global mock client and patch during import/reload of storage
global_mock_client = MockSupabaseClient()
mock_create_patch = patch('supabase.create_client', return_value=global_mock_client)
mock_create_patch.start()

# Reload storage module to force rebinding of create_client
if 'storage' in sys.modules:
    importlib.reload(sys.modules['storage'])
else:
    import storage

from storage import Storage, AnalyticsLogger, load_dhan_credentials_from_supabase

class TestStorage(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        self.mock_client = global_mock_client
        self.mock_client.insert_mock.reset_mock()
        self.mock_client.update_mock.reset_mock()
        self.mock_client.select_mock.reset_mock()
        self.mock_client.insert_mock.side_effect = None
        self.mock_client.update_mock.side_effect = None
        self.mock_client.select_mock.side_effect = None
        self.mock_client.execute_mock.side_effect = None
        self.mock_client.execute_mock.return_value.data = []
        self.mock_client.last_table = None

        self.storage = Storage()
        self.analytics = AnalyticsLogger()

    @patch('config.settings')
    async def test_log_signal_success(self, mock_settings):
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
        signal.suggested_lots = 2
        signal.capital = 10000.0
        signal.risk_pct = 1.5
        signal.option_sl = 10.0
        signal.option_target = 20.0
        signal.option_premium = 15.0
        signal.option_delta = 0.5

        await self.storage.log_signal(signal, 24001.0)
        self.assertEqual(self.mock_client.last_table, "ares_signals")
        self.mock_client.insert_mock.assert_called_once()
        inserted_data = self.mock_client.insert_mock.call_args[0][0]
        self.assertEqual(inserted_data["setup_type"], "OI_WALL_REJECTION")
        self.assertEqual(inserted_data["spot_at_signal"], 24001.0)
        self.assertTrue(any("Option Sizing: 2 lots" in r for r in inserted_data["reasons"]))

    @patch('config.settings')
    async def test_log_signal_exception_safety(self, mock_settings):
        self.mock_client.insert_mock.side_effect = Exception("Supabase down")
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
        await self.storage.log_signal(signal, 24001.0)
        self.mock_client.insert_mock.assert_called_once()

    @patch('config.settings')
    async def test_log_entry_variations(self, mock_settings):
        atm_mock = MagicMock()
        atm_mock.ce.oi = 1000000
        atm_mock.pe.oi = 500000
        atm_mock.ce.oi_change_pct = 5.0
        atm_mock.pe.oi_change_pct = 10.0

        signal = AresSignal(
            setup_type=SetupType.OI_WALL_REJECTION,
            direction=Direction.BEARISH,
            trigger_price=24000.0,
            entry_zone=(23990.0, 24010.0),
            stop_loss=24025.0,
            target_1=23950.0,
            target_2=23900.0,
            confidence="HIGH",
            reasons=["Reason 1"],
            timestamp=datetime.now(),
            strike_to_trade=24000,
            option_type="PE"
        )
        signal.suggested_lots = 2
        signal.capital = 10000.0
        signal.risk_pct = 1.5
        signal.option_sl = 10.0
        signal.option_target = 20.0
        signal.option_premium = 15.0
        signal.option_delta = -0.5

        self.analytics.log_entry("trade-123", signal, 24001.0, atm_mock)
        await asyncio.sleep(0.05)
        
        self.assertEqual(self.mock_client.last_table, "trade_analytics")
        self.mock_client.insert_mock.assert_called_once()
        inserted_data = self.mock_client.insert_mock.call_args[0][0]
        self.assertEqual(inserted_data["id"], "trade-123")
        self.assertEqual(inserted_data["oi_data"]["pcr"], 0.5)
        self.assertEqual(inserted_data["market_context"]["options_sizing"]["suggested_lots"], 2)

    @patch('config.settings')
    async def test_log_entry_exception_safety(self, mock_settings):
        atm_mock = MagicMock()
        atm_mock.ce.oi = 0
        del atm_mock.ce
        
        signal = AresSignal(
            setup_type=SetupType.OI_WALL_REJECTION,
            direction=Direction.BEARISH,
            trigger_price=24000.0,
            entry_zone=(23990.0, 24010.0),
            stop_loss=24025.0,
            target_1=23950.0,
            target_2=23900.0,
            confidence="HIGH",
            reasons=["Reason 1"],
            timestamp=datetime.now(),
            strike_to_trade=24000,
            option_type="PE"
        )

        self.analytics.log_entry("trade-456", signal, 24001.0, atm_mock)
        await asyncio.sleep(0.05)
        self.mock_client.insert_mock.assert_called_once()

    @patch('config.settings')
    @patch('asyncio.get_running_loop')
    async def test_log_entry_exception_pathway(self, mock_get_loop, mock_settings):
        mock_get_loop.side_effect = RuntimeError("No event loop")
        signal = AresSignal(
            setup_type=SetupType.OI_WALL_REJECTION,
            direction=Direction.BEARISH,
            trigger_price=24000.0,
            entry_zone=(23990.0, 24010.0),
            stop_loss=24025.0,
            target_1=23950.0,
            target_2=23900.0,
            confidence="HIGH",
            reasons=["Reason 1"],
            timestamp=datetime.now(),
            strike_to_trade=24000,
            option_type="PE"
        )
        self.analytics.log_entry("trade-exception", signal, 24001.0, None)
        await asyncio.sleep(0.05)

    @patch('config.settings')
    async def test_log_exit_bullish_and_bearish(self, mock_settings):
        self.mock_client.execute_mock.return_value.data = [{"entry_price": 24000.0, "direction": "BULLISH"}]
        self.analytics.log_exit("trade-123", 24050.0, "T1_HIT")
        await asyncio.sleep(0.05)
        
        self.mock_client.update_mock.assert_called_once()
        update_data = self.mock_client.update_mock.call_args[0][0]
        self.assertEqual(update_data["pnl_points"], 50.0)
        self.assertEqual(update_data["result_state"], "T1_HIT")

        self.mock_client.update_mock.reset_mock()
        self.mock_client.execute_mock.return_value.data = [{"entry_price": 24000.0, "direction": "BEARISH"}]
        self.analytics.log_exit("trade-456", 23920.0, "T2_HIT")
        await asyncio.sleep(0.05)
        
        self.mock_client.update_mock.assert_called_once()
        update_data_bearish = self.mock_client.update_mock.call_args[0][0]
        self.assertEqual(update_data_bearish["pnl_points"], 80.0)
        self.assertEqual(update_data_bearish["result_state"], "T2_HIT")

    @patch('config.settings')
    async def test_log_exit_no_record_found(self, mock_settings):
        self.mock_client.execute_mock.return_value.data = []
        self.analytics.log_exit("trade-none", 24000.0, "SL_HIT")
        await asyncio.sleep(0.05)
        self.mock_client.update_mock.assert_not_called()

    @patch('config.settings')
    @patch('asyncio.get_running_loop')
    async def test_log_exit_exception_pathway(self, mock_get_loop, mock_settings):
        mock_get_loop.side_effect = RuntimeError("No event loop")
        self.analytics.log_exit("trade-exception", 24000.0, "SL_HIT")
        await asyncio.sleep(0.05)
        self.mock_client.update_mock.assert_not_called()

    @patch('supabase.create_client')
    @patch('config.settings')
    def test_load_dhan_credentials_success(self, mock_settings, mock_create_client):
        mock_create_client.return_value = self.mock_client
        self.mock_client.execute_mock.return_value.data = [{"client_id": "mock_id", "access_token": "mock_token"}]

        load_dhan_credentials_from_supabase()
        self.assertEqual(mock_settings.dhan_client_id, "mock_id")
        self.assertEqual(mock_settings.dhan_access_token, "mock_token")

    @patch('supabase.create_client')
    @patch('config.settings')
    def test_load_dhan_credentials_empty(self, mock_settings, mock_create_client):
        mock_create_client.return_value = self.mock_client
        self.mock_client.execute_mock.return_value.data = []

        with self.assertRaises(ValueError):
            load_dhan_credentials_from_supabase()

class TestSignalIdAndTimezones(unittest.IsolatedAsyncioTestCase):
    """TASK-172 audit P1 item 13: signal_id join fix and entry/exit timestamp
    timezone consistency."""

    def setUp(self):
        self.mock_client = global_mock_client
        self.mock_client.insert_mock.reset_mock()
        self.mock_client.update_mock.reset_mock()
        self.mock_client.insert_mock.side_effect = None
        self.mock_client.execute_mock.side_effect = None
        self.mock_client.execute_mock.return_value.data = []
        self.mock_client.insert_mock.return_value.execute.return_value = MagicMock(data=[])

        self.storage = Storage()
        self.analytics = AnalyticsLogger()

    def _make_signal(self, timestamp=None):
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
            timestamp=timestamp or datetime.now(),
            strike_to_trade=24000,
            option_type="CE",
        )

    async def test_log_signal_converts_naive_ist_timestamp_to_utc(self):
        """Candle timestamps are naive IST wall-clock; they must be stored as
        the correct UTC instant (14:12 IST == 08:42 UTC), matching the real-UTC
        exit timestamps."""
        signal = self._make_signal(timestamp=datetime(2026, 7, 2, 14, 12, 0))
        await self.storage.log_signal(signal, 24001.0)

        inserted = self.mock_client.insert_mock.call_args[0][0]
        self.assertEqual(inserted["timestamp"], "2026-07-02T08:42:00+00:00")

    async def test_log_signal_captures_inserted_db_id(self):
        self.mock_client.insert_mock.return_value.execute.return_value = MagicMock(
            data=[{"id": 4242}]
        )
        signal = self._make_signal()
        await self.storage.log_signal(signal, 24001.0)
        self.assertEqual(signal.db_id, 4242)

    async def test_log_entry_writes_signal_id_and_utc_entry_timestamp(self):
        signal = self._make_signal(timestamp=datetime(2026, 7, 2, 14, 12, 0))
        signal.db_id = 4242

        self.analytics.log_entry("trade-join", signal, 24001.0, None)
        await asyncio.sleep(0.05)

        inserted = self.mock_client.insert_mock.call_args[0][0]
        self.assertEqual(inserted["signal_id"], 4242)
        self.assertEqual(inserted["entry_timestamp"], "2026-07-02T08:42:00+00:00")

    async def test_log_entry_signal_id_null_when_signal_insert_failed(self):
        """If ares_signals logging failed, analytics still logs with a NULL
        signal_id instead of crashing."""
        signal = self._make_signal()
        self.analytics.log_entry("trade-nojoin", signal, 24001.0, None)
        await asyncio.sleep(0.05)

        inserted = self.mock_client.insert_mock.call_args[0][0]
        self.assertIsNone(inserted["signal_id"])

    async def test_aware_timestamps_pass_through_unchanged(self):
        """Already-aware timestamps are only converted to UTC, never re-labeled."""
        from datetime import timezone as tz
        aware = datetime(2026, 7, 2, 8, 42, 0, tzinfo=tz.utc)
        signal = self._make_signal(timestamp=aware)
        await self.storage.log_signal(signal, 24001.0)

        inserted = self.mock_client.insert_mock.call_args[0][0]
        self.assertEqual(inserted["timestamp"], "2026-07-02T08:42:00+00:00")

    def test_to_utc_iso_string_inputs(self):
        """to_utc_iso handles naive strings, Z strings, and offset strings correctly."""
        from storage import to_utc_iso
        # Naive IST string
        self.assertEqual(to_utc_iso("2026-07-02T14:12:00"), "2026-07-02T08:42:00+00:00")
        # Trailing Z string
        self.assertEqual(to_utc_iso("2026-07-02T08:42:00Z"), "2026-07-02T08:42:00+00:00")
        # Explicit offset string
        self.assertEqual(to_utc_iso("2026-07-02T14:12:00+05:30"), "2026-07-02T08:42:00+00:00")



# Clean up patch after class execution
def tearDownModule():
    mock_create_patch.stop()
    # Reload storage again to restore default behaviour if needed
    if 'storage' in sys.modules:
        importlib.reload(sys.modules['storage'])
