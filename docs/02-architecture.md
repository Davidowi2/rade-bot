# 02 — Architecture

## Style
Modular monolith in Python. One process, clear module boundaries, dependency
injection for adapters. No microservices in v1 (YAGNI).

## Modules
```
src/rade/
├── config/          # settings from .env + yaml
├── ingestion/
│   ├── telegram/    # channel scraper + parser
│   └── marketdata/  # OHLCV fetchers
├── storage/         # DB models, repositories
├── features/        # indicators, structure, levels
├── probe/           # simulated hedge probe engine
├── strategy/        # signal generation, rules
├── risk/            # sizing, limits, kill switch
├── execution/       # broker adapters (tradelocker, bybit)
├── backtest/        # engine, metrics, reports
└── ops/             # logging, monitoring, scheduler
```

## Data flow
```
Telegram channel ──► ingestion.telegram ──► storage (raw + signals)
                                              │
Market data ──────► ingestion.marketdata ──► storage (bars)
                                              │
                                              ▼
                                    features (per bar)
                                              │
                                              ▼
                                    probe (direction score)
                                              │
                                              ▼
                                    strategy (trade decision)
                                              │
                                              ▼
                                    risk (size / gate)
                                              │
                                              ▼
                                    execution (broker adapter)
                                              │
                                              ▼
                                    storage (orders, positions, pnl)
```

## Key design decisions
- Adapters implement a common `BrokerAdapter` protocol → execution is
  broker-agnostic; backtest reuses the same interface with a simulated adapter.
- All time in UTC internally; display in user TZ.
- Idempotent ingestion: re-running never duplicates (upsert by (source, id)).
- Strategy logic is pure functions over (bars, features, signals) → easy to
  unit test and backtest.

## ADRs
See docs/decisions/.
