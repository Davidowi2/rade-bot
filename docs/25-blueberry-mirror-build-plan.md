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
| Per-trade risk ceiling | **$25** | confirmed 2026-11-14 |
| Daily-loss breaker | **1% of the day's opening equity** (≈ $49 at $4,862.73) | confirmed 2026-11-14 |
| Max open positions | **1** | confirmed 2026-11-14 |
| Stop after TP1 | **TP1** | confirmed |
| Close | whole position at the final target | confirmed |
| LIMIT signals | recorded, never sent | confirmed |
| Duplicate window | 900 s identical text | confirmed |

### Why the ceiling is $25

Measured over 1,972 filled setups: median stop $9 → $27 risk at 0.03 lots, and
a $25 ceiling passes 43.4% of the trader's signals. The ceiling is set on the
stop distance alone, so the true cost per trade is the stop **plus** the spread
and the poll-delay slippage. Measured on the destination feed on 2026-11-14:

| Cost | Value | At 0.03 lots |
|---|---|---|
| Spread (ask−bid) | 0.31 | $0.93 |
| ~15 s poll-delay move | p50 0.69 / p95 1.55 | $2.07 / $4.65 |

So a $25 ceiling keeps stop + costs inside roughly $30, and $30 would let the
true risk reach ~$36. $25 is the honest number.

### The binding constraint

Total drawdown room is **$162.73** (6% of $5,000 = $300 floor at $4,700).
At $27 risk per trade, **six losing trades leave $0.73 and the seventh breaches
the account**. The daily breaker (1% ≈ $49) and the max-1-position rule are the
controls that stand between a bad run and a terminated account.

## 3. Phases

### Phase 1 — Destination guard and configuration
- Add a destination guard that pins host + server + account id. Refuse anything
  else. Never delete the existing environment guard.
- Wire the quote provider to the destination account.
- Verify the instrument spec against the account before any order.
- Config: `MIRROR_FIXED_LOTS=0.03`, `MIRROR_MAX_TRADE_RISK_USD=25`,
  `MIRROR_DAILY_LOSS_FRACTION=0.01`, `MIRROR_MAX_OPEN_POSITIONS=1`,
  `MIRROR_DESTINATION_SERVER=BLBRY`, `MIRROR_DESTINATION_ACCOUNT_ID=<id>`.
- The destination pin is **required**: an executor with an unset pin does not
  start, the same fail-closed rule as the ceiling.

### Phase 2 — Exit policy: TP1 (done)
- The stop moves to TP1 after price passes TP1.
- Keep: hold through TP1, no close at TP1/TP2, broker-attached final target.
- An earlier revision used breakeven; the measurement behind it had an inverted
  sign and was corrected on 2026-11-14. See `docs/23` §5 A1.

### Phase 3 — Service-level risk controls (done)
- The daily-loss breaker, max open positions and max drawdown are enforced in
  the service (`RiskLimits.from_env()` into the engine, checked before
  submission).
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
