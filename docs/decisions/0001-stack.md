# ADR-0001: Technology stack

- Status: Accepted (proposed)
- Date: 2026-11-02

## Context
Need a stack that supports data ingestion, backtesting, broker adapters, and
future ML, on Windows, with a solo developer.

## Decision
- Language: Python 3.11+ (available: 3.14).
- Storage: SQLite (dev/backtest), optional Postgres later.
- Market data/brokers: ccxt for Bybit; custom REST for Tradelocker.
- Telegram: Telethon.
- Backtesting: `backtesting` lib initially; revisit vectorbt if needed.
- Config: pydantic-settings + .env.
- Lint/test: ruff, pytest, mypy.

## Consequences
- Single language, easy to iterate.
- Performance fine for swing timeframes.
- ML optional later; no heavy infra now.
