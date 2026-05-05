# Architecture Overview

ARES follows a strict **five-layer architecture** designed for modularity, performance, and fault tolerance.

## 1. 🧩 Ingestion Layer ([[03_Fetchers_and_Ingestion]])
Asynchronous fetchers (`PriceFetcher`, `OIFetcher`) poll the DhanHQ API for 1-min candles and real-time Option Chains. They also fetch dynamic levels (PDH/PDL).

## 2. ⚙️ Orchestration Layer ([[04_Engine]])
The `AresEngine` manages the evaluation pipeline. It maintains rolling state buffers (Volume, IV) and enforces signal cooldowns to prevent spam.

## 3. 🔬 Detection Layer ([[05_Detectors]])
A suite of specialized detectors score market conditions against technical and structural levels in priority order:
1. Failed Breakout
2. OI Wall Rejection
3. Exhaustion Reversal

## 4. 💾 Persistence Layer ([[07_Storage]])
All generated signals and active trade states are logged to Supabase (PostgreSQL) for post-session performance auditing and backtesting. Trade state is maintained via the [[06_Position_Manager]].

## 5. 📢 Broadcasting Layer
Signals are formatted into rich, scannable alerts and dispatched via Discord Webhooks and the local console. The `alerts.py` module handles formatting and dispatching.

## Data Flow
1. `main.py` triggers the `tick` every minute.
2. Fetchers grab the latest candle, option chain, and levels.
3. The data is passed to `AresEngine.tick()`.
4. Buffers are updated.
5. Detectors are run in sequence until a signal is generated.
6. If a signal is generated, it is passed to the `PositionManager` and `Storage`, and broadcasted to Discord.
7. During the next ticks, `PositionManager` continuously evaluates the live spot price against trailing stops.
