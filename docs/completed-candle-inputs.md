# Completed candle input contract

`PriceFetcher.fetch_latest_candle()` returns the latest newly completed one-minute
bar from the current IST trading session, or `None` when no fresh completed bar is
available. Candle timestamps denote bar starts and remain naive IST for existing
engine callers. A bar is complete only when its start plus one minute is no later
than the response sampling time. Future, prior-session and out-of-session bars do
not enter the engine. Duplicate timestamps retain the latest valid provider row.

VWAP is rebuilt on every usable response from all unique completed session bars
returned by Dhan, including history before process startup. Forming bars are
excluded. Revised provider rows replace prior values instead of accumulating
again. All-zero volume falls back to the selected bar's typical price. A session
change or explicit `reset_vwap()` clears the emitted-bar marker.

The watch evidence retains forming bars and explicit response-sample provenance.
When no fresh completed bar is available, the main loop waits through its existing
tick exit and pending order monitoring path; it does not tick detectors or collect
duplicate signal snapshots. API errors and malformed arrays remain errors.

This repair does not change Friday's strategy conditions, thresholds, confidence
calibration or option execution behavior. Detector buffers still require fresh
bars after a restart; historical candles are not replayed into live signal state.
