import asyncio
from datetime import datetime, timedelta, timezone
from math import isfinite
from typing import Optional, Tuple

from dhanhq import dhanhq

from models import OHLCVCandle
from config import settings
from oi_watch_context import IST, build_watch_candles


class PriceFetcher:
    """
    PriceFetcher is responsible for fetching 1-minute intraday candles
    for the NIFTY 50 index using the DhanHQ API.
    
    It rebuilds session VWAP from completed provider bars on each response.
    """

    def __init__(self):
        """
        Initialize the Dhan API client using settings from config.
        Set cumulative VWAP accumulators to 0.
        """
        try:
            from dhanhq import DhanContext
            context = DhanContext(settings.dhan_client_id, settings.dhan_access_token)
            self.dhan = dhanhq(context)
        except ImportError:
            self.dhan = dhanhq(
                settings.dhan_client_id,
                settings.dhan_access_token
            )
        self.cumulative_tp_vol: float = 0.0
        self.cumulative_vol: int = 0
        self.watch_candles: list = []
        self._session_date = None
        self._last_emitted_timestamp = None

    def reset_vwap(self) -> None:
        """
        Reset both cumulative typical-price-volume and volume accumulators to 0.
        This must be called at the start of every trading session (e.g., at 09:15 IST)
        so that the VWAP calculation is fresh for the day.
        """
        self.cumulative_tp_vol = 0.0
        self.cumulative_vol = 0
        self._session_date = None
        self._last_emitted_timestamp = None

    async def fetch_latest_spot(self) -> float:
        """Fetch a fresh independent REST LTP; never substitute a candle price."""
        securities = {settings.exchange_segment: [int(settings.security_id)]}
        loop = asyncio.get_running_loop()
        try:
            response = await loop.run_in_executor(None, lambda: self.dhan.ticker_data(securities))
        except Exception as exc:
            raise ValueError("Dhan REST LTP request failed.") from exc
        if not isinstance(response, dict) or response.get("status") != "success":
            raise ValueError("Dhan REST LTP request returned a failed response.")
        data = response.get("data")
        if isinstance(data, dict) and "data" in data:
            if data.get("status", "success") != "success":
                raise ValueError("Dhan REST LTP payload returned a failed response.")
            data = data["data"]
        try:
            value = data[settings.exchange_segment][str(settings.security_id)]["last_price"]
            if isinstance(value, bool):
                raise ValueError("Boolean REST LTP is invalid.")
            spot = float(value)
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            raise ValueError("Dhan REST LTP response has no valid quote for the requested instrument.") from exc
        if not isfinite(spot) or spot <= 0:
            raise ValueError("Dhan REST LTP must be finite and positive.")
        return spot

    async def fetch_latest_candle(self) -> Optional[OHLCVCandle]:
        """
        Return the latest newly completed current-session candle, or None.

        VWAP uses all unique completed bars returned for the session, including
        history before startup. Provider revisions replace previous values;
        duplicate polls never advance the signal engine. Malformed API responses
        retain the existing ValueError contract.
        """
        loop = asyncio.get_running_loop()
        
        today = datetime.now(IST).strftime("%Y-%m-%d")
        
        max_retries = 3
        last_exception = None
        response = None
        
        for attempt in range(max_retries):
            try:
                response = await loop.run_in_executor(
                    None,
                    lambda: self.dhan.intraday_minute_data(
                        settings.security_id,
                        settings.exchange_segment,
                        settings.instrument_type,
                        today,
                        today
                    )
                )
                
                if response and response.get("status") == "success":
                    break
                
                # If we get a failure with no error message (transient Dhan backend issue)
                remarks = response.get("remarks") if response else None
                if isinstance(remarks, dict) and all(v is None for v in remarks.values()):
                    if attempt < max_retries - 1:
                        await asyncio.sleep(1.5 * (attempt + 1))
                        continue
                
                # If it's a real error with a message, we don't necessarily want to retry 
                # (e.g. invalid token), but for "Unknown" we should.
                if response and response.get("status") == "failure":
                    if attempt < max_retries - 1:
                        await asyncio.sleep(1.0 * (attempt + 1))
                        continue
                        
            except Exception as e:
                last_exception = e
                if attempt < max_retries - 1:
                    await asyncio.sleep(1.0 * (attempt + 1))
                    continue
                raise e

        if not response or "data" not in response:
            if last_exception:
                raise last_exception
            raise ValueError("Empty or missing 'data' in Dhan API response for intraday_minute_data.")
            
        has_error_type = isinstance(response.get("data"), dict) and "errorType" in response.get("data")
        if response.get("status") == "failure" or has_error_type:
            # Robust error extraction
            error_msg = "Unknown API error"
            error_type = ""
            
            remarks = response.get("remarks")
            if isinstance(remarks, dict):
                error_msg = remarks.get("error_message") or remarks.get("errorMessage") or error_msg
                error_type = remarks.get("error_type") or remarks.get("errorType") or error_type
            elif isinstance(remarks, str) and remarks:
                error_msg = remarks
                
            if error_msg == "Unknown API error":
                data_field = response.get("data")
                if isinstance(data_field, dict):
                    error_msg = data_field.get("errorMessage") or data_field.get("error_message") or data_field.get("message") or data_field.get("error") or error_msg
                    error_type = data_field.get("errorType") or data_field.get("error_type") or error_type
                elif isinstance(data_field, str) and data_field:
                    error_msg = data_field
                    
            if error_msg == "Unknown API error":
                error_msg = f"Unknown API error (Empty Remarks). Raw response: {response}"
                
            raise ValueError(f"Dhan API Error ({error_type}): {error_msg}")
            
        data = response.get("data")
        if not data or not isinstance(data, dict):
            raise ValueError(f"No intraday data returned or data is not a dictionary. Raw data: {data}")
        
        # Determine the time key used by Dhan (can be "start_Time" or "timestamp" depending on segment)
        time_key = "start_Time" if "start_Time" in data else "timestamp"

        # Verify all necessary keys are present
        required_keys = [time_key, "open", "high", "low", "close", "volume"]
        if not all(key in data for key in required_keys):
            raise ValueError(f"Malformed response: missing required keys from Dhan API. Found keys: {list(data.keys())}")
            
        # Ensure we have at least one data point
        if not data.get(time_key):
            raise ValueError("No intraday candle data points returned by Dhan API.")
            
        if any(not isinstance(data[key], (list, tuple)) for key in required_keys):
            raise ValueError("Malformed response: intraday candle arrays must be lists or tuples.")
        count = len(data[time_key])
        if any(len(data[key]) != count for key in required_keys):
            raise ValueError("Malformed response: intraday candle arrays must have equal lengths.")

        # Use a single response sample time for both trading and watch evidence.
        # The normalizer handles UTC epochs, exchange-local strings and revisions.
        sampled_at = datetime.now(timezone.utc)
        session_date = sampled_at.astimezone(IST).date()
        # Validate all completed source rows before mutating session/dedup state.
        watch_candles = build_watch_candles(data, sampled_at, strict_completed=True)
        if self._session_date != session_date:
            self.reset_vwap()
            self._session_date = session_date
        self.watch_candles = watch_candles
        completed = [row for row in self.watch_candles if row["complete"]]
        if not completed:
            return None

        latest = completed[-1]
        timestamp = datetime.fromisoformat(latest["timestamp"]).replace(tzinfo=None)
        self.cumulative_tp_vol = sum(
            (row["high"] + row["low"] + row["close"]) / 3.0 * row["volume"]
            for row in completed
        )
        self.cumulative_vol = sum(int(row["volume"]) for row in completed)
        if self._last_emitted_timestamp is not None and timestamp <= self._last_emitted_timestamp:
            return None
        typical_price = (latest["high"] + latest["low"] + latest["close"]) / 3.0
        vwap = self.cumulative_tp_vol / self.cumulative_vol if self.cumulative_vol else typical_price
        candle = OHLCVCandle(
            timestamp=timestamp, open=latest["open"], high=latest["high"],
            low=latest["low"], close=latest["close"], volume=int(latest["volume"]), vwap=vwap,
        )
        self._last_emitted_timestamp = timestamp
        return candle

    async def fetch_previous_day_ohlc(self) -> Tuple[float, float]:
        """
        Fetch the previous trading day's high and low for the target asset
        directly from the Dhan API using historical daily data.
        
        @returns {Tuple[float, float]} (previous_day_high, previous_day_low)
        @throws {ValueError} If the API response is malformed or historical data is insufficient.
        """
        loop = asyncio.get_running_loop()
        
        # Calculate dates for the last 7 days to ensure we capture the last trading session
        now = datetime.now()
        to_date = now.strftime("%Y-%m-%d")
        from_date = (now - timedelta(days=7)).strftime("%Y-%m-%d")

        max_retries = 3
        response = None
        for attempt in range(max_retries):
            try:
                response = await loop.run_in_executor(
                    None,
                    lambda: self.dhan.historical_daily_data(
                        security_id=settings.security_id,
                        exchange_segment=settings.exchange_segment,
                        instrument_type=settings.instrument_type,
                        from_date=from_date,
                        to_date=to_date
                    )
                )
                if response and response.get("status") == "success":
                    break
                if attempt < max_retries - 1:
                    await asyncio.sleep(1.0 * (attempt + 1))
            except Exception:
                if attempt < max_retries - 1:
                    await asyncio.sleep(1.0 * (attempt + 1))
                    continue
                raise

        if not response or response.get("status") != "success":
            error_msg = response.get("remarks") if response else "Empty response"
            raise ValueError(f"Failed to fetch historical daily data from Dhan: {error_msg}")
            
        data = response.get("data", {})
        highs = data.get("high", [])
        lows = data.get("low", [])
        timestamps = data.get("timestamp", [])
        
        if not highs or not lows or not timestamps:
            raise ValueError("Incomplete historical data returned from Dhan API.")
            
        # We want the last completed day. 
        # Typically, Dhan's historical_daily_data returns data up to the last closed session.
        # We verify if the last entry is for today.
        last_ts = timestamps[-1]
        last_date = datetime.fromtimestamp(last_ts).date()
        today_date = now.date()
        
        # If the last entry is today, we take the one before it (the actual "previous" day)
        # Otherwise, we take the last entry.
        idx = -1
        if last_date >= today_date:
            if len(highs) < 2:
                raise ValueError("Insufficient historical data to determine previous day levels.")
            idx = -2
            
        def round_to_tick(val: float, tick_size: float = 0.05) -> float:
            return round(val / tick_size) * tick_size
            
        pdh = round_to_tick(float(highs[idx]))
        pdl = round_to_tick(float(lows[idx]))
        
        return pdh, pdl
