import threading
from typing import Optional

from dhanhq import DhanContext, MarketFeed

from config import settings


class TickFeed:
    """
    Thin wrapper around dhanhq's WebSocket MarketFeed exposing the latest
    NIFTY spot LTP for tick-driven exit checks between the 60s REST poll
    cycles (TASK-173, audit item 18).

    Subscribes in Ticker mode only (lowest-bandwidth — exit checks just need
    LTP). Runs on dhanhq's own background thread. Best-effort throughout:
    any connect/parse failure is caught so the caller can fall back to
    REST-only polling instead of crashing the main loop.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._latest_price: Optional[float] = None
        self._feed: Optional[MarketFeed] = None

    def _on_ticks(self, feed, data) -> None:
        try:
            if isinstance(data, dict) and data.get("LTP") is not None:
                price = float(data["LTP"])
                with self._lock:
                    self._latest_price = price
        except (TypeError, ValueError):
            pass

    def _on_error(self, feed, error) -> None:
        print(f"[-] TickFeed: WebSocket error: {error}")

    def start(self) -> bool:
        """Connect and subscribe to the configured spot instrument. Returns
        True on success, False if the feed could not be started (caller
        should continue on REST-only exit monitoring in that case)."""
        try:
            context = DhanContext(settings.dhan_client_id, settings.dhan_access_token)
            instruments = [(MarketFeed.IDX, int(settings.security_id), MarketFeed.Ticker)]
            feed = MarketFeed(context, instruments, on_ticks=self._on_ticks, on_error=self._on_error)
            feed.start()
            self._feed = feed
            return True
        except Exception as e:
            print(f"[-] TickFeed: failed to start WebSocket feed ({e}). Falling back to REST-only exit monitoring.")
            self._feed = None
            return False

    def stop(self) -> None:
        if self._feed is not None:
            try:
                self._feed.close_connection()
            except Exception:
                pass
            self._feed = None

    def get_latest_price(self) -> Optional[float]:
        with self._lock:
            return self._latest_price

    @property
    def is_active(self) -> bool:
        return self._feed is not None
