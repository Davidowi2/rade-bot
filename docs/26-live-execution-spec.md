# 26 — Live execution spec (BLBRY, real orders)

Status: **SPECIFICICATION, not built.** Nothing here is implemented until the
account holder approves it. Written 2026-11-14.

## 1. What "live" actually means here

Measured, not assumed:

| Fact | Value | Source |
|---|---|---|
| BLBRY credentials authenticate on | `demo.tradelocker.com` | login probe 2026-11-14 |
| BLBRY credentials on `live.tradelocker.com` | **401 — "check if server exists"** | login probe 2026-11-14 |
| Account id | `2346225` | `get_all_accounts()` |
| Account name | `BLBRY#2b44e3ec-…#1#1` | `TLAPI.account_name` |
| Balance | **4862.73** | `get_account_state()` |
| Open positions / orders | 0 / 0 | same |
| Today | 2 trades, gross +1.34, net +1.27, volume 0.02 | same |

**Consequence:** there is no separate "live host" to switch to. The funded
Blueberry account lives on the demo host. "Going live" therefore means
**placing real orders on account 2346225**, not changing hosts.

The existing `assert_demo_environment()` guard is about the *host*, and it is
correct for this account. It is not the thing standing between us and live
trading. The thing standing between us and live trading is configuration that
is deliberately fail-closed, plus four gaps below.

## 2. What is already built

- Gateway with a destination pin (server + account id) and a host assert.
- Executor: fixed-lot sizing, per-trade risk ceiling, entry gating.
- Risk limits wired into the service: daily loss fraction, max drawdown
  fraction, max open positions. A disabled control reports `disabled`.
- Exit ladder: stop moves to **TP1** after price passes TP1; nothing closes
  until the final target. (`docs/23` §5 A1.)
- Listener → parser → pipeline → Supabase ledger → per-result notification.
- Kill switch table (`mirror_control`) with `set_paused` / `pause_state`.
- Startup reconciliation against the broker.
- 481 tests passing; the service is **deployed and live in paper mode**.

## 3. What is missing for a real order

### G1 — Contract size is not confirmed
`MIRROR_CONTRACT_UNITS_CONFIRMED=0`. Every signal is recorded as `blocked`.
The account holder has said the size is 100 oz/lot, but the code has never read
the broker's own `lotSize`/`contractSize` for XAUUSD and matched it.

**Build:** read the instrument spec from the destination account, pin the value,
refuse on mismatch. Only then set `CONFIRMED=1`.

### G2 — No quote provider
`MIRROR_QUOTE_PROVIDER=none`. NOW (market) calls cannot be priced, so they are
blocked even with the size confirmed.

**Build:** `MIRROR_QUOTE_PROVIDER=tradelocker`, quote max age 2 s, and the
refusal path when no fresh quote exists.

### G3 — Destination pin points at the demo account
The deployment is pinned to `PLEXY` / `2509174`. The funded account is
`BLBRY` / `2346225`.

**Build:** pin BLBRY + 2346225, and use the BLBRY credential set. The PLEXY
credentials are a different account and must not be mixed in.

### G4 — Spread and slippage limits are not filters
Both are set to `5` price units. XAUUSD's normal spread is about 0.31, so a
limit of 5 accepts a book ~16× wider than normal.

Measured on the destination feed 2026-11-14:

| Cost | Value | At 0.03 lots |
|---|---|---|
| Spread (ask−bid) | 0.31 | $0.93 |
| 15 s poll-delay move | p50 0.69 / p95 1.55 | $2.07 / $4.65 |

**Build:** set both from a measured distribution (a week of samples), and count
spread + slippage **inside** the $25 ceiling, not beside it.

### G5 — No startup report
The service logs to stdout (Render captures it) but sends nothing to Telegram.
The account holder asked for the opposite: loud, no silent failures.

**Build:** on boot, send one message containing listener state, channel,
destination (server + account), contract size, sizing mode, quote provider,
execution mode, and **every risk control with its value or `disabled`**.

### G6 — Max drawdown fraction is too loose for this account
Blueprint default is 5% of peak. The challenge floor is 6% of $5,000 = $4,700.
From the current 4,862.73 the room is **$162.73 = 3.35%**. A 5% limit permits
$243 of drawdown and would breach the account.

**Build:** set the fraction from the challenge rule, not from a default.

### G7 — Fill and error notification coverage
Per-result notifications exist; it is not yet verified that a *fill* and a
*management* event reach Telegram.

**Build:** verify each event kind end to end; notify on fill, on stop move, on
close, and on every refusal with its reason.

## 4. The gate before the first real order

None of these is optional. In order:

1. **G1–G4 built and unit-tested.** No config-only shortcuts.
2. **Paper mode still clean** — the deployed service records real channel
   signals and notifies, with zero orders.
3. **Fake-channel end-to-end test.** Post a known signal to a private test
   channel; confirm the bot records it, sizes it, notifies it, and places
   **no** order.
4. **One real order, watched.** `MIRROR_EXECUTE=1`, `MIRROR_FIXED_LOTS=0.03`,
   ceiling $25, max 1 position, on the BLBRY account. Manual approval before
   submission; the run is watched end to end.
5. **Only then** leave it unattended.

## 5. Kill switch and rollback

- `mirror_control.execution_paused` in Supabase, checked before every order.
  Setting it to 1 stops new entries; open trades are still managed to closure
  (abandoning open risk is worse than the loss a breaker caps).
- `MIRROR_EXECUTE=0` in the environment is the hard stop: the executor does not
  start, and the service refuses to boot with execution requested but not
  started.
- To stop everything: pause in Supabase, then set `MIRROR_EXECUTE=0` and
  redeploy.

## 6. Questions that must be answered before building

1. **Is `demo.tradelocker.com` really where the funded account is traded?** It
   is the only host the BLBRY credentials work on. If Blueberry has a separate
   live host, that changes G3.
2. **Drawdown room.** The account reads 4,862.73 with a $4,700 floor, i.e.
   $162.73. An earlier figure of "$200–250 lost, $700 to recover" does not
   describe this account. Which is right?
3. **Daily breaker basis.** Fixed $50, or 1% of the day's opening equity
   (≈$48.63)? The two documents disagree.
4. **Which credential set is authoritative?** `.env` holds both the BLBRY set
   (`LOGIN`/`PASSWORD`/`SERVER`) and a PLEXY set (`TRADELOCKER_*`,
   account 2509174). Confirm BLBRY is the trading destination and PLEXY is
   dropped.
