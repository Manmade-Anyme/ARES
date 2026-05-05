# Ingestion Layer (Fetchers)

The Ingestion Layer is responsible for pulling all necessary data asynchronously from the DhanHQ API and Yahoo Finance. 

## Structure
The fetchers are located in the `fetchers/` directory.

### `price_fetcher.py`
- Connects to DhanHQ to retrieve 1-minute historical and current OHLCV data.
- Calculates VWAP incrementally.
- Also fetches Previous Day High (PDH) and Previous Day Low (PDL) dynamically via Yahoo Finance to construct base structural levels.

### `oi_fetcher.py`
- Connects to DhanHQ to pull the full Option Chain for the active NIFTY expiry.
- Parses CE and PE data into `OptionRow` models (see [[02_Models]]).
- Calculates Open Interest (OI) changes.

### `level_fetcher.py`
- Aggregates structural levels (like PDH/PDL from the PriceFetcher) and dynamic levels (like major OI Walls from the OIFetcher).
- Outputs a list of `ResistanceLevel` objects that the [[05_Detectors]] use for targeting and rejection logic.

## Usage in Pipeline
The fetchers are orchestrated by `main.py` before passing the resulting data into the [[04_Engine|AresEngine]].
