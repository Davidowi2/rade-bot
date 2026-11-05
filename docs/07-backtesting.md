# 07 — Backtesting

## Approach
Event-driven backtest reusing the same strategy/risk/execution interfaces as
live (simulated adapter). Bar-by-bar replay, no lookahead.

## Metrics
- Win rate, profit factor, expectancy (R per trade).
- Max drawdown, Sharpe/Sortino.
- Trade count, avg hold time, slippage vs model.
- Out-of-sample via walk-forward (rolling train/test windows).

## Validation
- In-sample fit only for rule discovery; final params frozen before OOS.
- Stress: vary costs (spread/commission) ±50%.
- No parameter tuning on OOS data.

## Data split (draft)
- Train: oldest 60%
- Validate: next 20%
- Test: last 20% (touch once).

## Open
- Cost model per instrument (spread, commission, swap).
- Slippage assumption.
