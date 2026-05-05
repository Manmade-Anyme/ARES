# Detection Layer

The Detection Layer houses the core alpha-generating logic of ARES. It consists of three specialized detectors located in the `detectors/` directory.

## 1. Failed Breakout (Highest Priority)
Located in `detectors/breakout.py`.

* **Logic:** Tracks "fake-outs" where price crosses a significant level (like PDH/PDL or an OI wall) but fails to sustain the breakout, reversing back. This is a stateful detector tracking the breakout process over several candles.
* **Scoring (4-point scale):**
  * `Closed Back`: Price returned past the level (Required).
  * `Weak Volume`: Breakout candle volume < Rolling Average.
  * `IV Crush`: Dropping Implied Volatility during the cross.
  * `OI Defense`: Option writers held or increased their positions at the breakout strike.
* **Targets:** Dynamically targets the next structural support/resistance levels.

## 2. OI Wall Rejection (High Priority)
Located in `detectors/oi_wall.py`.

* **Logic:** Identifies structural rejection at strikes with massive fresh Open Interest.
* **Confirmation:** Detects price "bounces" or "wick rejections" when the spot price tests a strike where the OI significantly exceeds a configured threshold (`OI_WALL_MIN_OI`). It is a stateless structural detector.

## 3. Exhaustion Reversal (Medium Priority)
Located in `detectors/exhaustion.py`.

* **Logic:** Catches "blow-off tops" or "panic bottoms" using volume and price divergence.
* **Triggers:** Fires when a volume climax (extreme volume spike, e.g., > 2.5x average) coincides with a doji-like indecision candle (small body) at a local price extreme, often accompanied by an IV spike.
* **Targets:** Uses surrounding structural levels to define realistic profit booking zones.
