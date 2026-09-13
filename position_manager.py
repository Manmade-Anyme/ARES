import asyncio
import uuid
from datetime import datetime, timezone, timedelta
from supabase import create_client, Client
from typing import List, Dict, Any, Optional, Tuple

from models import AresSignal, ATMStrikes
from config import settings
from alerts import send_trade_update
from storage import AnalyticsLogger


class PositionManager:
    """
    Manages active trades, evaluates trailing stops based on live spot price,
    and persists state to Supabase.
    
    Implements lazy initialization: if the database is unreachable on startup,
    it will retry connecting during the update loop until successful.
    """
    def __init__(self):
        self.supabase: Client = create_client(
            settings.supabase_url,
            settings.supabase_key
        )
        self.analytics = AnalyticsLogger()
        self.active_trades: List[Dict[str, Any]] = []
        self.is_initialized = False
        self._initialize_db()

    def _initialize_db(self):
        """
        Loads all non-closed trades from Supabase into the active memory list,
        regardless of the date they were opened. Trades ride across sessions
        until they hit T1/T2/SL. Closed rows stay in the table untouched
        (trade_analytics remains the permanent record).
        """
        try:
            response = self.supabase.table("active_trades").select("*").execute()
            records = response.data

            valid_trades = [
                record for record in records
                if record.get("state") not in ["CLOSED", "STOPPED_OUT"]
            ]

            self.active_trades = valid_trades
            self.is_initialized = True
            print(f"PositionManager initialized. Loaded {len(self.active_trades)} active trades (multi-day carry enabled).")
        except Exception as e:
            self.is_initialized = False
            print(f"Failed to initialize PositionManager DB: {e}")

    async def add_trade(self, signal: AresSignal, spot: float, atm: ATMStrikes = None) -> tuple[str, str]:
        """
        Formats an AresSignal, pushes it to Supabase as OPEN using atomic RPC,
        and stores it in memory. Returns (trade_id, binding_status).
        """
        for existing in self.active_trades:
            if existing.get("state") in ["CLOSED", "STOPPED_OUT"]:
                continue
            if (existing.get("setup_type") == signal.setup_type.value
                    and existing.get("direction") == signal.direction.value
                    and abs(float(existing.get("entry_price", 0.0)) - float(spot)) <= settings.trade_dedupe_tolerance_pts):
                print(f"[-] PositionManager: Duplicate {signal.setup_type.value} ({signal.direction.value}) trade at {spot:.2f} skipped — already tracking {existing['id']}.")
                return "", "DUPLICATE_SKIPPED" 

        import uuid
        trade_id = str(uuid.uuid5(uuid.NAMESPACE_OID, str(signal.id)))
        
        # Prepare RPC payload
        trade_data = {
            "id": trade_id,
            "signal_id": getattr(signal, "db_id", None),
            "signal_uuid": str(signal.id),
            "setup_type": signal.setup_type.value,
            "direction": signal.direction.value,
            "entry_price": float(spot),
            "stop_loss": float(signal.stop_loss),
            "target_1": float(signal.target_1),
            "target_2": float(signal.target_2),
            "state": "OPEN",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "added_time_ist": datetime.now(timezone(timedelta(hours=5, minutes=30))).strftime("%H:%M:%S")
        }

        # Market context for analytics
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
        if getattr(signal, "oi_wall_context", None) is not None:
            market_context["oi_wall"] = signal.oi_wall_context

        analytics_data = {
            "id": trade_id,
            "signal_id": getattr(signal, "db_id", None),
            "signal_uuid": str(signal.id),
            "setup_type": signal.setup_type.value,
            "direction": signal.direction.value,
            "entry_timestamp": __import__("storage").to_utc_iso(signal.timestamp),
            "entry_price": float(spot),
            "result_state": "OPEN",
            "market_context": market_context,
            "oi_data": {} # Fetching atm data would go here, we mock it empty for atomic insert
        }

        def _insert():
            payload = {
                "p_trade_id": trade_id,
                "p_signal_id": getattr(signal, "db_id", None),
                "p_signal_uuid": str(signal.id),
                "p_setup_type": signal.setup_type.value,
                "p_direction": signal.direction.value,
                "p_entry_price": float(spot),
                "p_stop_loss": float(signal.stop_loss),
                "p_target_1": float(signal.target_1),
                "p_target_2": float(signal.target_2),
                "p_added_time_ist": trade_data["added_time_ist"],
                "p_market_context": market_context,
                "p_oi_data": {},
                "p_entry_timestamp": analytics_data["entry_timestamp"]
            }
            res = self.supabase.rpc("create_trade_entry_bridge", payload).execute()
            # If RPC succeeds, it returns the inserted active_trades row or something similar
            return True

        import asyncio
        loop = asyncio.get_running_loop()
        import time
        for attempt in range(3):
            try:
                success = await loop.run_in_executor(None, _insert)
                if success:
                    self.active_trades.append(trade_data)
                    return trade_id, "BOUND"
            except Exception as e:
                print(f"Failed to push new trade to Supabase (attempt {attempt+1}): {e}")
                if attempt == 2:
                    raise RuntimeError("Database insertion failed for atomic trade entry after 3 attempts")
                await asyncio.sleep(1)
        
        return "", "FAILED"

    async def update_trades(
        self,
        spot_price: float,
        candle_high: Optional[float] = None,
        candle_low: Optional[float] = None,
    ) -> List[Tuple[str, str]]:
        """
        Loops through active trades and evaluates live price action against the active trailing stops.
        If state changes or SL is hit, updates the row in Supabase and triggers a Discord alert.

        Intrabar detection (TASK-172, audit item 11): SL/T1/T2 touches are
        checked against the candle's high/low when provided, not just the poll
        close — close-only detection cost an average +6.8 pts of slippage per
        stop. Falls back to spot_price when candle extremes are omitted.
        Two accounting rules follow from this:
          - Fill-at-level: events are reported/logged at the touched level
            (stop or target price), not at the close that detected them.
          - Optimistic resolution: if one candle touches both the stop and a
            target, the target is assumed to have filled first (wicks that
            touch targets are counted as wins).

        Returns a list of (trade_id, update_type) for every state change this
        tick, so the caller can react (e.g. clear the engine cooldown on SL_HIT).
        """
        events: List[Tuple[str, str]] = []
        if not self.is_initialized:
            self._initialize_db()
            if not self.is_initialized:
                return events  # Skip update if still not initialized

        high = candle_high if candle_high is not None else spot_price
        low = candle_low if candle_low is not None else spot_price

        for trade in self.active_trades:
            if trade["state"] in ["CLOSED", "STOPPED_OUT"]:
                continue

            state_changed = False
            update_type = None
            event_price = spot_price
            direction = trade["direction"]

            # Evaluate trailing stop logic
            if direction == "BULLISH":
                # Check if T2 hit
                if high >= trade["target_2"]:
                    trade["state"] = "CLOSED"
                    state_changed = True
                    update_type = "T2_HIT"
                    event_price = trade["target_2"]
                # Check if T1 hit and we haven't trailed yet
                elif trade["state"] == "OPEN" and high >= trade["target_1"]:
                    trade["state"] = "T1_HIT"
                    trade["stop_loss"] = trade["entry_price"]
                    state_changed = True
                    update_type = "T1_HIT"
                    event_price = trade["target_1"]
                # Check SL (optimistic when the candle spans both)
                elif low <= trade["stop_loss"]:
                    trade["state"] = "CLOSED"
                    state_changed = True
                    event_price = trade["stop_loss"]
                    if trade["stop_loss"] == trade["entry_price"]:
                        update_type = "STOPPED_OUT_AT_BE"  # Trailed SL hit, logged as break-even exit
                    else:
                        update_type = "SL_HIT"
            elif direction == "BEARISH":
                # Check if T2 hit
                if low <= trade["target_2"]:
                    trade["state"] = "CLOSED"
                    state_changed = True
                    update_type = "T2_HIT"
                    event_price = trade["target_2"]
                # Check if T1 hit and we haven't trailed yet
                elif trade["state"] == "OPEN" and low <= trade["target_1"]:
                    trade["state"] = "T1_HIT"
                    trade["stop_loss"] = trade["entry_price"]
                    state_changed = True
                    update_type = "T1_HIT"
                    event_price = trade["target_1"]
                # Check SL (optimistic when the candle spans both)
                elif high >= trade["stop_loss"]:
                    trade["state"] = "CLOSED"
                    state_changed = True
                    event_price = trade["stop_loss"]
                    if trade["stop_loss"] == trade["entry_price"]:
                        update_type = "STOPPED_OUT_AT_BE"  # Trailed SL hit, logged as break-even exit
                    else:
                        update_type = "SL_HIT"

            if state_changed:
                events.append((trade["id"], update_type))

                # Prepare update payload
                update_data = {
                    "state": trade["state"],
                    "stop_loss": trade["stop_loss"]
                }
                
                # Push update to Supabase asynchronously
                def _update(t_id=trade["id"], data=update_data):
                    self.supabase.table("active_trades").update(data).eq("id", t_id).execute()
                    
                try:
                    loop = asyncio.get_running_loop()
                    loop.run_in_executor(None, _update)
                except Exception as e:
                    print(f"Failed to update trade in Supabase: {e}")

                # Update permanent Analytics table on exit (fill-at-level:
                # the exit is recorded at the touched stop/target price)
                if trade["state"] in ["CLOSED", "STOPPED_OUT"]:
                    try:
                        if update_type == "STOPPED_OUT_AT_BE":
                            if direction == "BULLISH":
                                pnl_points_override = trade["target_1"] - trade["entry_price"]
                            else:
                                pnl_points_override = trade["entry_price"] - trade["target_1"]
                            self.analytics.log_exit(
                                trade["id"],
                                event_price,
                                update_type,
                                pnl_points_override=pnl_points_override,
                            )
                        else:
                            self.analytics.log_exit(trade["id"], event_price, update_type)
                    except Exception as e:
                        print(f"Failed to log trade exit to Analytics: {e}")

                # Send Discord alert
                try:
                    await send_trade_update(trade, event_price, update_type)
                except Exception as e:
                    print(f"Failed to send trade update alert: {e}")

        return events
