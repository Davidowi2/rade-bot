# ADR-0003: Learning target — model the trader's decision, not mirror his orders

- Status: Proposed (awaiting lead approval)
- Date: 2026-11-02

## Context
The channel publishes concrete orders: `XAUUSD SELL NOW 2982 / Sl: 2995 / Tp: 2974...`.
Two ways to "learn from a trader":
- **S0 Mirror** — take his setup verbatim (entry, SL, TP ladder) and execute it.
- **S1 Model** — learn the *decision function* behind his setups: what market
  state makes him go long vs short, how he sizes risk, where he places stops
  and targets. Then the bot forms its own trades from the same state.

Reconnaissance (docs/11) shows:
- S0 has a thin edge: TP-first 57.3%, median R +0.59, **mean R only +0.18**
  after charging the observed 0.25 spread.
- 90.2% of his stated entries match a bar close within $1 somewhere in
  [-120d, +24h] — his entry is a **limit level reached later**, not a market
  price at post time. Only 6.9% of `NOW` setups are within $1 of the next bar
  open.
- 20% of setups are historical recaps, 51% of episodes have no reported result.
- Direction mix is heavily long (1,726 BUY vs 670 SELL).

## Decision (proposed)
Build **all three** in Phase 2 and let walk-forward OOS decide:
- **S0** copy baseline (the benchmark his channel implies),
- **S1** learned decision model,
- **S2** model + simulated hedge probe as a filter.

Ship the winner, but require **S1 (or S2) to beat S0 by a stated margin** on
OOS expectancy. S0 alone is not an acceptable Phase 2 outcome, because it
reproduces a mean R of +0.18 — not a business.

Consequence: the trader's channel is used as a **labelled teaching set**, not
as a live signal feed. The bot does not need to wait for him to post.

## Alternatives considered
- **Live-copy only** — rejected: it is a mirror bot, which the lead explicitly
  ruled out, and it inherits a +0.18R mean edge.
- **Black-box ML on price alone** — rejected: it would not be "trading like
  him", and 2,396 samples cannot support a deep model.

## Consequences
- Labelling engine and feature pipeline become the centre of Phase 2, not
  execution code.
- Every backtest must report S0 alongside the candidate strategy.
- Honest negative result is an acceptable Phase 2 output (see 12-phase2-design).
