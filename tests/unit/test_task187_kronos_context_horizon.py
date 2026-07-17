import unittest
from unittest.mock import MagicMock, patch

import pandas as pd

from ml_signal.config import MLConfig
from ml_signal.kronos_consumer import (
    KRONOS_CONTEXT_CANDLES,
    KRONOS_CONTEXT_LOOKBACK_DAYS,
    KRONOS_HORIZON_CANDLES,
    KRONOS_MIN_CONTEXT_CANDLES,
    DEFAULT_CONFIG,
    KronosConsumer,
)


def _candles(start: str, periods: int) -> pd.DataFrame:
    """Naive-IST 1-min candles with a little movement so nothing degenerates."""
    ts = pd.date_range(start, periods=periods, freq="1min")
    close = [24100.0 + (i % 7) for i in range(periods)]
    return pd.DataFrame({
        "timestamp": ts,
        "open": close, "high": [c + 5 for c in close],
        "low": [c - 5 for c in close], "close": close,
        "volume": 1000,
    })


def _signal(ts_utc: str = "2026-07-17T03:50:00+00:00") -> dict:
    # 03:50 UTC == 09:20 IST — the starved-morning case that produced 0.0% live.
    return {
        "id": "227", "setup_type": "FAILED_BREAKOUT",
        "timestamp": ts_utc,
        "trigger_price": 24192.25, "target_1": 24222.25,
        "stop_loss": 24177.25, "direction": "BULLISH",
    }


class TestTask187ContextAndHorizon(unittest.TestCase):
    """
    Live regression (2026-07-17 signal #227): Kronos posted P=0.0% on a signal
    that hit T1 18 minutes later. Root cause was context starvation — the
    consumer fetched only the signal's own day, so a 09:20 signal got ~6
    candles. Kronos normalizes its context window, and 6 near-identical bars
    collapse the price scale, so every sampled path comes out flat and no path
    can ever reach T1 => a structural 0%, not a real forecast.

    Secondary: the barrier walked only 5 forecast candles while trades live
    ~45 minutes (time_stop_minutes), so even a healthy context was scored over
    the wrong window.

    These tests pin the two inputs that were wrong. They deliberately assert on
    what is handed to Kronos rather than on the probability itself — the
    TASK-186 suite mocked this boundary away entirely, which is exactly why
    both this bug and the earlier DH-905 bug shipped green.
    """

    @patch("ml_signal.kronos_consumer.load_intraday_candles_from_dhan")
    def test_forecast_requests_multiday_context_window(self, mock_load):
        mock_load.return_value = _candles("2026-07-17 09:15", 6)
        consumer = KronosConsumer(DEFAULT_CONFIG)

        fake_paths = MagicMock()
        fake_paths.predict_paths.return_value = [pd.DataFrame({"close": [24200.0]})]
        with patch.dict("sys.modules", {"paths": fake_paths}):
            consumer._forecast_probability(_signal())

        kwargs = mock_load.call_args.kwargs
        from_date = kwargs.get("from_date") or mock_load.call_args.args[4]
        to_date = kwargs.get("to_date") or mock_load.call_args.args[5]

        # The fix: fetch a window ending on the signal's day, not that day alone.
        self.assertEqual(to_date, "2026-07-17")
        self.assertNotEqual(from_date, to_date, "single-day fetch is the 0% bug")
        span = (pd.Timestamp(to_date) - pd.Timestamp(from_date)).days
        self.assertEqual(span, KRONOS_CONTEXT_LOOKBACK_DAYS)

    @patch("ml_signal.kronos_consumer.load_intraday_candles_from_dhan")
    def test_forecast_uses_trade_length_horizon_not_five(self, mock_load):
        mock_load.return_value = _candles("2026-07-17 09:15", 500)
        consumer = KronosConsumer(DEFAULT_CONFIG)

        fake_paths = MagicMock()
        fake_paths.predict_paths.return_value = [pd.DataFrame({"close": [24200.0]})]
        with patch.dict("sys.modules", {"paths": fake_paths}):
            consumer._forecast_probability(_signal("2026-07-17T06:00:00+00:00"))

        kwargs = fake_paths.predict_paths.call_args.kwargs
        self.assertEqual(kwargs["pred_len"], KRONOS_HORIZON_CANDLES)
        self.assertEqual(len(kwargs["y_timestamp"]), KRONOS_HORIZON_CANDLES)
        self.assertGreater(KRONOS_HORIZON_CANDLES, 5, "5 candles cannot span a ~45min trade")

    @patch("ml_signal.kronos_consumer.load_intraday_candles_from_dhan")
    def test_horizon_follows_profile_not_hardcoded_45(self, mock_load):
        """Expiry runs a 30-min time stop. Walking the barrier to 45 would score
        15 minutes against the original SL after the live SL has trailed to
        entry — counting hits the strategy closes at breakeven."""
        mock_load.return_value = _candles("2026-07-17 09:15", 500)
        consumer = KronosConsumer(MLConfig(kronos_horizon_candles=30))

        fake_paths = MagicMock()
        fake_paths.predict_paths.return_value = [pd.DataFrame({"close": [24200.0]})]
        with patch.dict("sys.modules", {"paths": fake_paths}):
            consumer._forecast_probability(_signal("2026-07-17T06:00:00+00:00"))

        kwargs = fake_paths.predict_paths.call_args.kwargs
        self.assertEqual(kwargs["pred_len"], 30)
        self.assertEqual(len(kwargs["y_timestamp"]), 30)

    @patch("main.settings")
    def test_main_threads_profile_time_stop_into_kronos(self, mock_settings):
        """The horizon must come from the applied profile. main.py applies the
        expiry/non-expiry profile before starting the consumer, so
        settings.time_stop_minutes is authoritative by then."""
        import asyncio
        from unittest.mock import AsyncMock
        mock_settings.discord_webhook_url = "https://discord.com/test"
        mock_settings.supabase_url = "https://mock.supabase.co"
        mock_settings.supabase_key = "mock_key"
        mock_settings.time_stop_minutes = 30  # expiry profile

        from main import _start_in_process_kronos_consumer

        captured = {}
        async def _capture(self, **kwargs):
            captured["horizon"] = self.config.kronos_horizon_candles

        with patch("ml_signal.kronos_consumer.KronosConsumer.run", _capture):
            asyncio.run(_start_in_process_kronos_consumer())

        self.assertEqual(captured["horizon"], 30)

    @patch("ml_signal.kronos_consumer.load_intraday_candles_from_dhan")
    def test_context_is_tail_capped_for_memory(self, mock_load):
        # Kronos runs in-process at 768mb; context must stay bounded no matter
        # how many days Dhan returns. See [[ares-kronos-memory-floor]].
        mock_load.return_value = _candles("2026-07-13 09:15", KRONOS_CONTEXT_CANDLES + 800)
        consumer = KronosConsumer(DEFAULT_CONFIG)

        fake_paths = MagicMock()
        fake_paths.predict_paths.return_value = [pd.DataFrame({"close": [24200.0]})]
        with patch.dict("sys.modules", {"paths": fake_paths}):
            consumer._forecast_probability(_signal("2026-07-17T06:00:00+00:00"))

        passed_df = fake_paths.predict_paths.call_args.kwargs["df"]
        self.assertLessEqual(len(passed_df), KRONOS_CONTEXT_CANDLES)

    @patch("ml_signal.kronos_consumer.load_intraday_candles_from_dhan")
    def test_starved_context_is_logged_loudly(self, mock_load):
        # The 0% shipped silently for months because a meaningless number is
        # indistinguishable from a confident one. Make starvation visible.
        mock_load.return_value = _candles("2026-07-17 09:15", 6)
        consumer = KronosConsumer(DEFAULT_CONFIG)

        fake_paths = MagicMock()
        fake_paths.predict_paths.return_value = [pd.DataFrame({"close": [24200.0]})]
        with patch.dict("sys.modules", {"paths": fake_paths}), \
             patch("builtins.print") as mock_print:
            consumer._forecast_probability(_signal())

        printed = " ".join(str(c) for c in mock_print.call_args_list)
        self.assertIn("context", printed.lower())
        self.assertLess(6, KRONOS_MIN_CONTEXT_CANDLES)

    @patch("ml_signal.kronos_consumer.load_intraday_candles_from_dhan")
    def test_healthy_context_does_not_warn(self, mock_load):
        mock_load.return_value = _candles("2026-07-13 09:15", KRONOS_CONTEXT_CANDLES)
        consumer = KronosConsumer(DEFAULT_CONFIG)

        fake_paths = MagicMock()
        fake_paths.predict_paths.return_value = [pd.DataFrame({"close": [24200.0]})]
        with patch.dict("sys.modules", {"paths": fake_paths}), \
             patch("builtins.print") as mock_print:
            consumer._forecast_probability(_signal("2026-07-17T06:00:00+00:00"))

        printed = " ".join(str(c) for c in mock_print.call_args_list)
        self.assertNotIn("thin context", printed.lower())


if __name__ == "__main__":
    unittest.main()
