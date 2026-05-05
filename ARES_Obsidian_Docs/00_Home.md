# ARES (Adaptive Reversal & Entry Signal) 🚀

Welcome to the ARES knowledge base. ARES is a high-performance, asynchronous algorithmic trading signal system designed specifically for **NIFTY 50 options scalping**. It continuously monitors 1-minute price action, Option Interest (OI), and Implied Volatility (IV) to identify high-probability reversal and continuation setups.

## Core Documentation Navigation

- [[01_Architecture|Architecture Overview]] - Understand the five-layer architecture.
- [[02_Models|Domain Models]] - Learn about the core data structures (OHLCVCandle, AresSignal, etc.).
- [[03_Fetchers_and_Ingestion|Ingestion Layer (Fetchers)]] - How ARES pulls data from DhanHQ.
- [[04_Engine|Orchestration Layer (Engine)]] - The brain of the system, managing buffers and cooldowns.
- [[05_Detectors|Detection Layer]] - In-depth look at Failed Breakout, OI Wall Rejection, and Exhaustion detectors.
- [[06_Position_Manager|Trade Management (Position Manager)]] - How active trades and trailing stops are managed.
- [[07_Storage|Persistence Layer (Storage)]] - Database interactions with Supabase.
- [[08_Configuration|Configuration & Settings]] - Environment variables and threshold parameters.

## Operational Parameters

- **Session Window:** Actively polls from **09:20 to 15:25 IST** (Standard NSE session).
- **Warmup State:** Requires **30 candles** (configurable) to fill rolling volume/IV buffers before generating signals.
- **Signal Cooldown:** Enforces a strict **5-minute cooldown** between alerts to prevent over-trading.

## Tech Stack Overview
- **Language:** Python 3.10+ (Asyncio-driven)
- **Broker API:** DhanHQ Python SDK
- **Database:** Supabase (PostgreSQL)
- **Deployment:** Dockerized for Fly.io
