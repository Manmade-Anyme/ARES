"""
Backtest Engine
===============
Replays historical OHLCV data through the ARES detectors to generate signals.

Since historical data does not include live option chain / OI information,
this engine runs a simplified pipeline:
  1. ExhaustionDetector  — fully functional (needs only OHLCV)
  2. FailedBreakoutDetector — uses PDH/PDL levels from daily data
  3. OIWallDetector — SKIPPED (requires live option chain)
"""

from collections import deque
from datetime import datetime, timedelta
from statistics import mean
from typing import List, Optional

import pandas as pd
import numpy as np

from models import OHLCVCandle, AresSignal, ResistanceLevel
from config import settings
from detectors.breakout import FailedBreakoutDetector
from detectors.exhaustion import ExhaustionDetector


class BacktestEngine:
    """
    Replays historical candle data through the ARES detection pipeline.

    For each trading day in the dataset:
      1. Compute PDH/PDL from the prior day's daily data
      2. Build structural levels (PDH, PDL, round numbers)
      3. Feed each intraday candle through the detectors
      4. Collect all generated signals

    The engine resets detector state at the start of each trading day
    to match the behaviour of a fresh live session.
    """

    def __init__(self):
        self.breakout_detector = FailedBreakoutDetector()
        self.exhaustion_detector = ExhaustionDetector()
        self.candle_buffer: deque = deque(maxlen=settings.candle_buffer_size)
        self.last_signal_time: Optional[datetime] = None

    def _reset_session(self) -> None:
        """Reset all detector state for a new trading day."""
        self.breakout_detector = FailedBreakoutDetector()
        self.exhaustion_detector = ExhaustionDetector()
        self.candle_buffer.clear()
        self.last_signal_time = None

    @staticmethod
    def _build_levels(pdh: float, pdl: float, spot: float) -> List[ResistanceLevel]:
        """
        Build structural support/resistance levels from PDH/PDL and
        round-number psychological levels.
        """
        levels = []

        if pdh > 0:
            levels.append(ResistanceLevel(price=pdh, source="prev_day_high", strength=3))
        if pdl > 0:
            levels.append(ResistanceLevel(price=pdl, source="prev_day_low", strength=3))

        # Add round-number levels within ±500 of spot (psychological S/R)
        interval = settings.strike_interval  # typically 50
        base = int(spot / interval) * interval
        for offset in range(-10, 11):
            lvl_price = float(base + offset * interval)
            if lvl_price > 0 and abs(lvl_price - spot) <= 500:
                levels.append(ResistanceLevel(
                    price=lvl_price, source="round_number", strength=1
                ))

        # Deduplicate by price, keeping highest strength
        deduped = {}
        for lvl in levels:
            if lvl.price not in deduped or lvl.strength > deduped[lvl.price].strength:
                deduped[lvl.price] = lvl

        return sorted(deduped.values(), key=lambda l: l.price)

    def run(
        self,
        candles_df: pd.DataFrame,
        daily_df: Optional[pd.DataFrame] = None,
        warmup_candles: int = 5,
    ) -> List[AresSignal]:
        """
        Execute the full backtest replay.

        Args:
            candles_df:      Intraday OHLCV DataFrame (timestamp, OHLCV).
            daily_df:        Daily OHLCV DataFrame for PDH/PDL. If None,
                             PDH/PDL is computed from the intraday data itself.
            warmup_candles:  Skip the first N candles per day to fill buffers.

        Returns:
            List of AresSignal objects generated during the replay.
        """
        if candles_df.empty:
            return []

        signals: List[AresSignal] = []

        # Pre-compute daily high/low lookup if daily_df provided
        daily_lookup = {}
        if daily_df is not None and not daily_df.empty:
            for _, row in daily_df.iterrows():
                d = pd.Timestamp(row["timestamp"]).date()
                daily_lookup[d] = (float(row["high"]), float(row["low"]))

        # Group intraday candles by date
        candles_df = candles_df.copy()
        candles_df["date"] = pd.to_datetime(candles_df["timestamp"]).dt.date
        grouped = candles_df.groupby("date")
        sorted_dates = sorted(grouped.groups.keys())

        prev_day_high = 0.0
        prev_day_low = 0.0

        for date_idx, current_date in enumerate(sorted_dates):
            day_candles = grouped.get_group(current_date).sort_values("timestamp")
            self._reset_session()

            # Get PDH/PDL for today from prior day
            if date_idx > 0:
                prev_date = sorted_dates[date_idx - 1]
                if prev_date in daily_lookup:
                    prev_day_high, prev_day_low = daily_lookup[prev_date]
                else:
                    # Compute from intraday data of previous day
                    prev_candles = grouped.get_group(prev_date)
                    prev_day_high = float(prev_candles["high"].max())
                    prev_day_low = float(prev_candles["low"].min())

            candle_count = 0
            for _, row in day_candles.iterrows():
                candle_count += 1

                candle = OHLCVCandle(
                    timestamp=pd.Timestamp(row["timestamp"]).to_pydatetime(),
                    open=float(row["open"]),
                    high=float(row["high"]),
                    low=float(row["low"]),
                    close=float(row["close"]),
                    volume=int(row["volume"]),
                )

                self.candle_buffer.append(candle)

                # Skip warmup
                if candle_count <= warmup_candles:
                    continue

                # Build levels
                levels = self._build_levels(prev_day_high, prev_day_low, candle.close)

                # Cooldown check
                if self.last_signal_time:
                    elapsed = candle.timestamp - self.last_signal_time
                    if elapsed < timedelta(minutes=settings.signal_cooldown_minutes):
                        continue

                # Compute avg volume
                avg_volume = mean([c.volume for c in self.candle_buffer]) if self.candle_buffer else float(candle.volume)

                # Run Exhaustion detector (IV approximated as 0 change)
                signal = self.exhaustion_detector.update(
                    candle=candle, iv_current=0.0, iv_prev=0.0, levels=levels
                )

                # Run FailedBreakout detector (OI approximated as stable)
                if not signal:
                    signal = self.breakout_detector.update(
                        candle=candle,
                        avg_volume=avg_volume,
                        iv_change_pct=0.0,
                        atm_ce_oi=100000, atm_ce_oi_prev=100000,
                        atm_pe_oi=100000, atm_pe_oi_prev=100000,
                        levels=levels,
                    )

                if signal:
                    self.last_signal_time = candle.timestamp
                    signals.append(signal)

        return signals
