"""
SQL to create the ares_signals table:

CREATE TABLE ares_signals (
  id bigserial primary key,
  setup_type text,
  direction text,
  confidence text,
  trigger_price numeric,
  spot_at_signal numeric,
  stop_loss numeric,
  target_1 numeric,
  target_2 numeric,
  strike integer,
  option_type text,
  reasons jsonb,
  timestamp timestamptz,
  created_at timestamptz default now()
);
"""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
import logging
import math
from typing import Any, Dict, List, Optional, Union
import numpy as np
from supabase import create_client, Client

from models import AresSignal
from config import settings

logger = logging.getLogger(__name__)

IST = timezone(timedelta(hours=5, minutes=30))



def to_utc_iso(ts: Any) -> str:
    """
    Normalize a timestamp for Supabase timestamptz columns (TASK-172, audit
    item 13/18). Candle timestamps arrive as naive IST wall-clock from the Dhan
    feed; storing them unlabeled made Postgres read them as UTC, putting entry
    timestamps 5h30m ahead of the real-UTC exit timestamps and breaking every
    hold-time analysis. Naive values are labeled IST, then converted to UTC.
    """
    if isinstance(ts, str):
        if ts.endswith("Z") or ts.endswith("z"):
            ts = ts[:-1] + "+00:00"
        ts = datetime.fromisoformat(ts)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=IST)
    return ts.astimezone(timezone.utc).isoformat()




class Storage:
    """
    Storage handles persisting ARES signals to a Supabase PostgreSQL database
    for post-session review and backtesting.
    """

    def __init__(self):
        """
        Initialize the Supabase client using credentials from settings.
        """
        self.supabase: Client = create_client(
            settings.supabase_url,
            settings.supabase_key
        )

    async def log_signal(self, signal: AresSignal, spot: float) -> bool:
        """
        Asynchronously logs a signal to the 'ares_signals' table in Supabase.
        
        Args:
            signal: The generated AresSignal object.
            spot: The current NIFTY spot price when the signal was generated.
        """
        def _insert():
            reasons = list(signal.reasons)
            if getattr(signal, "suggested_lots", None) is not None:
                reasons.append(
                    f"Option Sizing: {signal.suggested_lots} lots suggested | "
                    f"Capital: ₹{signal.capital:,.2f} | Risk: {signal.risk_pct:.1f}% | "
                    f"Option SL: ₹{signal.option_sl:.2f} | Option Target: ₹{signal.option_target:.2f} | "
                    f"Premium: ₹{signal.option_premium:.2f} | Delta: {signal.option_delta:+.4f}"
                )
            data = {
                "setup_type": signal.setup_type.value,
                "direction": signal.direction.value,
                "confidence": signal.confidence,
                "trigger_price": signal.trigger_price,
                "spot_at_signal": spot,
                "stop_loss": signal.stop_loss,
                "target_1": signal.target_1,
                "target_2": signal.target_2,
                "strike": signal.strike_to_trade,
                "option_type": signal.option_type,
                "reasons": reasons,  # Supabase handles list -> jsonb serialization
                "timestamp": to_utc_iso(signal.timestamp),
                "oi_wall_context": getattr(signal, "oi_wall_context", None),
            }
            mode = settings.signal_schema_mode
            if mode == "bridge":
                data.update({
                    "signal_uuid": str(signal.id),
                    "display_id": signal.display_id,
                })
            elif mode == "greenfield":
                data.update({
                    "id": str(signal.id),
                    "display_id": signal.display_id,
                })
            else:
                raise ValueError(f"Unsupported signal schema mode: {mode}")
            # Require the database to echo the persisted canonical UUID. A
            # successful HTTP response without the expected parent row is not
            # sufficient to let downstream trade writers proceed.
            response = self.supabase.table("ares_signals").insert(data).execute()
            rows = getattr(response, "data", None)
            if not rows or not isinstance(rows[0], dict):
                raise RuntimeError("Signal insert returned no persisted row")
            row = rows[0]
            if mode == "bridge":
                if str(row.get("signal_uuid")) != str(signal.id):
                    raise RuntimeError("Signal insert returned a mismatched canonical UUID")
                signal.db_id = row.get("id")
                if signal.db_id is None:
                    raise RuntimeError("Bridge signal insert returned no legacy row id")
            elif str(row.get("id")) != str(signal.id):
                raise RuntimeError("Signal insert returned a mismatched canonical UUID")
            return True

        try:
            # Run the synchronous Supabase insert in an executor to avoid blocking the event loop
            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(None, _insert)
        except Exception as e:  # pragma: no cover
            raise RuntimeError(f"Failed to persist signal: {e}") from e


class AnalyticsLogger:
    """
    AnalyticsLogger handles permanent storage of trade results and detailed
    market context in the 'trade_analytics' table for post-session analysis
    and machine learning.
    """

    def __init__(self):
        """
        Initialize the Supabase client using credentials from settings.
        """
        self.supabase: Client = create_client(
            settings.supabase_url,
            settings.supabase_key
        )
        self._entry_futures = {}

    def log_entry(self, trade_id: str, signal: AresSignal, spot: float, atm: Any = None) -> None:
        """
        Creates a new entry in 'trade_analytics' at the moment a trade is opened.
        
        Args:
            trade_id: The unique UUID of the trade.
            signal: The AresSignal object that triggered the trade.
            spot: The current spot price at entry.
            atm: The ATMStrikes context at entry (optional).
        """
        oi_data = {}
        if atm:
            try:
                # Calculate implied PCR for the ATM strike
                ce_oi = atm.ce.oi
                pe_oi = atm.pe.oi
                pcr = pe_oi / ce_oi if ce_oi > 0 else 0.0
                
                oi_data = {
                    "pcr": round(pcr, 4),
                    "atm_ce_oi": ce_oi,
                    "atm_pe_oi": pe_oi,
                    "ce_oi_change_pct": round(atm.ce.oi_change_pct, 2),
                    "pe_oi_change_pct": round(atm.pe.oi_change_pct, 2)
                }
            except Exception as e:  # pragma: no cover
                print(f"AnalyticsLogger: Failed to parse OI data for entry: {e}")

        db_reasons = list(signal.reasons)
        if getattr(signal, "suggested_lots", None) is not None:
            db_reasons.append(
                f"Option Sizing: {signal.suggested_lots} lots suggested | "
                f"Capital: ₹{signal.capital:,.2f} | Risk: {signal.risk_pct:.1f}% | "
                f"Option SL: ₹{signal.option_sl:.2f} | Option Target: ₹{signal.option_target:.2f} | "
                f"Premium: ₹{signal.option_premium:.2f} | Delta: {signal.option_delta:+.4f}"
            )

        market_context = {
            "reasons": db_reasons,
            "spot_at_signal": float(signal.trigger_price),
            "confidence": signal.confidence,
            "entry_spot": float(spot)
        }

        # Add Option Sizing calculations if populated
        if getattr(signal, "suggested_lots", None) is not None:
            market_context["options_sizing"] = {
                "suggested_lots": signal.suggested_lots,
                "option_sl": signal.option_sl,
                "option_target": signal.option_target,
                "capital": signal.capital,
                "delta": signal.option_delta,
                "premium": signal.option_premium,
                "risk_pct": signal.risk_pct
            }

        # Add OI Wall Telemetry Context (TASK-073)
        if getattr(signal, "oi_wall_context", None) is not None:
            market_context["oi_wall"] = signal.oi_wall_context

        if getattr(signal, "db_id", None) is not None:
            market_context["signal_db_id"] = signal.db_id

        mode = settings.signal_schema_mode
        if mode == "bridge":
            signal_fields = {
                "signal_id": getattr(signal, "db_id", None),
                "signal_uuid": str(signal.id),
            }
        elif mode == "greenfield":
            signal_fields = {"signal_id": str(signal.id)}
        else:
            raise ValueError(f"Unsupported signal schema mode: {mode}")

        data = {
            "id": trade_id,
            "setup_type": signal.setup_type.value,
            "direction": signal.direction.value,
            "entry_timestamp": to_utc_iso(signal.timestamp),
            "entry_price": float(spot),
            "result_state": "OPEN",
            "market_context": market_context,
            "oi_data": oi_data
        }
        data.update(signal_fields)

        def _insert():
            self.supabase.table("trade_analytics").insert(data).execute()

        try:
            loop = asyncio.get_running_loop()
            self._entry_futures[trade_id] = loop.run_in_executor(None, _insert)
        except RuntimeError:
            try:
                _insert()
            except Exception as e:
                print(f"Failed to log trade analytics entry: {e}")
        except Exception as e:
            print(f"Failed to log trade analytics entry: {e}")

    def log_exit(
        self,
        trade_id: str,
        exit_price: float,
        final_state: str,
        exit_timestamp: str | None = None,
        pnl_points_override: float | None = None,
    ) -> None:
        """
        Updates an existing entry in 'trade_analytics' with exit details.
        
        Args:
            trade_id: The unique UUID of the trade.
            exit_price: The spot price at exit.
            final_state: The final state of the trade (e.g., SL_HIT, T1_HIT).
            pnl_points_override: Optional P&L points to persist instead of
                calculating from the fill price. Used for BE stops after T1,
                where the fill remains at entry but T1 profit was locked.
        """
        def _update():
            mode = settings.signal_schema_mode
            if mode == "bridge":
                fields = "entry_price,direction,signal_id,signal_uuid,setup_type,entry_timestamp"
            else:
                fields = "entry_price,direction,signal_id,setup_type,entry_timestamp"
                
            import time
            for _ in range(3):
                response = self.supabase.table("trade_analytics").select(fields).eq("id", trade_id).execute()
                if response.data:
                    break
                time.sleep(0.1)
            if not response.data:
                return

            record = response.data[0]
            
            # Timestamp validation
            if exit_timestamp:
                try:
                    event_ts = to_utc_iso(exit_timestamp)
                except Exception:
                    event_ts = exit_timestamp
            else:
                event_ts = datetime.now(timezone.utc).isoformat()
                
            entry_ts = record.get("entry_timestamp")
            time_metrics_excluded = False
            if entry_ts and event_ts < entry_ts:
                print(f"AnalyticsLogger: Exit timestamp {event_ts} precedes entry {entry_ts} for {trade_id}")
                time_metrics_excluded = True

            entry_price = float(record["entry_price"])
            direction = record["direction"]

            # Calculate P&L points based on spot price unless the caller has a
            # more accurate economic result for a state whose fill is not the
            # realized profit (e.g. a trailed BE stop after T1).
            if pnl_points_override is not None:
                pnl = float(pnl_points_override)
            elif direction == "BULLISH":
                pnl = exit_price - entry_price
            else:
                pnl = entry_price - exit_price

            pnl = round(pnl, 2)
            
            score = None
            if final_state == "T2_HIT":
                score = 2
            elif final_state in ("T1_HIT", "STOPPED_OUT_AT_BE"):
                score = 1
            elif final_state in ("SL_HIT", "STOPPED_OUT"):
                score = 0

            update_data = {
                "exit_timestamp": event_ts,
                "exit_price": float(exit_price),
                "pnl_points": pnl,
                "score": score,
                "result_state": final_state
            }
            if time_metrics_excluded:
                update_data["time_metrics_excluded"] = True

            self.supabase.table("trade_analytics").update(update_data).eq("id", trade_id).execute()

            # Back-fill the ml_collection label columns. schema.sql always said
            # "Trade outcomes are back-filled when trades close" — the code to do
            # it was never written, so trade_id/trade_outcome/trade_pnl were NULL
            # on all 9,102 collected rows and the table could train nothing.
            #
            # Skipped when signal_id is NULL (log_signal failed): there is no row
            # to attribute the outcome to, and guessing one would poison the label.
            if mode == "greenfield":
                signal_val = record.get("signal_id")
                signal_col = "signal_id"
            else:
                signal_val = record.get("signal_uuid")
                signal_col = "signal_uuid"

            if signal_val is None:
                # Fallback to legacy logic for old rows when not in greenfield
                signal_id = record.get("signal_id")
                if signal_id is None:
                    return
                try:
                    res = self.supabase.table("ml_collection").update({
                        "trade_id": trade_id,
                        "trade_outcome": final_state,
                        "trade_pnl": pnl,
                        "trade_score": score,
                    }).eq("signal_id", str(signal_id)).execute()
                    if not res.data:
                        print(f"Storage: zero rows updated in ml_collection for signal_id {signal_id}")
                except Exception as ml_err:
                    print(f"Failed to back-fill ml_collection label: {ml_err}")
                return
            # Retry logic for ML outcome binding
            import time
            max_retries = 3
            for attempt in range(max_retries):
                try:
                    res = self.supabase.table("ml_collection").update({
                        "trade_outcome": final_state,
                        "trade_pnl": pnl,
                        "trade_score": score,
                    }).eq("trade_id", trade_id).eq(signal_col, str(signal_val)).execute()
                    if res.data:
                        break
                    else:
                        if attempt < max_retries - 1:
                            time.sleep(0.5 * (attempt + 1))
                        else:
                            print(f"Storage: Terminal reconciliation error - zero rows updated in ml_collection for trade {trade_id} after {max_retries} attempts")
                except Exception as ml_err:
                    print(f"Failed to back-fill ml_collection label: {ml_err}")
                    if attempt < max_retries - 1:
                        time.sleep(0.5 * (attempt + 1))

        try:
            loop = asyncio.get_running_loop()
            entry_future = self._entry_futures.pop(trade_id, None)
            if entry_future is not None and not entry_future.done():
                def _update_after_entry(_future):
                    try:
                        loop.run_in_executor(None, _update)
                    except RuntimeError:
                        pass

                entry_future.add_done_callback(_update_after_entry)
            else:
                loop.run_in_executor(None, _update)
        except RuntimeError:
            # No running loop (sync caller, tests, backfill scripts). Previously
            # this branch only printed, so log_exit silently did nothing at all
            # outside async context. Mirrors MLCollector._insert's fallback.
            #
            # Guarded separately: run_in_executor above only schedules the work,
            # so the outer handler never sees _update's own failures. Calling it
            # inline here would otherwise raise straight into the caller — a
            # different failure mode for the same function depending on context.
            try:
                _update()
            except Exception as e:  # pragma: no cover
                print(f"Failed to log trade analytics exit: {e}")  # pragma: no cover
        except Exception as e:  # pragma: no cover
            print(f"Failed to log trade analytics exit: {e}")  # pragma: no cover


def load_dhan_credentials_from_supabase() -> None:
    """
    Fetch Dhan client_id and access_token from Supabase and update settings.
    """
    from supabase import create_client
    from config import settings

    print("[+] Connecting to Supabase to fetch Dhan credentials...")
    supabase = create_client(settings.supabase_url, settings.supabase_key)
    response = supabase.table("api_keys").select("client_id, access_token").eq("provider", "DHAN").execute()
    if not response.data:
        raise ValueError("No DHAN credentials found in Supabase api_keys table")

    data = response.data[0]
    settings.dhan_client_id = data["client_id"]
    settings.dhan_access_token = data["access_token"]
    print("[+] Successfully loaded Dhan credentials from Supabase.")


def _sanitize_value(val: Any) -> Any:
    """Helper to convert NumPy and special values into PostgreSQL JSONB safe types."""
    if val is None:
        return None
    if isinstance(val, datetime):
        return to_utc_iso(val)
    if isinstance(val, (float, np.floating)):
        f_val = float(val)
        if math.isnan(f_val) or math.isinf(f_val):
            return None
        return round(f_val, 6)
    if isinstance(val, (int, np.integer)):
        return int(val)
    if isinstance(val, (bool, np.bool_)):
        return bool(val)
    if isinstance(val, dict):
        return {str(k): _sanitize_value(v) for k, v in val.items()}
    if isinstance(val, (list, tuple, set, np.ndarray)):
        return [_sanitize_value(x) for x in val]
    return str(val) if not isinstance(val, str) else val


def sanitize_feature_snapshot(features: Dict[str, Any]) -> Dict[str, Any]:
    """Sanitizes feature dictionary for safe JSONB serialization in PostgreSQL.

    Rules:
    - NumPy float/int converted to built-in float/int.
    - NaN and Inf converted to None (JSON null).
    - Float values rounded to 6 decimal places.
    - Datetime objects converted to UTC ISO-8601 strings.
    """
    if not isinstance(features, dict):
        return {}
    return {str(k): _sanitize_value(v) for k, v in features.items()}


@dataclass
class PredictionRecord:
    """Represents a validated, strongly-typed ML prediction record."""
    probability: float
    confidence_tier: str
    model_version: str
    spot: float
    feature_snapshot: Dict[str, Any]
    signal_id: Optional[str] = None
    trade_id: Optional[str] = None
    source: str = "event_triggered"
    timestamp: Optional[Union[datetime, str]] = None


class PredictionLogger:
    """Handles asynchronous, non-blocking persistence of ML predictions to Supabase."""

    def __init__(
        self,
        supabase_client: Optional[Client] = None,
        supabase_url: Optional[str] = None,
        supabase_service_role_key: Optional[str] = None,
        max_workers: int = 2,
    ):
        if supabase_client is not None:
            # Enforce backend-only credential contract: reject anon-key client
            anon_key = getattr(settings, "supabase_key", None)
            client_key = getattr(supabase_client, "supabase_key", None)
            if anon_key and client_key and client_key == anon_key:
                raise ValueError(
                    "PredictionLogger requires supabase_service_role_key; anon key client is not permitted."
                )
            self.supabase = supabase_client
        else:
            url = supabase_url or getattr(settings, "supabase_url", "")
            service_key = (
                supabase_service_role_key
                or getattr(settings, "supabase_service_role_key", None)
            )
            if not service_key:
                raise ValueError(
                    "PredictionLogger requires supabase_service_role_key; anon key is not permitted."
                )
            self.supabase = create_client(url, service_key)

        self._executor = ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix="ml_pred_logger",
        )

    def log_record(self, record: PredictionRecord) -> None:
        """Dispatches an insert using a PredictionRecord instance."""
        self.log_prediction(
            probability=record.probability,
            confidence_tier=record.confidence_tier,
            model_version=record.model_version,
            spot=record.spot,
            feature_snapshot=record.feature_snapshot,
            signal_id=record.signal_id,
            trade_id=record.trade_id,
            source=record.source,
            timestamp=record.timestamp,
        )

    def log_prediction(
        self,
        probability: float,
        confidence_tier: str,
        model_version: str,
        spot: float,
        feature_snapshot: Dict[str, Any],
        signal_id: Optional[str] = None,
        trade_id: Optional[str] = None,
        source: str = "event_triggered",
        timestamp: Optional[Union[datetime, str]] = None,
    ) -> None:
        """Dispatches an asynchronous insert to ml_predictions.

        Guaranteed non-blocking and safe against all runtime exceptions.
        """
        try:
            ts = timestamp or datetime.now(timezone.utc)
            record = {
                "timestamp": to_utc_iso(ts),
                "probability": float(probability),
                "confidence_tier": str(confidence_tier),
                "model_version": str(model_version),
                "signal_id": str(signal_id) if signal_id is not None else None,
                "trade_id": str(trade_id) if trade_id is not None else None,
                "spot": float(spot),
                "source": str(source),
                "feature_snapshot": sanitize_feature_snapshot(feature_snapshot),
            }

            try:
                asyncio.get_running_loop()
            except RuntimeError:
                # Synchronous fallback when called outside an active event loop
                self._insert_prediction(record)
                return

            self._executor.submit(self._insert_prediction, record)
        except Exception as exc:
            logger.warning("PredictionLogger: dispatch error: %s", exc)

    def _insert_prediction(self, record: Dict[str, Any]) -> None:
        try:
            self.supabase.table("ml_predictions").insert(record).execute()
        except Exception as exc:
            logger.warning("PredictionLogger: failed to persist prediction to ml_predictions: %s", exc)

    def shutdown(self, wait: bool = True) -> None:
        """Shut down the background executor."""
        self._executor.shutdown(wait=wait)
