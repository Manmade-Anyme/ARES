"""
Kronos live probability consumer.

Runs in-process inside main.py as a background asyncio task (TASK-186 —
the original TASK-184 design ran it as a separate process, but nothing in
the Fly deployment ever launched that process, so it never ran live).
Model init and per-signal inference are offloaded via asyncio.to_thread so
the trading loop's event loop is never blocked; the trade-off vs. a separate
process is that a hard crash (OOM, native segfault) now shares the process
with the trading loop. Polls the ares_signals table for new rows, runs its
own inference, posts its own follow-up Discord message.

Adds exactly one number per fired signal: the fraction of Kronos-sampled
forward price paths that clear the signal's own target before its own stop,
in the signal's own direction. Purely informational — does not touch signal
generation, entries, or SL/T1/T2.

Usage:
    python -m ml_signal.kronos_consumer
"""

import asyncio
import os
import sys
from datetime import datetime, timedelta
from typing import Optional, Set

import pandas as pd
from supabase import create_client, Client

from .config import MLConfig, DEFAULT_CONFIG
from .data import load_intraday_candles_from_dhan
from .discord import send_discord, ist_now
# Deliberately NOT importing from .live — its chain (live -> predictor ->
# joblib/xgboost) requires the offline-ML deps, which are absent from the
# production image and would kill this consumer at import time.

_VENDOR_DIR = os.path.join(os.path.dirname(__file__), "kronos_vendor")
if _VENDOR_DIR not in sys.path:
    sys.path.insert(0, _VENDOR_DIR)


KRONOS_MODEL = "NeoQuasar/Kronos-mini"
KRONOS_TOKENIZER = "NeoQuasar/Kronos-Tokenizer-2k"
KRONOS_MAX_CONTEXT = 2048
KRONOS_SAMPLE_COUNT = 20
MAX_SIGNAL_ATTEMPTS = 3


def barrier_hit_fraction(paths, entry: float, target: float, stop: float, bullish: bool) -> float:
    """
    Fraction of sampled forecast paths whose close series clears `target`
    before `stop`, walked in order (first-touch wins; a path that touches
    neither before the horizon ends counts as a miss).
    """
    if not paths:
        return float("nan")

    hits = 0
    for path_df in paths:
        closes = path_df["close"].tolist()
        resolved = False
        for c in closes:
            if bullish:
                if c >= target:
                    hits += 1
                    resolved = True
                    break
                if c <= stop:
                    resolved = True
                    break
            else:
                if c <= target:
                    hits += 1
                    resolved = True
                    break
                if c >= stop:
                    resolved = True
                    break
        # unresolved paths (neither barrier touched) count as a miss
    return hits / len(paths)


def format_kronos_alert(signal_id: str, setup_type: str, probability: float, model_name: str) -> str:
    marker = "+" if probability >= 0.5 else "-"
    return f"""```diff
{marker} 🔮 KRONOS FORWARD PROBABILITY
   🕒 Time     : {ist_now()} IST
   📡 Signal   : #{signal_id} ({setup_type})
   🎯 P(target before stop): {probability:.1%}
   🤖 Model    : {model_name}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
```"""


class KronosConsumer:

    def __init__(self, config: MLConfig = DEFAULT_CONFIG):
        self.config = config
        self._supabase: Optional[Client] = None
        self._dhan = None
        self._predictor = None
        self._processed_ids: Set[str] = set()
        self._failed_attempts: dict = {}

    def _init_supabase(self, url: str, key: str):
        self._supabase = create_client(url, key)

    def _init_dhan(self, client_id: str, access_token: str):
        try:
            from dhanhq import DhanContext, dhanhq
            context = DhanContext(client_id, access_token)
            self._dhan = dhanhq(context)
        except ImportError:
            from dhanhq import dhanhq
            self._dhan = dhanhq(client_id, access_token)

    def _load_model(self):
        from model import Kronos, KronosTokenizer, KronosPredictor
        tokenizer = KronosTokenizer.from_pretrained(KRONOS_TOKENIZER)
        model = Kronos.from_pretrained(KRONOS_MODEL)
        self._predictor = KronosPredictor(model, tokenizer, device="cpu", max_context=KRONOS_MAX_CONTEXT)

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

    def _forecast_probability(self, signal: dict) -> Optional[float]:
        """Blocking Kronos inference for one signal — run via to_thread."""
        from paths import predict_paths

        # Signal timestamps are stored tz-aware UTC; Dhan candles come back
        # tz-naive IST (data.py builds them with datetime.fromtimestamp).
        # Convert to naive IST so the no-look-ahead slice below compares like
        # with like — a tz-aware vs naive comparison raises TypeError.
        ts = pd.to_datetime(signal.get("timestamp"))
        if ts.tzinfo is not None:
            ts = ts.tz_convert("Asia/Kolkata").tz_localize(None)
        date_str = ts.strftime("%Y-%m-%d")

        candles = load_intraday_candles_from_dhan(
            self._dhan,
            self.config.security_id,
            self.config.exchange_segment,
            self.config.instrument_type,
            date_str,
        )
        candles = candles[candles["timestamp"] <= ts]
        if candles.empty:
            return None
        candles = candles.tail(KRONOS_MAX_CONTEXT)

        x_timestamp = candles["timestamp"].reset_index(drop=True)
        last_ts = x_timestamp.iloc[-1]
        y_timestamp = pd.Series(
            [last_ts + timedelta(minutes=i + 1) for i in range(self.config.lookforward_candles)]
        )

        paths = predict_paths(
            self._predictor,
            df=candles[["open", "high", "low", "close"]],
            x_timestamp=x_timestamp,
            y_timestamp=y_timestamp,
            pred_len=self.config.lookforward_candles,
            sample_count=KRONOS_SAMPLE_COUNT,
            verbose=False,
        )

        entry = float(signal.get("trigger_price", 0))
        target = float(signal.get("target_1", 0))
        stop = float(signal.get("stop_loss", 0))
        bullish = signal.get("direction") == "BULLISH"

        return barrier_hit_fraction(paths, entry, target, stop, bullish)

    def _load_dhan_credentials(self) -> tuple:
        # Same query as storage.py / ml_signal.live, inlined to avoid their
        # heavy import chains; reuses the already-initialized client.
        response = self._supabase.table("api_keys").select("client_id, access_token").eq("provider", "DHAN").execute()
        if not response.data:
            raise ValueError("No DHAN credentials found in Supabase api_keys table")
        data = response.data[0]
        return data["client_id"], data["access_token"]

    def _init_all(self, supabase_url: str, supabase_key: str):
        self._init_supabase(supabase_url, supabase_key)
        dhan_client_id, dhan_access_token = self._load_dhan_credentials()
        self._init_dhan(dhan_client_id, dhan_access_token)
        self._load_model()

    async def run(self, supabase_url: str, supabase_key: str):
        # Yield to event loop immediately so main.py startup isn't blocked
        await asyncio.sleep(0)
        # Offload synchronous credential loading and heavy model initialization to a worker thread
        await asyncio.to_thread(self._init_all, supabase_url, supabase_key)

        print(f"[Kronos Consumer] Starting (model={KRONOS_MODEL}, poll={self.config.signal_poll_interval_seconds}s)")

        is_first_run = True
        while True:
            try:
                signals = await self.fetch_new_signals()

                if is_first_run:
                    now_utc = pd.Timestamp.now(tz="UTC")
                    for signal in signals:
                        signal_id = str(signal.get("id", ""))
                        sig_ts = signal.get("created_at") or signal.get("timestamp")
                        is_recent = False
                        if sig_ts:
                            try:
                                dt = pd.to_datetime(sig_ts, utc=True)
                                if not pd.isna(dt):
                                    if (now_utc - dt).total_seconds() < 180:
                                        is_recent = True
                            except Exception:
                                pass
                        if not is_recent:
                            self._processed_ids.add(signal_id)
                    is_first_run = False

                for signal in signals:
                    signal_id = str(signal.get("id", ""))
                    if signal_id in self._processed_ids:
                        continue

                    # Mark processed only after success — a transient Dhan/
                    # Supabase error must not permanently eat the alert.
                    # Bounded retries so a persistently bad row can't spam
                    # the log every poll forever.
                    try:
                        probability = await asyncio.to_thread(self._forecast_probability, signal)
                    except Exception as e:
                        attempts = self._failed_attempts.get(signal_id, 0) + 1
                        self._failed_attempts[signal_id] = attempts
                        print(f"[Kronos Consumer] Signal #{signal_id} inference failed "
                              f"(attempt {attempts}/{MAX_SIGNAL_ATTEMPTS}): {e}")
                        if attempts >= MAX_SIGNAL_ATTEMPTS:
                            self._processed_ids.add(signal_id)
                        continue

                    self._processed_ids.add(signal_id)
                    if probability is None:
                        continue

                    content = format_kronos_alert(
                        signal_id=signal_id,
                        setup_type=signal.get("setup_type", "?"),
                        probability=probability,
                        model_name=KRONOS_MODEL,
                    )
                    await send_discord(self.config.discord_webhook_url, content)

                    print(f"[Kronos Consumer] Signal #{signal_id} ({signal.get('setup_type', '?')}) "
                          f"→ Kronos P(target before stop)={probability:.2%}")

            except Exception as e:
                print(f"[Kronos Consumer] Error: {e}")

            await asyncio.sleep(self.config.signal_poll_interval_seconds)


async def main():
    config = DEFAULT_CONFIG

    discord_url = os.getenv("DISCORD_WEBHOOK_URL", "")
    if discord_url:
        config.discord_webhook_url = discord_url
        print("[+] Discord alerts enabled for Kronos consumer.")
    else:
        print("[!] DISCORD_WEBHOOK_URL not set — alerts disabled.")

    consumer = KronosConsumer(config)
    await consumer.run(
        supabase_url=os.getenv("SUPABASE_URL", ""),
        supabase_key=os.getenv("SUPABASE_KEY", ""),
    )


if __name__ == "__main__":
    asyncio.run(main())
