"""
Event-triggered signal consumer.

Polls the ares_signals table for new signals, runs prediction on the
market context at signal time, and enriches the signal with ML confidence.

Usage:
    python -m ml_signal.signal_consumer
"""

import asyncio
import os
from datetime import datetime
from typing import Optional, Set

from supabase import create_client, Client

from .config import MLConfig, DEFAULT_CONFIG
from .predictor import SignalPredictor
from .features import build_feature_vector
from .discord import send_prediction_alert


class SignalConsumer:

    def __init__(self, config: MLConfig = DEFAULT_CONFIG):
        self.config = config
        self.predictor = SignalPredictor(config)
        self._supabase: Optional[Client] = None
        self._processed_ids: Set[str] = set()

    def _init_supabase(self, url: str, key: str):
        self._supabase = create_client(url, key)

    async def fetch_new_signals(self) -> list:
        if self._supabase is None:
            return []

        def _query():
            today = datetime.now().strftime("%Y-%m-%d")
            response = (
                self._supabase.table("ares_signals")
                .select("*")
                .gte("timestamp", today)
                .order("created_at", desc=False)
                .execute()
            )
            return response.data if response.data else []

        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, _query)

    async def log_prediction(self, prediction: dict):
        if self._supabase is None:
            return

        def _insert():
            try:
                self._supabase.table(self.config.supabase_table_predictions).insert(prediction).execute()
            except Exception as e:
                print(f"Failed to log prediction: {e}")

        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, _insert)

    async def run(
        self,
        supabase_url: str,
        supabase_key: str,
    ):
        self._init_supabase(supabase_url, supabase_key)
        self.predictor.load_model()

        print(f"[ML Consumer] Starting signal consumer (poll={self.config.signal_poll_interval_seconds}s)")

        while True:
            try:
                signals = await self.fetch_new_signals()

                for signal in signals:
                    signal_id = str(signal.get("id", ""))
                    if signal_id in self._processed_ids:
                        continue

                    self._processed_ids.add(signal_id)

                    features = signal.get("market_context", {})
                    spot = float(signal.get("spot_at_signal", signal.get("trigger_price", 0)))

                    candle = {
                        "open": features.get("open", spot),
                        "high": features.get("high", spot),
                        "low": features.get("low", spot),
                        "close": spot,
                        "volume": features.get("volume", 0),
                    }

                    result = self.predictor.predict_from_raw(
                        candle=candle,
                        volume_history=[],
                        iv_history=[],
                        atm_ce={"iv": 0, "oi": 0, "oi_change_pct": 0, "gamma": 0, "theta": 0, "vega": 0},
                        atm_pe={"iv": 0, "oi": 0, "oi_change_pct": 0, "gamma": 0, "theta": 0, "vega": 0},
                        total_ce_oi=0,
                        total_pe_oi=0,
                        all_ce_oi=None,
                        all_pe_oi=None,
                        levels=[],
                        timestamp=signal.get("timestamp", datetime.now()),
                        spot=spot,
                        pdh=None,
                        pdl=None,
                        dte=None,
                        is_expiry=False,
                    )

                    result["source"] = "event_triggered"
                    result["signal_id"] = signal_id
                    result["signal_setup_type"] = signal.get("setup_type", "")
                    result["spot"] = spot

                    await self.log_prediction(result)

                    proba = result["probability"]
                    tier = result["confidence_tier"]

                    await send_prediction_alert(
                        self.config,
                        probability=proba,
                        confidence_tier=tier,
                        spot=spot,
                        source="event_triggered",
                        signal_id=signal_id,
                        signal_setup_type=signal.get("setup_type", ""),
                    )

                    print(f"[ML Consumer] Signal #{signal_id} ({signal.get('setup_type', '?')}) "
                          f"→ Prob(T1)={proba:.2%}")

            except Exception as e:
                print(f"[ML Consumer] Error: {e}")

            await asyncio.sleep(self.config.signal_poll_interval_seconds)


async def main():
    config = DEFAULT_CONFIG

    discord_url = os.getenv("DISCORD_WEBHOOK_URL", "")
    if discord_url:
        config.discord_webhook_url = discord_url
        print("[+] Discord alerts enabled for signal consumer.")
    else:
        print("[!] DISCORD_WEBHOOK_URL not set — alerts disabled.")

    consumer = SignalConsumer(config)
    await consumer.run(
        supabase_url=os.getenv("SUPABASE_URL", ""),
        supabase_key=os.getenv("SUPABASE_KEY", ""),
    )


if __name__ == "__main__":
    asyncio.run(main())
