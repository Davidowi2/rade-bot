# 26 — Live execution spec (BLBRY, real orders)

Status: **PARTIALLY BUILT.** Done: G3 (destination pinned to BLBRY/2346225 with
the BLBRY credentials), G4 (measured guard values), G5 (startup report), G6
(drawdown fraction from the challenge rule). Remaining: G1, G2 and G7, which
are the live flip in §7. Written 2026-11-14.

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
- 502 tests passing; the service is **deployed and live in paper mode**, with
  the contract size confirmed and a quote provider set.

## 3. What was missing for a real order

G1–G7 are closed as of 2026-11-15. What remains before a real order is not a
build task: it is the sizing decision in `docs/28` and the demo run in §4.

### G1 — Contract size is not confirmed — **done 2026-11-15**
The broker's own specification for XAUUSD on account 2346225 was read:
`lotSize 100, lotStep 0.01, minLot 0.01, maxLot 40, status FULLY_OPEN`.
`MIRROR_CONTRACT_UNITS_PER_LOT=100` and `MIRROR_CONTRACT_UNITS_CONFIRMED=1`
are set in Render. Until both are set, every signal is recorded as `blocked` —
which is exactly what happened on 2026-11-15. See `docs/28`.

### G2 — No quote provider — **done 2026-11-15**
`MIRROR_QUOTE_PROVIDER=tradelocker`, quote max age 2 s. NOW (market) calls are
priced from the demo feed through the same gateway, which refuses a non-demo
environment.

### G3 — Destination pin — **resolved 2026-11-15**
The deployment is pinned to `BLBRY` / `2346225` and holds that account's own
credentials; the pin matches them.

The local `.env` holds a **different, older** account (`PLEXY` / `2509174`). It
must not be used to configure Render: `scripts/update_render_env.py` reads the
remote env back and writes the union, and `scripts/configure_render_service.py`
sets only the service fields. A stale local file therefore cannot retarget the
bot at the wrong account.

### G4 — Spread and slippage limits are not filters — **done 2026-11-15**
Both were set to `5` price units. XAUUSD's normal spread is about 0.31, so a
limit of 5 accepted a book ~16× wider than normal.

Measured on the destination feed 2026-11-14:

| Cost | Value | At 0.03 lots |
|---|---|---|
| Spread (ask−bid) | 0.31 | $0.93 |
| 15 s poll-delay move | p50 0.69 / p95 1.55 | $2.07 / $4.65 |

Now `MIRROR_MAX_SPREAD=1.0` and `MIRROR_MAX_SLIPPAGE=2.0`. The **defaults** were
the same problem: `RiskLimits` fell back to 5 / 5 / 5% / 3 positions, so a
deployment that forgot an env var silently got the permissive limit. The
defaults are now the tight values (spread 1.0, slippage 2.0, drawdown 3.35%, one
position), with a regression test that an empty environment yields them.

### G5 — No startup report — **done**
`send_startup_report()` in `src/rade/observer/service.py` sends one Telegram
message on boot: listener state and heartbeat, channel, destination (server +
account), contract size and whether it is confirmed, sizing mode, quote
provider, execution mode, every risk control with its value or `DISABLED`, the
pause switch, the store, and the lot and ceiling.

### G6 — Max drawdown fraction is too loose for this account — **done**
Was the 5% blueprint default, which from 4,862.73 permits $243 of drawdown
against a $162.73 room. Now `MIRROR_MAX_DRAWDOWN_FRACTION=0.0335`, and that is
also the code default, so an unset variable cannot restore the loose value.

### G7 — Fill and error notification coverage — **code complete**
The engine notifies on signal, accepted, rejected, filled, blocked,
not-copied, partial close, stop move, review and close, each with a distinct
`kind` so the notification ledger's `(trade, kind)` dedupe cannot drop a later
event. End-to-end delivery of a *fill* and a *management* event has not been
observed on the demo account yet; that is part of the demo run in §4 step 3.

## 4. The gate before the first real order

None of these is optional. In order:

1. **G1–G7 built and unit-tested** — done 2026-11-15. No config-only shortcuts.
2. **Paper mode still clean** — the deployed service records real channel
   signals and notifies, with zero orders.
3. **Fake-channel end-to-end test.** Post a known signal to a private test
   channel; confirm the bot records it, sizes it, notifies it, and places
   **no** order.
4. **The sizing mismatch resolved.** The ceiling and the lot must together
   admit the stop width the channel actually posts. See `docs/28` and step 0
   of the runbook in §7.
5. **One real order, watched.** `MIRROR_EXECUTE=1`, max 1 position, on the
   BLBRY account. Manual approval before submission; the run is watched end
   to end.
6. **Only then** leave it unattended.

## 5. Kill switch and rollback

- `mirror_control.execution_paused` in Supabase, checked before every order.
  Setting it to 1 stops new entries; open trades are still managed to closure
  (abandoning open risk is worse than the loss a breaker caps).
- `MIRROR_EXECUTE=0` in the environment is the hard stop: the executor does not
  start, and the service refuses to boot with execution requested but not
  started.
- To stop everything: pause in Supabase, then set `MIRROR_EXECUTE=0` and
  redeploy.

## 6. Questions that were blocking

1. **Is `demo.tradelocker.com` really where the account is traded? — answered
   2026-11-15.** It is the only host the BLBRY credentials work on; Render is
   pinned there and the gateway asserts the host on every call.
2. **Drawdown room — still open.** The account reads 4,862.73 with a $4,700
   floor, i.e. $162.73. An earlier figure of "$200–250 lost, $700 to recover"
   does not describe this account. Which is right?
3. **Daily breaker basis — answered.** `MIRROR_DAILY_LOSS_FRACTION=0.01` of
   the day's opening equity (≈$48.63 at 4,862.73). The "fixed $50" figure was
   the older document.
4. **Which credential set is authoritative? — answered 2026-11-15.** Render
   holds BLBRY / 2346225 and the pin matches it. The local `.env` holds PLEXY /
   2509174 and is no longer read for credentials.

## 7. The live flip (runbook)

Not a redeploy of new code. Rows 1–5 are **already set** as of 2026-11-15; the
only remaining change is row 6.

| # | Setting | Now (paper) | Live |
|---|---|---|---|
| 1 | `MIRROR_DESTINATION_SERVER` | `BLBRY` | `BLBRY` |
| 2 | `MIRROR_DESTINATION_ACCOUNT_ID` | `2346225` | `2346225` |
| 3 | `MIRROR_CONTRACT_UNITS_PER_LOT` | `100` | `100` |
| 4 | `MIRROR_CONTRACT_UNITS_CONFIRMED` | `1` | `1` |
| 5 | `MIRROR_QUOTE_PROVIDER` | `tradelocker` | `tradelocker` |
| 6 | `MIRROR_EXECUTE` | `0` | `1` |

Steps:

0. **Resolve the sizing mismatch first.** `0.03` lots × 100 units/lot is
   $3.00/point, so the $25 ceiling admits an **8.33-point** stop and the channel
   posts 10-point stops. `/api/status` reports `max_stop_distance`. As
   configured, the executor would refuse every signal — the same silent
   non-trading state as before, one layer further in. Raise the ceiling or
   lower the lot before step 5. See `docs/28`.
1. Confirm the broker still reports `lotSize` 100 for XAUUSD on account
   2346225 (`scripts/check_blueberry_account.py`). If it differs, stop.
2. Confirm the account has **no open positions and no pending orders**. If it
   has, resolve them first; the executor reconciles on startup but does not
   close anything it did not open.
3. Post a known signal to a private test channel and confirm the bot records
   it, sizes it, notifies it, and places **no** order.
4. Only then set row 6 (`MIRROR_EXECUTE=1`) and redeploy. The startup report
   must say `mode: EXECUTING ORDERS` and `contract 100/lot CONFIRMED`.
5. Watch the first trade end to end: entry, stop attached, stop move to TP1,
   final target.

To stop: set `MIRROR_EXECUTE=0` and redeploy (hard stop), or set
`execution_paused=1` in `mirror_control` (stops new entries, open trades are
still managed to closure).
