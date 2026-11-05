# 03 — Data model (draft)

## Tables

### raw_telegram_messages
id (pk), channel, message_id, posted_at, edited_at, deleted, text, raw_json, ingested_at

### signals
id (pk), message_id (fk), instrument, direction, entry_type, entry_price,
stop_loss, take_profit, rationale, confidence, parsed_at, parser_version

### instruments
id (pk), symbol, broker, asset_class, tick_size, contract_size, min_size

### bars
instrument_id (fk), timeframe, ts (pk composite), open, high, low, close, volume

### features
instrument_id, timeframe, ts (pk composite), feature_name, value

### probes
id (pk), instrument, started_at, ended_at, buy_limit, sell_limit, buy_filled_at,
sell_filled_at, first_side, hold_seconds, outcome, score

### orders
id (pk), broker, broker_order_id, instrument, side, type, price, size, status,
created_at, filled_at, avg_fill

### positions
id (pk), instrument, side, size, entry_price, stop_loss, take_profit, opened_at,
closed_at, close_reason, pnl

### pnl_daily
date (pk), equity_start, equity_end, realized, unrealized, max_dd

## Notes
- Use SQLite for dev/backtest, Postgres only if needed later.
- All timestamps UTC (ISO-8601).
- Migrations via alembic (or simple versioned SQL in v1).
