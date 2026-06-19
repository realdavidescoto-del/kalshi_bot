# Kalshi Bot — System Architecture

## Overview

A Python trading bot for Kalshi prediction markets. Monitors market data via WebSocket, evaluates macroeconomic signals via FRED API, applies risk controls, and executes trades (live or shadow mode).

---

## High-Level Data Flow

```
┌─────────────────────────────────────────────────────────────────────┐
│                        CLIENT LAYER                                  │
│  cli.py (argparse CLI)     main.py (daemon loop)                     │
└──────────────┬──────────────────────────┬───────────────────────────┘
               │                          │
               ▼                          ▼
┌──────────────────────────┐   ┌──────────────────────────┐
│     STRATEGY LAYER       │   │    MARKET DATA LAYER     │
│  strategy/macro_tracker  │   │  market/websocket_client │
│  ┌──────────────────┐    │   │  ┌────────────────────┐  │
│  │FredCalendarProv. │────┼──►│  │KalshiWebSocketCl.  │  │
│  │  (FRED API)      │    │   │  │  (Kalshi WS)       │  │
│  └────────┬─────────┘    │   │  └─────────┬──────────┘  │
│           │              │   │            │              │
│           ▼              │   │            ▼              │
│  ┌──────────────────┐    │   │  ┌────────────────────┐  │
│  │MacroTrackerStrat.│    │   │  │  LocalOrderBook    │  │
│  │  (signal gen)    │    │   │  │  (in-memory state) │  │
│  └────────┬─────────┘    │   │  └────────────────────┘  │
└───────────┼──────────────┘   └──────────────────────────┘
            │                          ▲
            ▼                          │
┌──────────────────────────────────────────────────────────────────────┐
│                      RISK & SAFETY LAYER                             │
│  safety/risk_manager.py    safety/kill_switch.py                     │
│  ┌──────────────────┐      ┌────────────────────┐                    │
│  │Kelly Criterion   │      │Balance Monitoring  │                    │
│  │VaR Cap (2%)      │      │Order Cancellation  │                    │
│  │Sector Limit(30%) │      │(Kalshi REST API)   │                    │
│  └────────┬─────────┘      └────────────────────┘                    │
└───────────┼──────────────────────────────────────────────────────────┘
            │
            ▼
┌──────────────────────────────────────────────────────────────────────┐
│                     EXECUTION LAYER                                   │
│  execution/engine.py     execution/fee_tracker.py                    │
│  execution/order_state.py execution/position_manager.py              │
│  ┌──────────────────────────────────────────────────────────────┐    │
│  │  ExecutionEngine                                              │    │
│  │  ┌─────────┐  ┌──────────────┐  ┌─────────────────────────┐  │    │
│  │  │Signing  │  │Rate Limiter  │  │Circuit Breaker          │  │    │
│  │  │(RSA-SHA)│─►│(Token Bucket)│─►│(3/5 failures → open)   │─►│───►│───► Kalshi REST API
│  │  └─────────┘  └──────────────┘  └─────────────────────────┘  │    │
│  │                                       │                       │    │
│  │                                       ▼                       │    │
│  │                              ┌────────────────┐              │    │
│  │                              │Dead Letter Q   │              │    │
│  │                              │(failed orders) │              │    │
│  │                              └────────────────┘              │    │
│  └──────────────────────────────────────────────────────────────┘    │
└──────────────────────────────────────────────────────────────────────┘
            │
            ▼ (if shadow mode)
┌──────────────────────────────────────────────────────────────────────┐
│                     PERSISTENCE LAYER                                 │
│  data/database.py (SQLite)                                           │
│  ┌──────────────────────────────────────────────────────┐            │
│  │  Tables:                                              │            │
│  │  ┌──────────────┐  ┌──────────────┐  ┌────────────┐ │            │
│  │  │macro_releases│──│shadow_trades │  │market_data │ │            │
│  │  │id PRIMARY KEY│  │id PRIMARY KEY│  │_history    │ │            │
│  │  │indicator     │  │ticker        │  │id PK       │ │            │
│  │  │release_date  │  │action        │  │ticker      │ │            │
│  │  │actual_value  │  │outcome_side  │  │best_bid    │ │            │
│  │  │forecast_value│  │price/quantity│  │best_ask    │ │            │
│  │  │previous_value│  │release_id FK─┤  │source      │ │            │
│  │  └──────────────┘  └──────────────┘  └────────────┘ │            │
│  └──────────────────────────────────────────────────────┘            │
└──────────────────────────────────────────────────────────────────────┘
            │
            ▼
┌──────────────────────────────────────────────────────────────────────┐
│                    OBSERVABILITY LAYER                                │
│  observability/metrics.py   observability/logging_config.py          │
│  observability/health.py    observability/alerts.py                  │
│  ┌──────────────────────────────────────────────┐                    │
│  │Health HTTP Server (:8080)  │ Metrics         │                    │
│  │Structured Logging (file+stdout)               │                    │
│  └──────────────────────────────────────────────┘                    │
└──────────────────────────────────────────────────────────────────────┘
```

---

## Module Map

| Module | Responsibility | Key Classes | External Deps |
|--------|---------------|-------------|---------------|
| `main.py` | Daemon entry, main loop, graceful shutdown | — | psutil |
| `cli.py` | CLI interface for manual operations | — | — |
| `config.py` | Env loading, RSA key mgmt, retry logic | `Config` | dotenv, cryptography |
| `market/order_book.py` | In-memory bid/ask tracking | `LocalOrderBook` | — |
| `market/websocket_client.py` | WS auth, subscription, reconnection | `KalshiWebSocketClient` | websocket-client, cryptography |
| `strategy/macro_tracker.py` | Macro signal gen, FRED polling | `MacroTrackerStrategy`, `FredCalendarProvider`, `MockCalendarProvider` | requests |
| `safety/risk_manager.py` | Kelly sizing, VaR cap, sector limits | `RiskManager` | — |
| `safety/kill_switch.py` | Balance monitor, order cancellation | `KillSwitch` | cryptography |
| `execution/engine.py` | Order signing, placement, shadow mode | `ExecutionEngine` | cryptography |
| `execution/fee_tracker.py` | Rounding accumulator, rebate triggers | `FeeAccumulatorTracker` | — |
| `execution/order_state.py` | Order lifecycle state machine | `OrderStateMachine`, `Order` | — |
| `execution/position_manager.py` | Position tracking, PnL calc | `PositionManager`, `Position` | — |
| `data/database.py` | SQLite persistence | — | sqlite3 |
| `resilience/circuit_breaker.py` | API circuit breaker pattern | `CircuitBreaker`, `CircuitBreakerRegistry` | — |
| `resilience/rate_limiter.py` | Token bucket rate limiting | `TokenBucketRateLimiter`, `MultiTierRateLimiter` | — |
| `resilience/dead_letter_queue.py` | Failed order persistence & retry | `DeadLetterQueue`, `DLQRegistry` | — |
| `observability/` | Logging, metrics, health, alerts | — | uvicorn |

---

## Main Loop Sequence (main.py)

```
1. SETUP PHASE
   ├── setup_structured_logging()
   ├── initialize_db()
   ├── Config.validate()
   ├── verify_startup_health()
   ├── start_health_server(8080)
   ├── setup_alerts()
   ├── create order_book, kill_switch, strategy
   └── ws_client.connect() + wait_for_connection(10s)

2. TICK LOOP (every poll_interval sec)
   ├── kill_switch.check_and_trigger_with_capital()
   │   ├── get_balance() → Kalshi REST API
   │   └── if balance < threshold → cancel_all_orders()
   ├── strategy.check_for_new_release(capital)
   │   ├── FredCalendarProvider.fetch_latest_observation()
   │   ├── check DB if already processed
   │   ├── MockCalendarProvider.trigger_mock_release()
   │   │   ├── RiskManager.size_order()
   │   │   ├── ExecutionEngine.place_order()
   │   │   │   ├── if shadow: log + DB + position update
   │   │   │   └── if live: sign → rate limit → circuit breaker → POST
   │   │   └── return result
   │   └── return triggered
   ├── order_book.get_best_yes_bid/ask()
   ├── every 30s: psutil memory/cpu stats
   └── log STATUS REPORT

3. SHUTDOWN (SIGINT/SIGTERM)
   ├── disconnect ws_client
   ├── stop_health_server
   └── log finalization
```

---

## Data Flow: Trade Lifecycle

```
FRED API ──► MacroTrackerStrategy
                │
                ▼
         detect new release
                │
                ▼
         MockCalendarProvider.trigger_mock_release()
                │
                ├── RiskManager.calculate_kelly_fraction()
                │       estimated_prob vs market_price
                │       ↓
                ├── RiskManager.get_position_size_fraction()
                │       raw_kelly × 0.25 (fractional Kelly)
                │       ↓ min(VaR 2% cap)
                │
                ├── RiskManager.get_max_allowed_wager_for_sector()
                │       sector_exposure vs 30% limit
                │       ↓
                ├── RiskManager.size_order()
                │       proposed_wager vs sector limit
                │       ↓
                └── ExecutionEngine.place_order()
                        ↓
                ┌── SHADOW MODE? ──┐
                │                 │
                ▼                 ▼
          log to file +     sign_headers()
          SQLite +           rate_limiter
          position mgr       circuit_breaker
                               │
                               ▼
                          POST /trade-api/v2/portfolio/orders
                               │
                         ┌─────┴─────┐
                         │ 200/201   │ error
                         │           ▼
                         │      dead_letter_queue
                         ▼
                    order_state: FILLED
```

---

## External Dependencies

| Service | Protocol | Endpoint | Used By |
|---------|----------|----------|---------|
| Kalshi REST | HTTPS | `external-api.kalshi.com` (prod) / `external-api.demo.kalshi.co` (demo) | `ExecutionEngine`, `KillSwitch` |
| Kalshi WebSocket | WSS | `api.elections.kalshi.com` (prod) / `external-api-ws.demo.kalshi.co` (demo) | `KalshiWebSocketClient` |
| FRED API | HTTPS | `api.stlouisfed.org/fred/series/observations` | `FredCalendarProvider` |

---

## Database Schema & Relationships

```
macro_releases (1) ────── (0..*) shadow_trades
    id (PK)                     release_id (FK → macro_releases.id ON DELETE SET NULL)

shadow_trades (no direct FK to market_data_history)

market_data_history (standalone snapshot table)
```

Potential missing relationships:
- `shadow_trades` could reference `market_data_history` for market context at time of trade
- No `orders` table tracking live order lifecycle (only in-memory via OrderStateMachine)
- No `portfolio_snapshots` table for capital/balance history over time

---

## Key Configuration (config.py)

| Variable | Default | Purpose |
|----------|---------|---------|
| KALSHI_API_KEY_ID | — | API key for signing |
| KALSHI_PRIVATE_KEY_PATH | — | Path to RSA PEM key |
| KALSHI_ENV | demo | demo/prod |
| SHADOW_MODE | True | Simulate vs live |
| MAX_VAR_LIMIT_PCT | 0.02 (2%) | Max position risk |
| MAX_SECTOR_LIMIT_PCT | 0.30 (30%) | Max sector exposure |
| KELLY_MULTIPLIER | 0.25 (25%) | Fractional Kelly |
| KILL_SWITCH_MIN_BALANCE | 100.00 | Balance floor |
