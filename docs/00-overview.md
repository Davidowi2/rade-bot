# 00 — Overview

## Vision
Rade Bot is a **swing-direction** trading bot. It is not a scalper. It learns the
style of a single trader whose analysis is published on a public Telegram
channel, then reproduces that style: slow, directional positions held for hours
to days, never minutes.

## What "learning" means here
We do not train a black box on price alone. We build a **labelled dataset** of
the trader's actual calls (entry, direction, instrument, rationale) scraped from
Telegram, align each call to market data, and derive explicit, testable rules
(entry conditions, invalidation, stop, target, hold time). The bot then executes
those rules. ML is a helper, not the product.

## The hedge probe (novel element)
Before committing direction on a borderline setup, the bot runs a **simulated**
probe: it places (in simulation, or as resting limit orders that we observe)
opposite-side limit orders around price and watches which side gets touched /
filled and how long price holds. This tells us who is in control (buyers vs
sellers) without taking a full directional position first. It is **not** live
hedging and does **not** leave us net-flat exposed in production.

## Scope boundaries
- v1 asset: Gold/Forex via Tradelocker (demo first). Bybit/crypto deferred to v2.
- Swing bias on D1 bars; max hold 15h (probe AND real trade).
- Paper/demo first. Live only after gated promotion checklist.
- One trader, one style. No multi-strategy in v1.
- 1 week paper/demo minimum, gated on ≥30 closed trades + checklist.

## Success criteria (draft — to be refined)
- Backtest expectancy > 0 on ≥ 2 years of data for the target instrument.
- Out-of-sample (walk-forward) profit factor ≥ 1.3.
- Max drawdown within risk budget (see 04-risk-management.md).
- Paper-trading for ≥ 4 weeks with no unhandled exceptions and slippage within model.

## Open decisions
See [01-requirements.md](01-requirements.md) §Open questions.
