import unittest
from unittest.mock import patch, MagicMock
from datetime import datetime
from collections import deque

from ml_signal.collector import MLCollector
from ml_signal.config import MLConfig


class TestMLCollector(unittest.TestCase):

    def setUp(self):
        self.config = MLConfig()
        self.url = "https://mock.supabase.co"
        self.key = "mock-key"

    def _make_mock_candle(self):
        candle = MagicMock()
        candle.open = 24100.0
        candle.high = 24150.0
        candle.low = 24080.0
        candle.close = 24120.0
        candle.volume = 150000
        candle.vwap = 24110.0
        candle.timestamp = datetime.now()
        return candle

    def _make_mock_option_row(self, iv=15.0, oi=500000, oi_change=2.5, gamma=0.05, theta=-0.8, vega=0.3):
        row = MagicMock()
        row.iv = iv
        row.oi = oi
        row.oi_change_pct = oi_change
        row.gamma = gamma
        row.theta = theta
        row.vega = vega
        return row

    def _make_mock_atm(self, spot=24120.0):
        atm = MagicMock()
        atm.spot_price = spot
        atm.ce = self._make_mock_option_row(iv=15.0, oi=600000)
        atm.pe = self._make_mock_option_row(iv=16.5, oi=800000)
        return atm

    def _make_mock_levels(self):
        lvl1 = MagicMock()
        lvl1.price = 24200.0
        lvl2 = MagicMock()
        lvl2.price = 24000.0
        return [lvl1, lvl2]

    @patch("ml_signal.collector.create_client")
    def test_check_table_exists_true(self, mock_create_client):
        mock_supabase = MagicMock()
        mock_supabase.table().select().limit().execute.return_value = MagicMock(data=[])
        mock_create_client.return_value = mock_supabase

        collector = MLCollector(self.url, self.key, self.config)
        result = collector.check_table_exists()
        self.assertTrue(result)

    @patch("ml_signal.collector.create_client")
    def test_check_table_exists_false(self, mock_create_client):
        mock_supabase = MagicMock()
        mock_supabase.table().select().limit().execute.side_effect = Exception("relation does not exist")
        mock_create_client.return_value = mock_supabase

        collector = MLCollector(self.url, self.key, self.config)
        result = collector.check_table_exists()
        self.assertFalse(result)

    @patch("ml_signal.collector.create_client")
    def test_snapshot_basic(self, mock_create_client):
        mock_supabase = MagicMock()
        mock_supabase.table().select().limit().execute.return_value = MagicMock(data=[])
        mock_create_client.return_value = mock_supabase

        collector = MLCollector(self.url, self.key, self.config)
        candle = self._make_mock_candle()
        atm = self._make_mock_atm()
        levels = self._make_mock_levels()

        collector.snapshot(
            candle=candle,
            atm=atm,
            full_chain=[],
            levels=levels,
            spot=24120.0,
            signal=None,
            pdh=24200.0,
            pdl=24000.0,
            is_expiry=False,
        )

        stats = collector.stats
        self.assertEqual(stats["total_snapshots"], 1)
        self.assertEqual(stats["signals_recorded"], 0)

    @patch("ml_signal.collector.create_client")
    def test_snapshot_with_signal(self, mock_create_client):
        mock_supabase = MagicMock()
        mock_supabase.table().select().limit().execute.return_value = MagicMock(data=[])
        mock_create_client.return_value = mock_supabase

        collector = MLCollector(self.url, self.key, self.config)
        candle = self._make_mock_candle()
        atm = self._make_mock_atm()

        mock_signal = MagicMock()
        mock_signal.signal_id = "1234"
        mock_signal.setup_type = "FAILED_BREAKOUT"
        mock_signal.direction = "BEARISH"
        mock_signal.confidence = "HIGH"

        collector.snapshot(
            candle=candle,
            atm=atm,
            full_chain=[],
            levels=[],
            spot=24120.0,
            signal=mock_signal,
            pdh=None,
            pdl=None,
            is_expiry=False,
        )

        stats = collector.stats
        self.assertEqual(stats["total_snapshots"], 1)
        self.assertEqual(stats["signals_recorded"], 1)

    @patch("ml_signal.collector.create_client")
    def test_snapshot_insert_called(self, mock_create_client):
        mock_supabase = MagicMock()
        table_mock = MagicMock()
        mock_supabase.table.return_value = table_mock
        mock_create_client.return_value = mock_supabase

        collector = MLCollector(self.url, self.key, self.config)
        candle = self._make_mock_candle()
        atm = self._make_mock_atm()

        collector.snapshot(
            candle=candle,
            atm=atm,
            full_chain=[],
            levels=[],
            spot=24120.0,
            signal=None,
            pdh=None,
            pdl=None,
            is_expiry=False,
        )

        mock_supabase.table.assert_called_once_with("ml_collection")
        self.assertTrue(table_mock.insert.called)

    @patch("ml_signal.collector.create_client")
    def test_volume_history_maintained(self, mock_create_client):
        mock_supabase = MagicMock()
        mock_supabase.table.return_value = MagicMock()
        mock_create_client.return_value = mock_supabase

        collector = MLCollector(self.url, self.key, self.config)
        candle = self._make_mock_candle()
        atm = self._make_mock_atm()

        for _ in range(3):
            collector.snapshot(
                candle=candle,
                atm=atm,
                full_chain=[],
                levels=[],
                spot=24120.0,
                signal=None,
                pdh=None,
                pdl=None,
                is_expiry=False,
            )

        self.assertEqual(collector.stats["volume_history_size"], 3)

    @patch("ml_signal.collector.create_client")
    def test_candle_to_dict(self, mock_create_client):
        mock_supabase = MagicMock()
        mock_create_client.return_value = mock_supabase

        collector = MLCollector(self.url, self.key, self.config)
        candle = self._make_mock_candle()
        result = collector._candle_to_dict(candle)

        self.assertEqual(result["open"], 24100.0)
        self.assertEqual(result["high"], 24150.0)
        self.assertEqual(result["low"], 24080.0)
        self.assertEqual(result["close"], 24120.0)
        self.assertEqual(result["volume"], 150000)
        self.assertEqual(result["vwap"], 24110.0)

    @patch("ml_signal.collector.create_client")
    def test_levels_to_prices(self, mock_create_client):
        mock_supabase = MagicMock()
        mock_create_client.return_value = mock_supabase

        collector = MLCollector(self.url, self.key, self.config)
        levels = self._make_mock_levels()
        prices = collector._levels_to_prices(levels)

        self.assertEqual(prices, [24200.0, 24000.0])

    @patch("ml_signal.collector.create_client")
    def test_empty_levels(self, mock_create_client):
        mock_supabase = MagicMock()
        mock_create_client.return_value = mock_supabase

        collector = MLCollector(self.url, self.key, self.config)
        prices = collector._levels_to_prices([])
        self.assertEqual(prices, [])

    @patch("ml_signal.collector.create_client")
    def test_stats_initial(self, mock_create_client):
        mock_supabase = MagicMock()
        mock_create_client.return_value = mock_supabase

        collector = MLCollector(self.url, self.key, self.config)
        stats = collector.stats

        self.assertEqual(stats["total_snapshots"], 0)
        self.assertEqual(stats["signals_recorded"], 0)
        self.assertEqual(stats["volume_history_size"], 0)
        self.assertEqual(stats["iv_history_size"], 0)

    @patch("ml_signal.collector.create_client")
    def test_snapshot_insert_error_does_not_raise(self, mock_create_client):
        mock_supabase = MagicMock()
        table_mock = MagicMock()
        table_mock.insert.return_value = table_mock
        table_mock.execute.side_effect = Exception("DB error")
        mock_supabase.table.return_value = table_mock
        mock_create_client.return_value = mock_supabase

        collector = MLCollector(self.url, self.key, self.config)
        candle = self._make_mock_candle()
        atm = self._make_mock_atm()

        try:
            collector.snapshot(
                candle=candle,
                atm=atm,
                full_chain=[],
                levels=[],
                spot=24120.0,
                signal=None,
                pdh=None,
                pdl=None,
                is_expiry=False,
            )
        except Exception:
            self.fail("snapshot() raised an exception on insert failure")

        self.assertEqual(collector.stats["total_snapshots"], 1)

    @patch("ml_signal.collector.create_client")
    def test_candle_to_dict_partial_data(self, mock_create_client):
        mock_supabase = MagicMock()
        mock_create_client.return_value = mock_supabase

        collector = MLCollector(self.url, self.key, self.config)

        candle = MagicMock()
        candle.open = 24100.0
        candle.high = 24150.0
        candle.low = 24080.0
        del candle.close
        del candle.volume
        del candle.vwap

        result = collector._candle_to_dict(candle)
        self.assertEqual(result["close"], 0)
        self.assertEqual(result["volume"], 0)


if __name__ == '__main__':
    unittest.main()
