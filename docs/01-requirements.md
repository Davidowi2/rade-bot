# 01 — Requirements (SRS)

## 1. Functional requirements

### FR-1 Telegram ingestion
- FR-1.1 System shall ingest messages from a configured public Telegram channel.
- FR-1.2 System shall parse each message into a structured signal: timestamp,
  author, raw text, extracted entities (instrument, direction, entry, SL, TP,
  rationale), confidence.
- FR-1.3 System shall store raw messages immutably (append-only) and parsed
  signals separately, so re-parsing never loses source data.
- FR-1.4 System shall handle edits/deletes of source messages by marking, not
  overwriting.

### FR-2 Market data
- FR-2.1 System shall fetch OHLCV for configured instruments/timeframes.
- FR-2.2 System shall store bars locally with no gaps > 1 bar; gaps flagged.
- FR-2.3 System shall support at least H1, H4, D1 (swing timeframes).

### FR-3 Feature engineering
- FR-3.1 System shall compute trend, structure (HH/HL/LH/LL), volatility,
  session, and level features per bar.

### FR-4 Hedge probe (simulated)
- FR-4.1 System shall run a probe that places opposite-side limit orders in
  simulation and records which side triggers first and hold duration.
- FR-4.2 System shall aggregate probe outcomes into a direction score.
- FR-4.3 System shall never leave a live net position from a probe (probes are
  simulated or immediately flattened).

### FR-5 Signal generation
- FR-5.1 System shall produce a trade decision: instrument, direction, entry
  type (market/limit), entry price, SL, TP, size, rationale tags.
- FR-5.2 System shall refuse to trade when confidence < threshold or risk
  budget exhausted.

### FR-6 Execution
- FR-6.1 System shall place/cancel/modify orders via broker adapter.
- FR-6.2 System shall support Tradelocker (demo) and Bybit (paper/testnet first).
- FR-6.3 System shall reconcile local state with broker state on every tick.

### FR-7 Risk management
- FR-7.1 Per-trade risk ≤ configured % of equity.
- FR-7.2 Daily loss limit halts new entries.
- FR-7.3 Kill switch stops all activity and flattens (paper/demo) on trigger.

### FR-8 Observability
- FR-8.1 Structured logs for every decision and order.
- FR-8.2 Daily PnL + drawdown report.

## 2. Non-functional requirements
- NFR-1 Reproducibility: backtests deterministic given same data + seed.
- NFR-2 Latency: swing bot; decision latency < 5s acceptable.
- NFR-3 Reliability: auto-restart on crash; state persisted.
- NFR-4 Security: secrets in .env, never in code or logs.
- NFR-5 Testability: pure functions for signal logic; adapters mockable.

## 3. Open questions (lead decisions needed)
Resolved:
1. Telegram channel: "Free trading signal" (@XAUUSD_FOREX4) — historical backfill.
2. Instrument v1: XAUUSD (gold). Trader's instrument is our instrument.
3. Swing anchor timeframe: D1.
4. Max hold: 15h for both probe and real trade (D1 bias, intraday exit).
5. Paper/demo duration: 1 week, gated on ≥30 closed trades + checklist.
6. Historical bar data source: Tradelocker API.
7. D1 vs 15h reconciliation: keep D1 bias + intraday exit.

Still open:
8. News/event filter (avoid trading around high-impact releases)? (recommend yes)
9. Success metric thresholds (win rate, PF, expectancy, max DD)?
10. Alerting channel (Telegram bot to lead? email?)?
11. MT5 gateway: v2/v3 (deferred; do not block v1).
12. Does trader post other instruments we should later include?
