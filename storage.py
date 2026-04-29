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
                "reasons": signal.reasons,  # Supabase handles list -> jsonb serialization
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
