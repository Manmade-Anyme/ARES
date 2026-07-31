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

    def add_trade(self, signal: AresSignal, spot: float, atm: ATMStrikes = None):
        """
        Formats an AresSignal, pushes it to Supabase as OPEN, and stores it in memory.
        Also logs entry to the permanent trade_analytics table.

        Duplicate guard (TASK-172, audit item 13): the same setup firing twice
        in quick succession produced double rows (06-29 14:12 OI wall logged
        twice). A signal matching an already-tracked open trade's setup,
        direction and entry (within 1 pt) is skipped.
        """
        for existing in self.active_trades:
            if existing.get("state") in ["CLOSED", "STOPPED_OUT"]:
                continue
            if (existing.get("setup_type") == signal.setup_type.value
                    and existing.get("direction") == signal.direction.value
                    and abs(float(existing.get("entry_price", 0.0)) - float(spot)) <= settings.trade_dedupe_tolerance_pts):
                print(f"[-] PositionManager: Duplicate {signal.setup_type.value} ({signal.direction.value}) trade at {spot:.2f} skipped — already tracking {existing['id']}.")
                return

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
          - Pessimistic resolution: if one candle touches both the stop and a
            target, the stop is assumed to have filled first.

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

            self._apply_time_stop(trade)

            state_changed = False
            update_type = None
            event_price = spot_price
            direction = trade["direction"]

            # Evaluate trailing stop logic
            if direction == "BULLISH":
                # Check SL first (pessimistic when the candle spans both)
                if low <= trade["stop_loss"]:
                    trade["state"] = "CLOSED"
                    state_changed = True
                    event_price = trade["stop_loss"]
                    if trade.get("_time_stopped"):
                        update_type = "TIME_STOP"  # Breakeven exit forced by time-stop, not a T1 win
                    elif trade["stop_loss"] == trade["entry_price"]:
                        update_type = "STOPPED_OUT_AT_BE"  # Trailed SL hit, logged as break-even exit
                    else:
                        update_type = "SL_HIT"
                # Check if T2 hit
                elif high >= trade["target_2"]:
                    trade["state"] = "CLOSED"
                    state_changed = True
                    update_type = "T2_HIT"
                    event_price = trade["target_2"]
                # Check if T1 hit and we haven't trailed yet
                elif trade["state"] == "OPEN" and high >= trade["target_1"]:
                    trade["state"] = "T1_HIT"
                    trade["stop_loss"] = trade["entry_price"]
                    trade.pop("_time_stopped", None)  # Real T1: no longer a time-stop exit
                    state_changed = True
                    update_type = "T1_HIT"
                    event_price = trade["target_1"]
            elif direction == "BEARISH":
                # Check SL first (pessimistic when the candle spans both)
                if high >= trade["stop_loss"]:
                    trade["state"] = "CLOSED"
                    state_changed = True
                    event_price = trade["stop_loss"]
                    if trade.get("_time_stopped"):
                        update_type = "TIME_STOP"  # Breakeven exit forced by time-stop, not a T1 win
                    elif trade["stop_loss"] == trade["entry_price"]:
                        update_type = "STOPPED_OUT_AT_BE"  # Trailed SL hit, logged as break-even exit
                    else:
                        update_type = "SL_HIT"
                # Check if T2 hit
                elif low <= trade["target_2"]:
                    trade["state"] = "CLOSED"
                    state_changed = True
                    update_type = "T2_HIT"
                    event_price = trade["target_2"]
                # Check if T1 hit and we haven't trailed yet
                elif trade["state"] == "OPEN" and low <= trade["target_1"]:
                    trade["state"] = "T1_HIT"
                    trade["stop_loss"] = trade["entry_price"]
                    trade.pop("_time_stopped", None)  # Real T1: no longer a time-stop exit
                    state_changed = True
                    update_type = "T1_HIT"
                    event_price = trade["target_1"]

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
                        self.analytics.log_exit(trade["id"], event_price, update_type)
                    except Exception as e:
                        print(f"Failed to log trade exit to Analytics: {e}")

                # Send Discord alert
                try:
                    await send_trade_update(trade, event_price, update_type)
                except Exception as e:
                    print(f"Failed to send trade update alert: {e}")

        return events
