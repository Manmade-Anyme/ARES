import unittest
from unittest.mock import AsyncMock, MagicMock, patch
import asyncio
from datetime import datetime

from ml_signal.kronos_consumer import KronosConsumer, DEFAULT_CONFIG
from ml_signal.config import MLConfig


class TestTask186InProcessKronos(unittest.TestCase):

    @patch("ml_signal.kronos_consumer.KronosConsumer._load_dhan_credentials")
    @patch("ml_signal.kronos_consumer.create_client")
    def test_kronos_consumer_initialization(self, mock_create_client, mock_load_dhan):
        mock_load_dhan.return_value = ("client123", "token123")
        config = MLConfig(discord_webhook_url="https://discord.com/api/webhooks/mock")
        consumer = KronosConsumer(config)
        self.assertEqual(consumer.config.discord_webhook_url, "https://discord.com/api/webhooks/mock")
        self.assertEqual(len(consumer._processed_ids), 0)

    @patch("ml_signal.kronos_consumer.send_discord", new_callable=AsyncMock)
    @patch("ml_signal.kronos_consumer.KronosConsumer._load_dhan_credentials")
    @patch("ml_signal.kronos_consumer.create_client")
    def test_kronos_consumer_seeds_past_signals_on_startup(self, mock_create_client, mock_load_dhan, mock_send_discord):
        mock_load_dhan.return_value = ("client123", "token123")
        consumer = KronosConsumer(DEFAULT_CONFIG)
        
        # Mock Supabase table query returning 2 past signals
        mock_supabase = MagicMock()
        mock_query = MagicMock()
        mock_query.gte.return_value.order.return_value.execute.return_value.data = [
            {"id": "7767", "setup_type": "FAILED_BREAKOUT", "timestamp": "2026-07-13 10:00:00"},
            {"id": "7768", "setup_type": "TREND_CONTINUATION", "timestamp": "2026-07-13 11:25:00"},
        ]
        mock_supabase.table.return_value.select.return_value = mock_query
        consumer._supabase = mock_supabase

        # Run fetch_new_signals
        signals = asyncio.run(consumer.fetch_new_signals())
        self.assertEqual(len(signals), 2)
        self.assertEqual(signals[0]["id"], "7767")

    @patch("ml_signal.kronos_consumer.send_discord", new_callable=AsyncMock)
    @patch("ml_signal.kronos_consumer.KronosConsumer._load_dhan_credentials")
    @patch("ml_signal.kronos_consumer.create_client")
    def test_kronos_consumer_utc_timezone_recent_signal_detection(self, mock_create_client, mock_load_dhan, mock_send_discord):
        import pandas as pd
        mock_load_dhan.return_value = ("client123", "token123")
        consumer = KronosConsumer(DEFAULT_CONFIG)
        
        now_utc = pd.Timestamp.now(tz="UTC")
        recent_ts_str = (now_utc - pd.Timedelta(seconds=30)).isoformat()
        old_ts_str = (now_utc - pd.Timedelta(minutes=10)).isoformat()

        signals = [
            {"id": "old_1", "created_at": old_ts_str},
            {"id": "recent_1", "created_at": recent_ts_str},
        ]

        mock_supabase = MagicMock()
        mock_supabase.table.return_value.select.return_value.gte.return_value.order.return_value.execute.return_value.data = signals
        consumer._supabase = mock_supabase

        # Execute run loop logic for 1 iteration
        with patch.object(consumer, "_init_all"), \
             patch.object(consumer, "_forecast_probability", return_value=0.75), \
             patch("asyncio.sleep", side_effect=[None, asyncio.CancelledError]):
            with self.assertRaises(asyncio.CancelledError):
                asyncio.run(consumer.run("http://url", "key"))

        # old_1 was seeded as historical (no alert); recent_1 was processed
        # normally — exactly one Discord post, and it references recent_1.
        self.assertIn("old_1", consumer._processed_ids)
        self.assertIn("recent_1", consumer._processed_ids)
        mock_send_discord.assert_called_once()
        self.assertIn("recent_1", mock_send_discord.call_args.args[1])

    @patch("ml_signal.kronos_consumer.load_intraday_candles_from_dhan")
    def test_forecast_probability_handles_utc_signal_vs_naive_ist_candles(self, mock_load_candles):
        """Regression: signal timestamps are tz-aware UTC, Dhan candles are
        tz-naive IST — the no-look-ahead slice must not raise TypeError and
        must slice at the IST-equivalent time (TASK-186 live silence bug)."""
        import pandas as pd
        consumer = KronosConsumer(DEFAULT_CONFIG)

        # 09:15–10:30 IST naive candles; signal at 04:31 UTC == 10:01 IST
        ts_index = pd.date_range("2026-07-13 09:15", periods=76, freq="1min")
        candles = pd.DataFrame({
            "timestamp": ts_index,
            "open": 24100.0, "high": 24110.0, "low": 24090.0, "close": 24100.0,
            "volume": 0,
        })
        mock_load_candles.return_value = candles

        fake_path = pd.DataFrame({"close": [24100, 24120, 24130]})
        fake_paths_mod = MagicMock()
        fake_paths_mod.predict_paths.return_value = [fake_path]

        signal = {
            "timestamp": "2026-07-13T04:31:00+00:00",
            "trigger_price": 24100.0, "target_1": 24125.0,
            "stop_loss": 24080.0, "direction": "BULLISH",
        }
        with patch.dict("sys.modules", {"paths": fake_paths_mod}):
            prob = consumer._forecast_probability(signal)

        self.assertEqual(prob, 1.0)
        # context passed to Kronos must end at 10:01 IST, not 04:31
        passed_df = fake_paths_mod.predict_paths.call_args.kwargs["x_timestamp"]
        self.assertEqual(passed_df.iloc[-1], pd.Timestamp("2026-07-13 10:01"))

    @patch("ml_signal.kronos_consumer.load_intraday_candles_from_dhan")
    def test_failed_inference_is_retried_not_dropped(self, mock_load_candles):
        """Regression: a transient error must not permanently mark the signal
        processed — it stays retryable until MAX_SIGNAL_ATTEMPTS."""
        from ml_signal.kronos_consumer import MAX_SIGNAL_ATTEMPTS
        import pandas as pd

        consumer = KronosConsumer(DEFAULT_CONFIG)
        signal = {"id": "99", "created_at": pd.Timestamp.now(tz="UTC").isoformat(),
                  "timestamp": pd.Timestamp.now(tz="UTC").isoformat()}

        mock_supabase = MagicMock()
        mock_supabase.table.return_value.select.return_value.gte.return_value.order.return_value.execute.return_value.data = [signal]
        consumer._supabase = mock_supabase

        boom = MagicMock(side_effect=RuntimeError("dhan hiccup"))
        # +1 None for run()'s initial asyncio.sleep(0) yield
        sleeps = [None] * MAX_SIGNAL_ATTEMPTS + [asyncio.CancelledError]
        with patch.object(consumer, "_init_all"), \
             patch.object(consumer, "_forecast_probability", boom), \
             patch("asyncio.sleep", side_effect=sleeps):
            with self.assertRaises(asyncio.CancelledError):
                asyncio.run(consumer.run("http://url", "key"))

        self.assertEqual(boom.call_count, MAX_SIGNAL_ATTEMPTS)
        self.assertIn("99", consumer._processed_ids)  # gave up after max attempts

    def test_import_does_not_require_offline_ml_deps(self):
        """Regression: the production image ships only root requirements.txt
        (no joblib/xgboost/sklearn). Importing kronos_consumer via a chain
        that needs them (e.g. ml_signal.live -> predictor -> joblib) killed
        the consumer at startup in prod (TASK-186 live silence, round 2)."""
        import subprocess, sys, textwrap
        code = textwrap.dedent("""
            import sys
            for mod in ("joblib", "xgboost", "sklearn", "optuna", "shap"):
                sys.modules[mod] = None  # None => ImportError on import
            import ml_signal.kronos_consumer
            print("OK")
        """)
        result = subprocess.run([sys.executable, "-c", code],
                                capture_output=True, text=True)
        self.assertIn("OK", result.stdout, msg=result.stderr)

    @patch("main.settings")
    def test_start_in_process_kronos_consumer_helper(self, mock_settings):
        mock_settings.discord_webhook_url = "https://discord.com/test"
        mock_settings.supabase_url = "https://mock.supabase.co"
        mock_settings.supabase_key = "mock_key"

        from main import _start_in_process_kronos_consumer

        with patch("ml_signal.kronos_consumer.KronosConsumer.run", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = None
            asyncio.run(_start_in_process_kronos_consumer())
            mock_run.assert_called_once_with(supabase_url="https://mock.supabase.co", supabase_key="mock_key")


if __name__ == "__main__":
    unittest.main()
