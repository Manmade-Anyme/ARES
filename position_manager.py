import asyncio
import uuid
from datetime import datetime, timezone, timedelta
from supabase import create_client, Client
from typing import List, Dict, Any, Tuple

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

    def add_trade(self, signal: AresSignal, spot: float, atm: ATMStrikes = None):
        """
        Formats an AresSignal, pushes it to Supabase as OPEN, and stores it in memory.
        Also logs entry to the permanent trade_analytics table.
        """
        trade_id = str(uuid.uuid4())
        trade_data = {
            "id": trade_id,
            "signal_id": getattr(signal, "signal_id", f"{__import__('random').randint(0, 9999):04d}"),
            "setup_type": signal.setup_type.value,
            "direction": signal.direction.value,
            "entry_price": float(spot),
            "stop_loss": float(signal.stop_loss),
            "target_1": float(signal.target_1),
            "target_2": float(signal.target_2),
            "state": "OPEN",
            # We explicitly set created_at so we can reliably parse it later
            "created_at": datetime.now(timezone.utc).isoformat(),
            "added_time_ist": datetime.now(timezone(timedelta(hours=5, minutes=30))).strftime("%H:%M:%S")
        }
        
        # Add to memory list
        self.active_trades.append(trade_data)
        
        # Push to Supabase asynchronously to avoid blocking the main event loop
        def _insert():
            self.supabase.table("active_trades").insert(trade_data).execute()
            
        try:
            loop = asyncio.get_running_loop()
            loop.run_in_executor(None, _insert)
        except Exception as e:
            print(f"Failed to push new trade to Supabase: {e}")
            
        # 2. Log to permanent Analytics table
        try:
            self.analytics.log_entry(trade_id, signal, spot, atm)
        except Exception as e:
            print(f"Failed to log trade to Analytics: {e}")

    def _apply_time_stop(self, trade: Dict[str, Any]) -> None:
        """
        Time-stop (TASK-171): an OPEN trade that hasn't reached T1 within
        `time_stop_minutes` gets its SL tightened to entry (risk-free). The
        trade stays alive — if momentum resumes it can still run to T1/T2 —
        but a no-progress drift now exits near breakeven instead of full SL.
        Exits caused by this tightening are labeled TIME_STOP, not T1_HIT.
        """
        created_at_str = trade.get("created_at")
        if not created_at_str or trade["state"] != "OPEN":
            return
        try:
            created_at = datetime.fromisoformat(created_at_str.replace("Z", "+00:00"))
            age_minutes = (datetime.now(timezone.utc) - created_at).total_seconds() / 60.0
        except Exception:
            return
        if age_minutes < settings.time_stop_minutes:
            return

        entry = trade["entry_price"]
        if trade["direction"] == "BULLISH" and trade["stop_loss"] < entry:
            trade["stop_loss"] = entry
            trade["_time_stopped"] = True
        elif trade["direction"] == "BEARISH" and trade["stop_loss"] > entry:
            trade["stop_loss"] = entry
            trade["_time_stopped"] = True

    async def update_trades(self, spot_price: float) -> List[Tuple[str, str]]:
        """
        Loops through active trades and evaluates live price action against the active trailing stops.
        If state changes or SL is hit, updates the row in Supabase and triggers a Discord alert.

        Returns a list of (trade_id, update_type) for every state change this
        tick, so the caller can react (e.g. clear the engine cooldown on SL_HIT).
        """
        events: List[Tuple[str, str]] = []
        if not self.is_initialized:
            self._initialize_db()
            if not self.is_initialized:
                return events  # Skip update if still not initialized

        for trade in self.active_trades:
            if trade["state"] in ["CLOSED", "STOPPED_OUT"]:
                continue

            self._apply_time_stop(trade)

            state_changed = False
            update_type = None
            direction = trade["direction"]

            # Evaluate trailing stop logic
            if direction == "BULLISH":
                # Check if T2 hit
                if spot_price >= trade["target_2"]:
                    trade["state"] = "CLOSED"
                    state_changed = True
                    update_type = "T2_HIT"
                # Check if T1 hit and we haven't trailed yet
                elif trade["state"] == "OPEN" and spot_price >= trade["target_1"]:
                    trade["state"] = "T1_HIT"
                    trade["stop_loss"] = trade["entry_price"]
                    trade.pop("_time_stopped", None)  # Real T1: no longer a time-stop exit
                    state_changed = True
                    update_type = "T1_HIT"
                # Check if SL hit
                elif spot_price <= trade["stop_loss"]:
                    trade["state"] = "CLOSED"
                    state_changed = True
                    if trade.get("_time_stopped"):
                        update_type = "TIME_STOP"  # Breakeven exit forced by time-stop, not a T1 win
                    elif trade["stop_loss"] == trade["entry_price"]:
                        update_type = "T1_HIT"  # Trailed SL hit, logged as a T1 win
                    else:
                        update_type = "SL_HIT"
            elif direction == "BEARISH":
                # Check if T2 hit
                if spot_price <= trade["target_2"]:
                    trade["state"] = "CLOSED"
                    state_changed = True
                    update_type = "T2_HIT"
                # Check if T1 hit and we haven't trailed yet
                elif trade["state"] == "OPEN" and spot_price <= trade["target_1"]:
                    trade["state"] = "T1_HIT"
                    trade["stop_loss"] = trade["entry_price"]
                    trade.pop("_time_stopped", None)  # Real T1: no longer a time-stop exit
                    state_changed = True
                    update_type = "T1_HIT"
                # Check if SL hit
                elif spot_price >= trade["stop_loss"]:
                    trade["state"] = "CLOSED"
                    state_changed = True
                    if trade.get("_time_stopped"):
                        update_type = "TIME_STOP"  # Breakeven exit forced by time-stop, not a T1 win
                    elif trade["stop_loss"] == trade["entry_price"]:
                        update_type = "T1_HIT"  # Trailed SL hit, logged as a T1 win
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

                # Update permanent Analytics table on exit
                if trade["state"] in ["CLOSED", "STOPPED_OUT"]:
                    try:
                        self.analytics.log_exit(trade["id"], spot_price, update_type)
                    except Exception as e:
                        print(f"Failed to log trade exit to Analytics: {e}")
                    
                # Send Discord alert
                try:
                    await send_trade_update(trade, spot_price, update_type)
                except Exception as e:
                    print(f"Failed to send trade update alert: {e}")

        return events
