"""
Historical Data Fetcher
========================

Fetches daily and intraday historical OHLCV data from the Dhan API v2.
Implements pagination for intraday data (90-day chunks) and rate limiting.

Dhan API endpoints:
  - Daily:     POST https://api.dhan.co/v2/charts/historical
  - Intraday:  POST https://api.dhan.co/v2/charts/intraday
"""

import time
import asyncio
from datetime import datetime, timedelta
from typing import Dict, Any, List, Optional

import httpx
import pandas as pd
import numpy as np

from config import settings


# ─── Constants ──────────────────────────────────────────────────────────────
DHAN_DAILY_URL = "https://api.dhan.co/v2/charts/historical"
DHAN_INTRADAY_URL = "https://api.dhan.co/v2/charts/intraday"

# Dhan allows 90 days per intraday request
INTRADAY_CHUNK_DAYS = 89

# Rate limit: max 5 requests per second to be safe
RATE_LIMIT_DELAY = 0.25


class HistoricalFetcher:
    """
    Fetches historical OHLCV data from the Dhan API v2.
    
    Supports both daily candles (unlimited history) and intraday candles
    (1, 5, 15, 25, 60 minute intervals, up to 5 years).
    
    Handles automatic pagination for intraday requests that exceed the
    90-day per-request limit imposed by Dhan.
    """

    def __init__(self):
        """Initialize with Dhan API credentials from settings."""
        self.headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "access-token": settings.dhan_access_token,
        }
        self._last_request_time: float = 0.0

    async def _rate_limit(self) -> None:
        """Enforce minimum delay between API requests to avoid throttling."""
        elapsed = time.monotonic() - self._last_request_time
        if elapsed < RATE_LIMIT_DELAY:
            await asyncio.sleep(RATE_LIMIT_DELAY - elapsed)
        self._last_request_time = time.monotonic()

    def _parse_response(self, data: Dict[str, Any]) -> pd.DataFrame:
        """
        Parse the Dhan columnar OHLCV response into a pandas DataFrame.
        
        Dhan returns data as parallel arrays:
          { open: [...], high: [...], low: [...], close: [...], volume: [...], timestamp: [...] }
        
        Args:
            data: The raw JSON response dict from Dhan.
            
        Returns:
            DataFrame with columns: timestamp, open, high, low, close, volume
            
        Raises:
            ValueError: If the response lacks required fields.
        """
        required = ["open", "high", "low", "close", "volume", "timestamp"]
        missing = [k for k in required if k not in data]
        if missing:
            raise ValueError(f"Dhan response missing keys: {missing}. Got: {list(data.keys())}")

        df = pd.DataFrame({
            "timestamp": pd.to_datetime(
                np.array(data["timestamp"], dtype=np.int64),
                unit="s",
                utc=True
            ).tz_convert("Asia/Kolkata").tz_localize(None),
            "open": np.array(data["open"], dtype=np.float64),
            "high": np.array(data["high"], dtype=np.float64),
            "low": np.array(data["low"], dtype=np.float64),
            "close": np.array(data["close"], dtype=np.float64),
            "volume": np.array(data["volume"], dtype=np.int64),
        })

        # Drop any rows where all price columns are zero (dead bars)
        price_cols = ["open", "high", "low", "close"]
        df = df[~(df[price_cols] == 0).all(axis=1)]

        return df.sort_values("timestamp").reset_index(drop=True)

    async def fetch_daily(
        self,
        from_date: str,
        to_date: str,
        security_id: Optional[str] = None,
        exchange_segment: Optional[str] = None,
        instrument: Optional[str] = None,
    ) -> pd.DataFrame:
        """
        Fetch daily candle data from Dhan API.
        
        Args:
            from_date: Start date in 'YYYY-MM-DD' format.
            to_date:   End date in 'YYYY-MM-DD' format.
            security_id:      Defaults to settings.security_id.
            exchange_segment: Defaults to settings.exchange_segment.
            instrument:       Defaults to settings.instrument_type.
            
        Returns:
            DataFrame with daily OHLCV data.
        """
        payload = {
            "securityId": security_id or settings.security_id,
            "exchangeSegment": exchange_segment or settings.exchange_segment,
            "instrument": instrument or settings.instrument_type,
            "expiryCode": 0,
            "oi": False,
            "fromDate": from_date,
            "toDate": to_date,
        }

        await self._rate_limit()

        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(DHAN_DAILY_URL, json=payload, headers=self.headers)
            resp.raise_for_status()
            data = resp.json()

        return self._parse_response(data)

    async def fetch_intraday(
        self,
        from_date: str,
        to_date: str,
        interval: str = "5",
        security_id: Optional[str] = None,
        exchange_segment: Optional[str] = None,
        instrument: Optional[str] = None,
    ) -> pd.DataFrame:
        """
        Fetch intraday candle data from Dhan API, with automatic pagination
        across 90-day windows.
        
        Args:
            from_date: Start datetime 'YYYY-MM-DD HH:MM:SS' or 'YYYY-MM-DD'.
            to_date:   End datetime 'YYYY-MM-DD HH:MM:SS' or 'YYYY-MM-DD'.
            interval:  Candle interval. One of '1', '5', '15', '25', '60'.
            security_id:      Defaults to settings.security_id.
            exchange_segment: Defaults to settings.exchange_segment.
            instrument:       Defaults to settings.instrument_type.
            
        Returns:
            Concatenated DataFrame of intraday OHLCV data across all chunks.
        """
        # Parse dates (accept both date-only and datetime strings)
        try:
            start = datetime.strptime(from_date, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            start = datetime.strptime(from_date, "%Y-%m-%d").replace(hour=9, minute=15, second=0)

        try:
            end = datetime.strptime(to_date, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            end = datetime.strptime(to_date, "%Y-%m-%d").replace(hour=15, minute=30, second=0)

        chunks: List[pd.DataFrame] = []
        chunk_start = start

        while chunk_start < end:
            chunk_end = min(chunk_start + timedelta(days=INTRADAY_CHUNK_DAYS), end)

            payload = {
                "securityId": security_id or settings.security_id,
                "exchangeSegment": exchange_segment or settings.exchange_segment,
                "instrument": instrument or settings.instrument_type,
                "interval": str(interval),
                "oi": False,
                "fromDate": chunk_start.strftime("%Y-%m-%d %H:%M:%S"),
                "toDate": chunk_end.strftime("%Y-%m-%d %H:%M:%S"),
            }

            await self._rate_limit()

            try:
                async with httpx.AsyncClient(timeout=30.0) as client:
                    resp = await client.post(DHAN_INTRADAY_URL, json=payload, headers=self.headers)
                    resp.raise_for_status()
                    data = resp.json()

                df_chunk = self._parse_response(data)
                if not df_chunk.empty:
                    chunks.append(df_chunk)
                    print(f"  ✓ Fetched {len(df_chunk)} candles: "
                          f"{chunk_start.strftime('%Y-%m-%d')} → {chunk_end.strftime('%Y-%m-%d')}")
            except Exception as e:
                print(f"  ✗ Error fetching chunk {chunk_start.strftime('%Y-%m-%d')} → "
                      f"{chunk_end.strftime('%Y-%m-%d')}: {e}")

            chunk_start = chunk_end + timedelta(days=1)

        if not chunks:
            return pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume"])

        result = pd.concat(chunks, ignore_index=True)
        result = result.drop_duplicates(subset=["timestamp"]).sort_values("timestamp").reset_index(drop=True)
        return result
