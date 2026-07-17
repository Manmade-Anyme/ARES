import unittest
from unittest.mock import MagicMock

from ml_signal.data import load_intraday_candles_from_dhan


def _ok_response():
    return {
        "status": "success",
        "data": {
            "start_Time": [1752556500, 1752556560],
            "open": [24100.0, 24105.0],
            "high": [24110.0, 24115.0],
            "low": [24095.0, 24100.0],
            "close": [24105.0, 24112.0],
            "volume": [1000, 2000],
        },
    }


class TestLoadIntradayCandlesFromDhan(unittest.TestCase):
    """
    Regression cover for the DH-905 bug: the loader passed `security_id` in the
    `instrument_type` slot, so every live call failed with
    Input_Exception (DH-905) and the Kronos consumer could never score a signal.
    The TASK-186 tests mocked this function outright, so nothing caught it.
    """

    def test_passes_instrument_type_not_security_id(self):
        dhan = MagicMock()
        dhan.intraday_minute_data.return_value = _ok_response()

        load_intraday_candles_from_dhan(
            dhan,
            security_id="13",
            exchange_segment="IDX_I",
            instrument_type="INDEX",
            from_date="2026-07-15",
            to_date="2026-07-15",
        )

        kwargs = dhan.intraday_minute_data.call_args.kwargs
        self.assertEqual(kwargs["instrument_type"], "INDEX")
        self.assertEqual(kwargs["security_id"], "13")
        self.assertEqual(kwargs["exchange_segment"], "IDX_I")
        self.assertEqual(kwargs["from_date"], "2026-07-15")
        self.assertEqual(kwargs["to_date"], "2026-07-15")

    def test_passes_multiday_window_through(self):
        """TASK-187: callers need a multi-day window for a usable model context;
        the loader must forward both bounds rather than collapse them to one day."""
        dhan = MagicMock()
        dhan.intraday_minute_data.return_value = _ok_response()

        load_intraday_candles_from_dhan(
            dhan, "13", "IDX_I", "INDEX", "2026-07-07", "2026-07-17",
        )

        kwargs = dhan.intraday_minute_data.call_args.kwargs
        self.assertEqual(kwargs["from_date"], "2026-07-07")
        self.assertEqual(kwargs["to_date"], "2026-07-17")

    def test_returns_ohlcv_frame(self):
        dhan = MagicMock()
        dhan.intraday_minute_data.return_value = _ok_response()

        df = load_intraday_candles_from_dhan(
            dhan, "13", "IDX_I", "INDEX", "2026-07-15", "2026-07-15",
        )

        self.assertEqual(list(df.columns), ["timestamp", "open", "high", "low", "close", "volume"])
        self.assertEqual(len(df), 2)
        self.assertEqual(df["close"].iloc[-1], 24112.0)

    def test_raises_on_api_failure(self):
        dhan = MagicMock()
        dhan.intraday_minute_data.return_value = {
            "status": "failure",
            "remarks": {"error_code": "DH-905"},
        }

        with self.assertRaises(ValueError):
            load_intraday_candles_from_dhan(dhan, "13", "IDX_I", "INDEX", "2026-07-15", "2026-07-15")


if __name__ == "__main__":
    unittest.main()
