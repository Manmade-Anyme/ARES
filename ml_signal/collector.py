import asyncio
import json
from collections import deque
from datetime import datetime
from typing import Optional, List, Dict, Any

from supabase import create_client, Client

from .config import MLConfig, DEFAULT_CONFIG
from .features import (
    compute_candle_features,
    compute_volume_features,
    compute_iv_features,
    compute_oi_features,
    compute_greek_features,
    compute_structure_features,
    compute_meta_features,
)


class MLCollector:
    def __init__(
        self,
        supabase_url: str,
        supabase_key: str,
        config: MLConfig = DEFAULT_CONFIG,
    ):
        self.supabase: Client = create_client(supabase_url, supabase_key)
        self.config = config

        self.volume_history: deque = deque(maxlen=20)
        self.iv_history: deque = deque(maxlen=20)

        self._total_snapshots = 0
        self._signals_recorded = 0

    @staticmethod
    def _candle_to_dict(candle) -> Dict[str, float]:
        return {
            "open": float(getattr(candle, "open", 0)),
            "high": float(getattr(candle, "high", 0)),
            "low": float(getattr(candle, "low", 0)),
            "close": float(getattr(candle, "close", 0)),
            "volume": int(getattr(candle, "volume", 0)),
            "vwap": float(getattr(candle, "vwap", 0)),
        }

    @staticmethod
    def _option_row_to_dict(option) -> Dict[str, Any]:
        return {
            "iv": float(getattr(option, "iv", 0)),
            "oi": int(getattr(option, "oi", 0)),
            "oi_change_pct": float(getattr(option, "oi_change_pct", 0)),
            "gamma": float(getattr(option, "gamma", 0)),
            "theta": float(getattr(option, "theta", 0)),
            "vega": float(getattr(option, "vega", 0)),
        }

    def _compute_totals_from_chain(
        self, full_chain: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        total_ce_oi = 0
        total_pe_oi = 0
        all_ce_oi: List[int] = []
        all_pe_oi: List[int] = []

        for strike_data in full_chain:
            if isinstance(strike_data, dict):
                ce = strike_data.get("ce", {})
                pe = strike_data.get("pe", {})
                if isinstance(ce, dict):
                    oi_val = int(ce.get("oi", 0))
                    total_ce_oi += oi_val
                    all_ce_oi.append(oi_val)
                if isinstance(pe, dict):
                    oi_val = int(pe.get("oi", 0))
                    total_pe_oi += oi_val
                    all_pe_oi.append(oi_val)

        return {
            "total_ce_oi": total_ce_oi,
            "total_pe_oi": total_pe_oi,
            "all_ce_oi": all_ce_oi,
            "all_pe_oi": all_pe_oi,
        }

    @staticmethod
    def _levels_to_prices(levels) -> List[float]:
        prices = []
        for lvl in levels:
            if hasattr(lvl, "price"):
                prices.append(float(lvl.price))
            elif isinstance(lvl, (int, float)):
                prices.append(float(lvl))
            elif isinstance(lvl, dict):
                prices.append(float(lvl.get("price", 0)))
        return prices

    def snapshot(
        self,
        candle,
        atm,
        full_chain: List[Dict[str, Any]],
        levels,
        spot: float,
        signal=None,
        pdh: Optional[float] = None,
        pdl: Optional[float] = None,
        is_expiry: bool = False,
        dte: Optional[int] = None,
        timestamp: Optional[datetime] = None,
    ) -> None:
        candle_dict = self._candle_to_dict(candle)
        atm_ce_dict = self._option_row_to_dict(atm.ce)
        atm_pe_dict = self._option_row_to_dict(atm.pe)

        self.volume_history.append(candle_dict["volume"])
        self.iv_history.append(atm_ce_dict["iv"])

        oi_totals = self._compute_totals_from_chain(full_chain)
        level_prices = self._levels_to_prices(levels)
        ts = timestamp or candle.timestamp if hasattr(candle, "timestamp") else datetime.now()

        candle_feats = compute_candle_features(candle_dict)

        vol_feats = compute_volume_features(
            candle_dict["volume"], list(self.volume_history)
        )

        iv_feats = compute_iv_features(
            current_iv=atm_ce_dict["iv"],
            iv_ce=atm_ce_dict["iv"],
            iv_pe=atm_pe_dict["iv"],
            iv_history=list(self.iv_history),
        )

        oi_feats = compute_oi_features(
            atm_ce_oi=atm_ce_dict["oi"],
            atm_pe_oi=atm_pe_dict["oi"],
            total_ce_oi=oi_totals["total_ce_oi"],
            total_pe_oi=oi_totals["total_pe_oi"],
            ce_oi_change_pct=atm_ce_dict["oi_change_pct"],
            pe_oi_change_pct=atm_pe_dict["oi_change_pct"],
            all_ce_oi=oi_totals["all_ce_oi"],
            all_pe_oi=oi_totals["all_pe_oi"],
        )

        greek_feats = compute_greek_features(
            atm_ce_gamma=atm_ce_dict["gamma"],
            atm_pe_gamma=atm_pe_dict["gamma"],
            atm_ce_theta=atm_ce_dict["theta"],
            atm_pe_theta=atm_pe_dict["theta"],
            atm_ce_vega=atm_ce_dict["vega"],
            atm_pe_vega=atm_pe_dict["vega"],
            spot=spot,
        )

        struct_feats = compute_structure_features(
            spot=spot,
            levels=level_prices,
            full_chain=full_chain,
            pdh=pdh,
            pdl=pdl,
        )

        meta_feats = compute_meta_features(
            timestamp=ts,
            dte=dte,
            is_expiry=is_expiry,
        )

        signal_generated = signal is not None
        signal_id = (
            str(getattr(signal, "signal_id", "")) if signal_generated else None
        )
        signal_setup = (
            str(getattr(signal, "setup_type", ""))
            if signal_generated
            else None
        )
        signal_direction = (
            str(getattr(signal, "direction", ""))
            if signal_generated
            else None
        )
        signal_confidence = (
            str(getattr(signal, "confidence", ""))
            if signal_generated
            else None
        )

        detector_scores = {
            "failed_breakout": int(signal_setup == "FAILED_BREAKOUT") if signal_generated else 0,
            "oi_wall_rejection": int(signal_setup == "OI_WALL_REJECTION") if signal_generated else 0,
            "exhaustion_reversal": int(signal_setup == "EXHAUSTION_REVERSAL") if signal_generated else 0,
        }

        record = {
            "timestamp": ts.isoformat() if hasattr(ts, "isoformat") else str(ts),
            "spot": spot,
            "candle_features": json.dumps(candle_feats),
            "volume_features": json.dumps(vol_feats),
            "iv_features": json.dumps(iv_feats),
            "oi_features": json.dumps(oi_feats),
            "greek_features": json.dumps(greek_feats),
            "structure_features": json.dumps(struct_feats),
            "meta_features": json.dumps(meta_feats),
            "signal_generated": signal_generated,
            "signal_id": signal_id,
            "signal_setup_type": signal_setup,
            "signal_direction": signal_direction,
            "signal_confidence": signal_confidence,
            "detector_scores": json.dumps(detector_scores),
            "raw_candle": json.dumps(candle_dict),
            "raw_atm_oi": json.dumps({"ce": atm_ce_dict, "pe": atm_pe_dict}),
        }

        self._total_snapshots += 1
        if signal_generated:
            self._signals_recorded += 1

        try:
            loop = asyncio.get_running_loop()
            loop.run_in_executor(None, self._insert, record)
        except RuntimeError:
            self._insert(record)

    def check_table_exists(self) -> bool:
        try:
            self.supabase.table("ml_collection").select("id", count="exact").limit(1).execute()
            return True
        except Exception:
            return False

    def _insert(self, record: Dict[str, Any]) -> None:
        try:
            self.supabase.table("ml_collection").insert(record).execute()
        except Exception:
            pass

    @property
    def stats(self) -> Dict[str, Any]:
        return {
            "total_snapshots": self._total_snapshots,
            "signals_recorded": self._signals_recorded,
            "volume_history_size": len(self.volume_history),
            "iv_history_size": len(self.iv_history),
        }
