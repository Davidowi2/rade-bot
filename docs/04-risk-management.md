# 04 — Risk management

## Principles
- Capital preservation first. A bot that survives beats a bot that maximizes.
- Risk is defined per trade as % of equity, not fixed lot size.

## Rules (defaults — tune later)
- Per-trade risk: 0.5% of equity (max 1.0%).
- Max concurrent positions: 3.
- Daily loss limit: 2% of equity → halt new entries for the day.
- Weekly loss limit: 5% → require manual reset.
- Max drawdown from peak: 10% → kill switch (flatten paper/demo).
- Min risk:reward for entry: 1:1.5.

## Position sizing
size = (equity * risk_pct) / (entry - stop) in price units, then clamped to
broker min/max and to a max notional cap.

## Kill switch
- Triggered by: daily/weekly limit, max DD, repeated broker errors, data gap,
  manual flag file.
- Action: cancel all open orders, flatten positions (in paper/demo), alert,
  stop loop.

## What we will NOT do
- No martingale / no averaging down on losers.
- No leverage beyond configured max.
- No trading during configured blackout windows (news, weekends if required).

## Open
- Confirm risk_pct and limits with lead.
- Confirm whether overnight/weekend holding allowed.
