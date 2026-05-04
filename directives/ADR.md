# Architecture Decision Record (ADR): ARES Trading System

## 1. Architectural Style: Five-Layer Modular Design
We have adopted a layered architecture to separate concerns and ensure maintainability.

- **Layer 1: Ingestion (Fetchers)**
    - Uses `asyncio` and `httpx` to pull data from DhanHQ.
    - Decisions: Decouple price fetching from OI fetching to allow for independent polling intervals if needed.
- **Layer 2: Orchestration (Engine)**
    - Centralizes the "tick" logic.
    - Decisions: Maintains stateful rolling buffers (Volume, IV) to provide context to detectors.
- **Layer 3: Detection (Detectors)**
    - Pure logic layer.
    - Decisions: Short-circuit evaluation (Priority: Breakout > OI Wall > Exhaustion) to prevent signal overlap and ensure the highest confidence setup is prioritized.
- **Layer 4: Persistence (Storage)**
    - Supabase (PostgreSQL).
    - Decisions: Execute DB calls in an executor to prevent blocking the main event loop.
- **Layer 5: Broadcasting (Alerts)**
    - Discord and Console.
    - Decisions: Pre-format messages into rich Markdown for readability on mobile/desktop.
- **Layer 6: Position Management (Tracking)**
    - Managed by `PositionManager`.
    - Decisions: Persistent state tracking using Supabase. Real-time evaluation of trailing stops to entry (T1 logic) to minimize risk.
- **Layer 7: Analysis (Backtesting)**
    - Managed by `backtest/` suite.
    - Decisions: Export historical results to TradingView PineScript (v6) for visual audit.

## 2. Tech Stack Decisions
- **Python 3.10+**: Chosen for `asyncio` support and robust library ecosystem.
- **Pydantic-Settings**: Used for a "fail-fast" configuration. The system should crash on startup if credentials or critical thresholds are missing.
- **Python Dataclasses**: Used for domain models to ensure O(1) attribute access and low memory overhead during high-frequency polling.
- **Supabase**: Chosen for its easy-to-use API and reliable PostgreSQL backend, facilitating remote logging.

## 3. Key Design Patterns
- **Stateless Detectors (mostly)**: Detectors primarily evaluate the current state provided by the engine. The `FailedBreakout` detector is the only stateful component, tracking active level crosses across multiple candles.
- **Domain-Driven Models**: Everything from a single candle (`OHLCVCandle`) to a full signal (`AresSignal`) is represented by a strict dataclass to avoid "dictionary-passing" anti-patterns.
- **Zero Hardcoded Values**: All detection parameters (ratios, offsets, thresholds) are moved to environment variables.

## 4. Error Handling
- **Non-Fatal DB Errors**: Database logging failures should never crash the engine; they are caught and logged to console.
- **Fatal Auth Errors**: API authentication failures (401) trigger a longer wait period and a specific alert, as they require human intervention.
