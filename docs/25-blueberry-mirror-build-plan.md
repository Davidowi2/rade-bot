# 25 — Blueberry mirror build plan

Status: **plan, approved by the account holder.** Nothing below is deployed.
Date: 2026-11-14.

## 1. Verified destination facts

Read from the broker on 2026-11-14 with the account holder's credentials.

| Fact | Value |
|---|---|
| Host | `demo.tradelocker.com` |
| Server | `BLBRY` |
| Account | Blueberry 1-step evaluation, USD |
| Balance | $4,862.73 |
| XAUUSD instrument id | 4231 |
| Contract size (lotSize) | 100 oz per lot |
| Lot step / min / max | 0.01 / 0.01 / 40 |
| Tick size | 0.01 |
| Symbol status | FULLY_OPEN |
| Leverage | 30 |
| Open positions / orders | none |

The account id and credentials live in the deployment secrets, never in this
document and never in the repository.

## 2. Confirmed parameters

| Parameter | Value | Source |
|---|---|---|
| Copied signals | NOW / MARKET only | confirmed |
| Lot size | exactly 0.03, or refuse | confirmed |
| Per-trade risk ceiling | **$30** | confirmed 2026-11-14 |
| Daily-loss breaker | **$50** (of the day's opening equity) | confirmed 2026-11-14 |
| Max open positions | **1** | confirmed 2026-11-14 |
| Stop after TP1 | **breakeven at actual fill** | confirmed 2026-11-14 |
| Close | whole position at the final target | confirmed |
| LIMIT signals | recorded, never sent | confirmed |
| Duplicate window | 900 s identical text | confirmed |

### Why the ceiling is $30

Measured over 1,972 filled setups: median stop $9 → $27 risk at 0.03 lots.
A $30 ceiling passes 77.7% of the trader's signals. $20 passes 10.5% (silently
disables the bot). $25 passes 43.4%.

### The binding constraint

Total drawdown room is **$162.73** (6% of $5,000 = $300 floor at $4,700).
At $27 risk per trade, **five losing trades breach the account**. The daily
breaker ($50) and the max-1-position rule are the controls that stand between
a bad run and a terminated account.

## 3. Phases

### Phase 1 — Destination guard and configuration
- Add a destination guard that pins host + server + account id. Refuse anything
  else. Never delete the existing environment guard.
- Wire the quote provider to the destination account.
- Verify the instrument spec against the account before any order.
- Config: `MIRROR_FIXED_LOTS=0.03`, `MIRROR_MAX_TRADE_RISK_USD=30`,
  `MIRROR_DAILY_LOSS_FRACTION` set from $50, `MIRROR_MAX_OPEN_POSITIONS=1`.

### Phase 2 — Exit policy: breakeven
- Change the stop-move target from TP1 to the actual fill price after TP1.
- Keep: hold through TP1, no close at TP1/TP2, broker-attached final target.
- Tests for the new branch and the refusal paths.

### Phase 3 — Service-level risk controls
- Port the daily-loss breaker, max open positions and max drawdown into the
  observer service (today they exist only in the research engine).
- A disabled control reports `disabled`, never `passing`.

### Phase 4 — Verification
- Fake-channel end-to-end: private Telegram channel → bot → destination
  account. Parse, quote, size, order, stop move, close, every notification.
- Demo execution matrix (the 17 cases in `docs/22` Gate 3).
- Notification drill: confirm the Telegram messages arrive.
- Re-run the exit-policy comparison on 1-minute data.

### Phase 5 — Deploy
- Render + Supabase + Vercel dashboard.
- Uptime Robot ping to reduce spin-down.
- Secrets in dashboards only.
- Health alerts: stale heartbeat, failed notification, orphaned position.

### Phase 6 — Shadow, then live
- Shadow on the real channel with no orders.
- Funded only after the fake-channel test and the demo matrix pass.

## 4. Honest gaps that remain

- Live push has never been observed; the 15 s poll is the delivery path.
- Spread/slippage limits are unmeasured on the destination feed.
- The exit-policy comparison is a 30-minute-bar model, not real fills.
- No shadow run on the real channel has happened.
- The rules of the firm prohibit copying from a third party. Recorded, not
  engineered around.

## 5. Definition of done for Monday

- [ ] Destination guard refuses the wrong account.
- [ ] Fake-channel test passes end-to-end on the destination account.
- [ ] Notifications arrive.
- [ ] Daily breaker and max-1-position verified in the service.
- [ ] Exit policy is breakeven and tested.
- [ ] Deployment is live and monitored.

If any box is unchecked, no real order is placed.
