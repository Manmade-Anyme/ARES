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
from typing import Any
from supabase import create_client, Client

from models import AresSignal
from config import settings


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

    async def log_signal(self, signal: AresSignal, spot: float) -> None:
        """
        Asynchronously logs a signal to the 'ares_signals' table in Supabase.
        
        This method suppresses any exceptions so that database connectivity issues
        do not crash the main trading loop.
        
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
                "timestamp": signal.timestamp.isoformat()
            }
            # Execute the insert
            self.supabase.table("ares_signals").insert(data).execute()

        try:
            # Run the synchronous Supabase insert in an executor to avoid blocking the event loop
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, _insert)
        except Exception as e:
            # Print the error, but do NOT raise it
            print(f"Failed to log signal to Supabase: {e}")


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
            except Exception as e:
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

        data = {
            "id": trade_id,
            "setup_type": signal.setup_type.value,
            "direction": signal.direction.value,
            "entry_timestamp": signal.timestamp.isoformat(),
            "entry_price": float(spot),
            "result_state": "OPEN",
            "market_context": market_context,
            "oi_data": oi_data
        }

        def _insert():
            self.supabase.table("trade_analytics").insert(data).execute()

        try:
            loop = asyncio.get_running_loop()
            loop.run_in_executor(None, _insert)
        except Exception as e:
            print(f"Failed to log trade analytics entry: {e}")

    def log_exit(self, trade_id: str, exit_price: float, final_state: str) -> None:
        """
        Updates an existing entry in 'trade_analytics' with exit details.
        
        Args:
            trade_id: The unique UUID of the trade.
            exit_price: The spot price at exit.
            final_state: The final state of the trade (e.g., SL_HIT, T1_HIT).
        """
        from datetime import datetime, timezone
        
        def _update():
            # First, fetch the entry price to calculate P&L
            response = self.supabase.table("trade_analytics").select("entry_price", "direction").eq("id", trade_id).execute()
            if not response.data:
                return

            record = response.data[0]
            entry_price = float(record["entry_price"])
            direction = record["direction"]
            
            # Calculate P&L points based on spot price
            if direction == "BULLISH":
                pnl = exit_price - entry_price
            else:
                pnl = entry_price - exit_price

            update_data = {
                "exit_timestamp": datetime.now(timezone.utc).isoformat(),
                "exit_price": float(exit_price),
                "pnl_points": round(pnl, 2),
                "result_state": final_state
            }
            
            self.supabase.table("trade_analytics").update(update_data).eq("id", trade_id).execute()

        try:
            loop = asyncio.get_running_loop()
            loop.run_in_executor(None, _update)
        except Exception as e:
            print(f"Failed to log trade analytics exit: {e}")


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

