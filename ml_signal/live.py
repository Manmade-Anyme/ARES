"""
Standalone continuous live runner for the XGBoost Signal Prediction Module.

Runs independently of ARES. Polls Dhan API for latest candle + option chain,
computes features, runs inference, and logs predictions to Supabase.

Usage:
    python -m ml_signal.live
"""

import asyncio
import os
from collections import deque
from datetime import datetime
from typing import Optional, List, Dict, Any, Tuple

from dhanhq import dhanhq
from supabase import create_client, Client

from detectors.expiry_detector import days_to_expiry
from .config import MLConfig, DEFAULT_CONFIG
from .predictor import SignalPredictor
from .discord import send_prediction_alert, send_summary_alert


class LiveRunner:

    def __init__(self, config: MLConfig = DEFAULT_CONFIG):
        self.config = config
        self.predictor = SignalPredictor(config)

        self.volume_history: deque = deque(maxlen=20)
        self.iv_history: deque = deque(maxlen=20)
        self._dhan = None
        self._supabase: Optional[Client] = None

        self._total_predictions = 0
        self._high_count = 0
        self._med_count = 0
        self._low_count = 0
        self._prob_sum = 0.0
        self._last_summary_time: Optional[datetime] = None

    def _init_dhan(self, client_id: str, access_token: str):
        try:
            from dhanhq import DhanContext
            context = DhanContext(client_id, access_token)
            self._dhan = dhanhq(context)
        except ImportError:
            self._dhan = dhanhq(client_id, access_token)

    def _init_supabase(self, url: str, key: str):
        self._supabase = create_client(url, key)

    async def fetch_candle(self, security_id: str, exchange: str, date: str) -> Optional[Dict[str, float]]:
        loop = asyncio.get_running_loop()

        def _fetch():
            return self._dhan.intraday_minute_data(security_id, exchange, security_id, date, date)

        response = await loop.run_in_executor(None, _fetch)

        if not response or response.get("status") != "success":
            return None

        data = response.get("data", {})
        if not data:
            return None

        time_key = "start_Time" if "start_Time" in data else "timestamp"

        return {
            "timestamp": datetime.fromtimestamp(data[time_key][-1] / 1000
                                                if isinstance(data[time_key][-1], (int, float)) and data[time_key][-1] > 1e11
                                                else data[time_key][-1]),
            "open": float(data["open"][-1]),
            "high": float(data["high"][-1]),
            "low": float(data["low"][-1]),
            "close": float(data["close"][-1]),
            "volume": int(data["volume"][-1]),
        }

    async def fetch_option_chain(self, security_id: str, exchange: str, expiry: str) -> Optional[Dict[str, Any]]:
        loop = asyncio.get_running_loop()

        def _fetch():
            return self._dhan.option_chain(
                under_security_id=int(security_id),
                under_exchange_segment=exchange,
                expiry=expiry,
            )

        response = await loop.run_in_executor(None, _fetch)

        if not response or response.get("status") != "success":
            return None

        return response

    async def log_prediction(self, prediction: Dict[str, Any]):
        if self._supabase is None:
            return

        def _insert():
            try:
                self._supabase.table(self.config.supabase_table_predictions).insert(prediction).execute()
            except Exception as e:
                print(f"Failed to log prediction: {e}")

        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, _insert)

    def _parse_option_chain(self, oc_response: Optional[Dict[str, Any]], spot: float) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        atm_ce = {"iv": 0, "oi": 0, "oi_change_pct": 0, "gamma": 0, "theta": 0, "vega": 0}
        atm_pe = {"iv": 0, "oi": 0, "oi_change_pct": 0, "gamma": 0, "theta": 0, "vega": 0}

        if oc_response and "data" in oc_response:
            resp_data = oc_response["data"]
            oc_data = resp_data.get("oc", resp_data.get("data", {}).get("oc", {})) if isinstance(resp_data, dict) else {}

            atm_strike = round(spot / 50) * 50
            for strike_key, data in oc_data.items():
                if abs(float(strike_key) - atm_strike) <= 25:
                    ce = data.get("ce", {})
                    pe = data.get("pe", {})
                    
                    ce_greeks = ce.get("greeks", {})
                    pe_greeks = pe.get("greeks", {})
                    
                    atm_ce = {
                        "iv": float(ce.get("implied_volatility", 0)),
                        "oi": int(ce.get("oi", 0)),
                        "oi_change_pct": 0,
                        "gamma": float(ce_greeks.get("gamma", 0)),
                        "theta": float(ce_greeks.get("theta", 0)),
                        "vega": float(ce_greeks.get("vega", 0)),
                    }
                    atm_pe = {
                        "iv": float(pe.get("implied_volatility", 0)),
                        "oi": int(pe.get("oi", 0)),
                        "oi_change_pct": 0,
                        "gamma": float(pe_greeks.get("gamma", 0)),
                        "theta": float(pe_greeks.get("theta", 0)),
                        "vega": float(pe_greeks.get("vega", 0)),
                    }
                    break
        return atm_ce, atm_pe

    async def run(
        self,
        dhan_client_id: str,
        dhan_access_token: str,
        supabase_url: str,
        supabase_key: str,
        security_id: str = "13",
        exchange_segment: str = "IDX_I",
        expiry: str = "",
    ):
        self._init_dhan(dhan_client_id, dhan_access_token)
        self._init_supabase(supabase_url, supabase_key)
        continuous_models = os.path.join(os.path.dirname(os.path.abspath(__file__)), "models_continuous")
        from .predictor import discover_latest_model
        latest_path, _ = discover_latest_model(continuous_models)
        self.predictor.load_model(latest_path)

        print(f"[ML Signal] Starting continuous prediction loop (poll={self.config.poll_interval_seconds}s)")

        while True:
            now = datetime.now()

            try:
                date_str = now.strftime("%Y-%m-%d")

                candle = await self.fetch_candle(security_id, exchange_segment, date_str)
                if candle is None:
                    await asyncio.sleep(self.config.poll_interval_seconds)
                    continue

                spot = candle["close"]

                oc_response = await self.fetch_option_chain(security_id, exchange_segment, expiry)
                atm_ce, atm_pe = self._parse_option_chain(oc_response, spot)

                # Must match what MLCollector records, or the model trains on a real
                # dte and is served the 7.0 fallback. `expiry` is the same date the
                # chain above was fetched for.
                dte = days_to_expiry(expiry)

                result = self.predictor.predict_from_raw(
                    candle=candle,
                    volume_history=list(self.volume_history),
                    iv_history=list(self.iv_history),
                    atm_ce=atm_ce,
                    atm_pe=atm_pe,
                    total_ce_oi=0,
                    total_pe_oi=0,
                    all_ce_oi=None,
                    all_pe_oi=None,
                    levels=[],
                    timestamp=candle.get("timestamp", now),
                    spot=spot,
                    pdh=None,
                    pdl=None,
                    dte=dte,
                    is_expiry=(dte == 0),
                )

                # Append AFTER the predict call — build_feature_vector expects
                # PRIOR bars only. Appending first made iv_change_1 a self-vs-self
                # diff and zeroed iv_acceleration. Same contract as
                # MLCollector.snapshot; see tests/unit/test_ml_feature_fidelity.py.
                self.volume_history.append(candle["volume"])
                self.iv_history.append(atm_ce["iv"])

                result["source"] = "continuous"

                await self.log_prediction(result)

                proba = result["probability"]
                tier = result["confidence_tier"]

                self._total_predictions += 1
                self._prob_sum += proba
                if tier == "HIGH":
                    self._high_count += 1
                elif tier == "MEDIUM":
                    self._med_count += 1
                else:
                    self._low_count += 1

                if tier == "HIGH":
                    await send_prediction_alert(
                        self.config,
                        probability=proba,
                        confidence_tier=tier,
                        spot=spot,
                        source="continuous",
                    )

                if self._last_summary_time is None:
                    self._last_summary_time = now
                elapsed = (now - self._last_summary_time).total_seconds()
                if elapsed >= self.config.discord_summary_interval_minutes * 60:
                    await send_summary_alert(
                        self.config,
                        total_predictions=self._total_predictions,
                        high_count=self._high_count,
                        med_count=self._med_count,
                        low_count=self._low_count,
                        avg_probability=self._prob_sum / max(self._total_predictions, 1),
                    )
                    self._total_predictions = 0
                    self._high_count = 0
                    self._med_count = 0
                    self._low_count = 0
                    self._prob_sum = 0.0
                    self._last_summary_time = now

                print(f"[{now.strftime('%H:%M:%S')}] Spot={spot:.2f}  "
                      f"Prob(T1)={proba:.2%}  Confidence={tier}")

            except Exception as e:
                print(f"[ML Signal] Error in prediction loop: {e}")

            await asyncio.sleep(self.config.poll_interval_seconds)


def load_dhan_credentials_from_supabase(supabase_url: str, supabase_key: str):
    supabase = create_client(supabase_url, supabase_key)
    response = supabase.table("api_keys").select("client_id, access_token").eq("provider", "DHAN").execute()
    if not response.data:
        raise ValueError("No DHAN credentials found in Supabase api_keys table")
    data = response.data[0]
    return data["client_id"], data["access_token"]


async def main():
    config = DEFAULT_CONFIG

    supabase_url = os.getenv("SUPABASE_URL", "")
    supabase_key = os.getenv("SUPABASE_KEY", "")

    if not supabase_url or not supabase_key:
        raise ValueError("SUPABASE_URL and SUPABASE_KEY must be set in .env")

    discord_url = os.getenv("DISCORD_WEBHOOK_URL", "")
    if discord_url:
        config.discord_webhook_url = discord_url
        print("[+] Discord alerts enabled.")
    else:
        print("[!] DISCORD_WEBHOOK_URL not set — alerts disabled.")

    print("[+] Loading Dhan credentials from Supabase...")
    dhan_client_id, dhan_access_token = load_dhan_credentials_from_supabase(supabase_url, supabase_key)
    print("[+] Dhan credentials loaded successfully.")

    runner = LiveRunner(config)
    await runner.run(
        dhan_client_id=dhan_client_id,
        dhan_access_token=dhan_access_token,
        supabase_url=supabase_url,
        supabase_key=supabase_key,
        security_id=os.getenv("SECURITY_ID", "13"),
        exchange_segment=os.getenv("EXCHANGE_SEGMENT", "IDX_I"),
        expiry=os.getenv("EXPIRY", ""),
    )


if __name__ == "__main__":
    asyncio.run(main())
