"""
Data Store
==========

Provides local Parquet-based caching for historical OHLCV data.
Follows the Data Engineering principle of immutable raw storage:
  - Raw API data is saved as-is under data/raw/
  - Cleaned / processed data lives under data/clean/

This avoids redundant API calls and provides fast local replay.
"""

import os
from pathlib import Path
from typing import Optional

import pandas as pd


# ─── Default storage root relative to project ──────────────────────────────
DATA_DIR = Path(__file__).resolve().parent.parent / "data"
RAW_DIR = DATA_DIR / "raw"
CLEAN_DIR = DATA_DIR / "clean"


class DataStore:
    """
    Manages Parquet-based local storage for historical candle data.
    
    Directory layout:
        data/
          raw/          ← Immutable copies of every API fetch
          clean/        ← Processed, deduplicated, forward-filled datasets
    """

    def __init__(self, base_dir: Optional[Path] = None):
        """
        Initialize the store and ensure directories exist.
        
        Args:
            base_dir: Override the default data directory (useful for tests).
        """
        self.base = base_dir or DATA_DIR
        self.raw_dir = self.base / "raw"
        self.clean_dir = self.base / "clean"
        self.raw_dir.mkdir(parents=True, exist_ok=True)
        self.clean_dir.mkdir(parents=True, exist_ok=True)

    def _make_key(self, symbol: str, interval: str, from_date: str, to_date: str) -> str:
        """Build a filename key from query parameters."""
        safe_from = from_date.replace(" ", "_").replace(":", "-")
        safe_to = to_date.replace(" ", "_").replace(":", "-")
        return f"{symbol}_{interval}_{safe_from}_to_{safe_to}"

    # ── Save / Load Raw ─────────────────────────────────────────────────────

    def save_raw(self, df: pd.DataFrame, symbol: str, interval: str,
                 from_date: str, to_date: str) -> Path:
        """
        Save a raw DataFrame to Parquet (immutable snapshot).
        
        Returns:
            Path to the saved file.
        """
        key = self._make_key(symbol, interval, from_date, to_date)
        path = self.raw_dir / f"{key}.parquet"
        df.to_parquet(path, index=False, engine="pyarrow")
        return path

    def load_raw(self, symbol: str, interval: str,
                 from_date: str, to_date: str) -> Optional[pd.DataFrame]:
        """
        Attempt to load a previously-fetched raw dataset.
        
        Returns:
            DataFrame if found, else None.
        """
        key = self._make_key(symbol, interval, from_date, to_date)
        path = self.raw_dir / f"{key}.parquet"
        if path.exists():
            return pd.read_parquet(path, engine="pyarrow")
        return None

    # ── Save / Load Clean ────────────────────────────────────────────────────

    def save_clean(self, df: pd.DataFrame, name: str) -> Path:
        """Save a clean/processed DataFrame."""
        path = self.clean_dir / f"{name}.parquet"
        df.to_parquet(path, index=False, engine="pyarrow")
        return path

    def load_clean(self, name: str) -> Optional[pd.DataFrame]:
        """Load a clean/processed DataFrame."""
        path = self.clean_dir / f"{name}.parquet"
        if path.exists():
            return pd.read_parquet(path, engine="pyarrow")
        return None

    # ── Cleaning Pipeline ────────────────────────────────────────────────────

    @staticmethod
    def clean(df: pd.DataFrame) -> pd.DataFrame:
        """
        Data quality pipeline:
          1. Drop exact duplicate rows
          2. Drop rows with NaN prices
          3. Forward-fill isolated NaN volume bars
          4. Remove extreme outlier candles (>10% gap from neighbours)
          5. Sort by timestamp and reset index
          6. Downcast floats to float32 for memory efficiency
          
        Args:
            df: Raw OHLCV DataFrame.
            
        Returns:
            Cleaned DataFrame.
        """
        if df.empty:
            return df

        out = df.copy()

        # 1. Dedup
        out = out.drop_duplicates(subset=["timestamp"])

        # 2. Drop rows where any price column is NaN
        price_cols = ["open", "high", "low", "close"]
        out = out.dropna(subset=price_cols)

        # 3. Forward-fill missing volume
        out["volume"] = out["volume"].ffill().fillna(0).astype("int64")

        # 4. Remove extreme outliers (close price jumps > 10% from rolling median)
        if len(out) > 5:
            rolling_median = out["close"].rolling(5, min_periods=1, center=True).median()
            pct_diff = ((out["close"] - rolling_median) / rolling_median).abs()
            out = out[pct_diff < 0.10]

        # 5. Sort and reset
        out = out.sort_values("timestamp").reset_index(drop=True)

        # 6. Downcast for memory
        for col in price_cols:
            out[col] = out[col].astype("float32")

        return out
