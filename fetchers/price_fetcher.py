import asyncio
from datetime import datetime
from typing import Dict, Any, Tuple

import httpx

from dhanhq import dhanhq

from models import OHLCVCandle
from config import settings


class PriceFetcher:
    """
    PriceFetcher is responsible for fetching 1-minute intraday candles
    for the NIFTY 50 index using the DhanHQ API.
    
    It maintains session accumulators for calculating the Volume Weighted Average Price (VWAP)
    incrementally over the trading day.
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

    def reset_vwap(self) -> None:
        """
        Reset both cumulative typical-price-volume and volume accumulators to 0.
        This must be called at the start of every trading session (e.g., at 09:15 IST)
        so that the VWAP calculation is fresh for the day.
        """
        self.cumulative_tp_vol = 0.0
        self.cumulative_vol = 0

    async def fetch_latest_candle(self) -> OHLCVCandle:
        """
        Fetch the most recently completed 1-minute candle from Dhan API
        and compute the incremental VWAP based on the accumulated session volume.
        
        VWAP accumulation logic:
            - Typical Price = (High + Low + Close) / 3
            - Increment cumulative volume by the candle's volume
            - Increment cumulative (typical price * volume)
            - VWAP = cumulative (typical price * volume) / cumulative volume
            
        Returns:
            OHLCVCandle: The most recently completed candle with VWAP populated.
            
        Raises:
            ValueError: If the response data from Dhan is empty or malformed.
        """
        loop = asyncio.get_running_loop()
        
        today = datetime.now().strftime("%Y-%m-%d")
        response: Dict[str, Any] = await loop.run_in_executor(
            None,
            lambda: self.dhan.intraday_minute_data(
                settings.security_id,
                settings.exchange_segment,
                settings.instrument_type,
                today,
                today
            )
        )
        
        if not response or "data" not in response:
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
                error_msg = f"Unknown API error. Raw response: {response}"
                
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
            
        # Extract the last candle from the arrays
        ts_val = data[time_key][-1]
        
        try:
            # Dhan start_Time could be an epoch integer or a string
            if isinstance(ts_val, (int, float)):
                # Dhan sometimes returns epoch with milliseconds or seconds
                # standardizing to seconds if it looks like milliseconds
                if ts_val > 1e11:
                    ts_val /= 1000.0
                timestamp = datetime.fromtimestamp(ts_val)
            else:
                # Basic string parsing fallback
                try:
                    timestamp = datetime.strptime(str(ts_val), "%Y-%m-%d %H:%M:%S")
                except ValueError:
                    from dateutil import parser
                    timestamp = parser.parse(str(ts_val))
        except Exception as e:
            raise ValueError(f"Could not parse timestamp from API: {ts_val}") from e
            
        open_price = float(data["open"][-1])
        high_price = float(data["high"][-1])
        low_price = float(data["low"][-1])
        close_price = float(data["close"][-1])
        volume = int(data["volume"][-1])
        
        # Compute incremental VWAP
        typical_price = (high_price + low_price + close_price) / 3.0
        
        self.cumulative_tp_vol += typical_price * volume
        self.cumulative_vol += volume
        
        if self.cumulative_vol > 0:
            vwap = self.cumulative_tp_vol / self.cumulative_vol
        else:
            vwap = typical_price
            
        return OHLCVCandle(
            timestamp=timestamp,
            open=open_price,
            high=high_price,
            low=low_price,
            close=close_price,
            volume=volume,
            vwap=vwap
        )

    async def fetch_previous_day_ohlc(self) -> Tuple[float, float]:
        """
        Fetch the previous trading day's high and low for NIFTY 50 
        from Yahoo Finance's free public chart endpoint to ensure reliability.
        
        Returns:
            Tuple[float, float]: (previous_day_high, previous_day_low)
        """
        url = f"https://query2.finance.yahoo.com/v8/finance/chart/{settings.yahoo_symbol}?range=5d&interval=1d"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
        }
        async with httpx.AsyncClient() as client:
            response = await client.get(url, headers=headers, timeout=10.0)
            response.raise_for_status()
            data = response.json()
            
            result = data.get("chart", {}).get("result", [])
            if not result:
                raise ValueError("Could not parse chart result from Yahoo Finance.")
                
            quote = result[0].get("indicators", {}).get("quote", [])
            if not quote:
                raise ValueError("Could not parse quote indicators from Yahoo Finance.")
                
            highs = quote[0].get("high", [])
            lows = quote[0].get("low", [])
            timestamps = result[0].get("timestamp", [])
            
            valid_indexes = [i for i in range(len(timestamps)) if highs[i] is not None and lows[i] is not None]
            if not valid_indexes:
                raise ValueError("No valid daily candle data found.")
            
            from datetime import datetime, timezone, timedelta
            # IST offset
            now_ist_date = (datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)).date()
            
            target_idx = -1
            for idx in reversed(valid_indexes):
                ts = timestamps[idx]
                dt_ist = (datetime.fromtimestamp(ts, tz=timezone.utc) + timedelta(hours=5, minutes=30)).date()
                if dt_ist < now_ist_date:
                    target_idx = idx
                    break
                    
            if target_idx == -1:
                target_idx = valid_indexes[-1]
                
            def round_to_tick(val: float, tick_size: float = 0.05) -> float:
                return round(val / tick_size) * tick_size
                
            pdh = round_to_tick(float(highs[target_idx]))
            pdl = round_to_tick(float(lows[target_idx]))
            return pdh, pdl
