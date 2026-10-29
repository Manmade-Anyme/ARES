import threading
from collections import deque
from datetime import datetime, timedelta, timezone
from math import isfinite
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
        self._latest_received_at: Optional[datetime] = None
        self._feed: Optional[MarketFeed] = None
        self._ticks = deque(maxlen=50000)
        self._tick_sequence = 0
        self._read_sequence = 0

    @staticmethod
    def _now_utc():
        return datetime.now(timezone.utc)

    @staticmethod
    def _market_time(value, received_at):
        """Decode the SDK's UTC LTT string or the API's epoch timestamp."""
        try:
            if isinstance(value, bool) or value is None:
                return None
            if isinstance(value, (int, float)):
                result = datetime.fromtimestamp(float(value), timezone.utc)
            elif isinstance(value, datetime):
                result = value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
            else:
                text = str(value).strip()
                try:
                    result = datetime.fromisoformat(text.replace("Z", "+00:00"))
                    result = result.replace(tzinfo=timezone.utc) if result.tzinfo is None else result.astimezone(timezone.utc)
                except ValueError:
                    parsed_time = datetime.strptime(text, "%H:%M:%S").time()
                    result = datetime.combine(received_at.date(), parsed_time, tzinfo=timezone.utc)
                    if (result - received_at).total_seconds() > 12 * 3600:
                        result -= timedelta(days=1)
                    elif (received_at - result).total_seconds() > 12 * 3600:
                        result += timedelta(days=1)
            return result
        except (TypeError, ValueError, OverflowError, OSError):
            return None

    def _on_ticks(self, feed, data) -> None:
        try:
            if isinstance(data, dict) and data.get("LTP") is not None:
                if isinstance(data["LTP"], bool):
                    return
                price = float(data["LTP"])
                if not isfinite(price) or price <= 0:
                    return
                received_at = self._now_utc()
                market_at = self._market_time(data.get("LTT"), received_at)
                with self._lock:
                    if self._latest_received_at is not None and received_at <= self._latest_received_at:
                        received_at = self._latest_received_at + timedelta(microseconds=1)
                    self._latest_price = price
                    self._latest_received_at = received_at
                    self._tick_sequence += 1
                    self._ticks.append({
                        "sequence": self._tick_sequence, "price": price,
                        "received_at": received_at,
                        "exchange_at": market_at,
                        "source": "dhan_websocket_tick" if market_at is not None else "dhan_websocket_receipt",
                    })
        except (TypeError, ValueError, OverflowError):
            pass

    def _on_error(self, feed, error) -> None:
        print(f"[-] TickFeed: WebSocket error: {error}")

    def start(self) -> bool:
        """Connect and subscribe to the configured spot instrument. Returns
        True on success, False if the feed could not be started (caller
        should continue on REST-only exit monitoring in that case)."""
        try:
            context = DhanContext(settings.dhan_client_id, settings.dhan_access_token)
            instruments = [(MarketFeed.IDX, str(settings.security_id), MarketFeed.Ticker)]
            feed = MarketFeed(context, instruments, on_ticks=self._on_ticks, on_error=self._on_error)
            self._feed = feed
            feed.start()
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

    def get_latest_observation(self, max_age_seconds: float = 5) -> Optional[tuple[float, datetime]]:
        """Return a recent valid index LTP with its local UTC receipt time.

        Receipt time is the observation timestamp, never an invented exchange
        event time. A stale feed must not advance the new option lifecycle.
        """
        if isinstance(max_age_seconds, bool) or not isfinite(max_age_seconds) or max_age_seconds < 0:
            return None
        with self._lock:
            price, received_at = self._latest_price, self._latest_received_at
        if price is None or received_at is None:
            return None
        age = (datetime.now(timezone.utc) - received_at).total_seconds()
        if not 0 <= age <= max_age_seconds:
            return None
        return price, received_at

    def get_observations_since(self):
        """Return each buffered tick once in WebSocket arrival order."""
        with self._lock:
            observations = [dict(item) for item in self._ticks if item["sequence"] > self._read_sequence]
            self._read_sequence = self._tick_sequence
        return observations

    def get_complete_minute_observations(self, source_minute, *, now=None):
        """Dhan Ticker mode has no sequence watermark to prove a complete minute.

        Keep ambiguity unresolved: socket continuity and surrounding ticks cannot
        prove that no threshold-crossing tick was omitted by the provider/network.
        A future feed adapter may implement a certified history source here.
        """
        return [], False

    @property
    def is_active(self) -> bool:
        return self._feed is not None
