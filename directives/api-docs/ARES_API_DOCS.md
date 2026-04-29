# ARES API Documentation

This directory contains the auto-generated or consolidated documentation for the core classes and interfaces inside the ARES trading system.

## Ingestion Layer (Fetchers)

### `PriceFetcher` (in `fetchers/price_fetcher.py`)
Responsible for fetching 1-minute intraday candles for the NIFTY 50 index using the DhanHQ API. Maintains session accumulators for calculating the Volume Weighted Average Price (VWAP) incrementally.

**Methods:**
- `reset_vwap() -> None`: Resets cumulative VWAP accumulators to 0. Called at the start of every trading session.
- `fetch_latest_candle() -> OHLCVCandle`: Fetches the most recently completed 1-minute candle from Dhan API and computes the incremental VWAP.
- `fetch_previous_day_ohlc() -> Tuple[float, float]`: Fetches the previous trading day's high and low for NIFTY 50 from Yahoo Finance's free public chart endpoint (fallback/primary oracle).

### `OIFetcher` (in `fetchers/oi_fetcher.py`)
Fetches the NIFTY option chain from the Dhan API every cycle. It tracks the previous cycle's Open Interest (OI) for each strike and side to compute the OI change percentage dynamically.

**Methods:**
- `get_nearest_expiry() -> str`: Fetches the nearest expiry date from the Dhan API. Result is cached per day.
- `fetch_chain(spot_price: float, expiry: str) -> Tuple[ATMStrikes, List[Dict[str, Any]]]`: Fetches the current option chain, calculates OI changes, and builds ATM strikes and the full chain.

### `LevelFetcher` (in `fetchers/level_fetcher.py`)
Aggregates and formats critical price levels (like PDH, PDL, VWAP) into a standardized `ResistanceLevel` format.

## Orchestration Layer (Engine)

### `AresEngine` (in `engine.py`)
The main orchestrator. It holds state (recent candles, IV history) and coordinates the fetchers, detectors, and alerts.

**Methods:**
- `poll()`: The main loop method. Coordinates fetching data, checking cooldowns, running detectors, and dispatching alerts.

## Detection Layer (Detectors)

### `FailedBreakoutDetector` (in `detectors/breakout.py`)
Tracks "fake-outs" where price crosses a significant level but fails to hold. Stateful detector.

### `OIWallDetector` (in `detectors/oi_wall.py`)
Identifies structural rejection at strikes with massive fresh Open Interest. Stateless detector.

### `ExhaustionDetector` (in `detectors/exhaustion.py`)
Catches "blow-off tops" or "panic bottoms" using volume/price divergence.

## Models (in `models.py`)

- `OHLCVCandle`: Dataclass representing a single 1-minute candle (open, high, low, close, volume, vwap, timestamp).
- `OptionRow`: Dataclass for a single option contract (strike, type, ltp, iv, oi, oi_prev, oi_change_pct, gamma, theta).
- `ATMStrikes`: Container for the ATM Call and Put `OptionRow`s.
- `ResistanceLevel`: Dataclass defining a support/resistance level (price, type, strength).
- `AresSignal`: Output dataclass generated when a detector finds a setup (setup_type, direction, confidence, entry, stop_loss, targets, reasons).

## Broadcasting Layer

### `alerts.py`
Formats and dispatches Discord notifications asynchronously.

**Functions:**
- `format_signal(signal: AresSignal, spot: float) -> str`: Formats the AresSignal into a clear, scannable Discord message.
- `send_discord(signal: AresSignal, spot: float) -> None`: Dispatches the formatted signal to Discord.
- `send_startup_alert(pdh: float, pdl: float) -> None`: Sends an initialization message to Discord at startup.
- `send_error_alert(error_msg: str) -> None`: Sends system-level error alerts to Discord.
