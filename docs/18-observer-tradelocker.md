# 18 — Observer step 3: TradeLocker read-only surface

**Status: read surface verified; latency not attributable on this host; the
stop-modification row is untouched and still `BLOCKED`.**

Script: `scripts/verify_tradelocker_readonly.py`. Evidence:
`docs/evidence/tradelocker-readonly-2026-11-12T13-42-40Z.json` (11/11 calls) and
`…T13-52-27Z.json` (8/11, link timeouts).

The probe runs against `TRADELOCKER_ENV=demo` only and **refuses to start**
otherwise. It then replaces every mutating method on the client with one that
raises, so a mistake in the probe cannot place, modify or close anything. The
guard is checked at the end of every run and recorded: `create_order`,
`close_position` and `modify_position` each returned `refused`.

---

## 1. What the read surface returns

| Route | Result | Note |
|---|---|---|
| accounts | 2 demo accounts | `…-1#1` balance 30.16 USD, `…-2#2` balance 100,992.92 USD |
| account state | 26 fields | `balance`, `equity` equivalents, `openNetPnL`, `positionsCount`, `stopOutLevel`, margin fields |
| positions | 0 rows | empty, not an error |
| orders | 0 rows | empty |
| executions | 0 rows | the demo session has not traded |
| instrument details | `XAUUSD`, type `EQUITY_CFD`, exchange `Plexy`/`Metals` | tick size returned as a nested object |
| quotes | ask **3880.23** / bid **3879.99** | spread **0.24**, sizes 100/100 |
| rate limits | 18 routes | table below |

### Documented rate limits

| Route | Limit | Route | Limit |
|---|---|---|---|
| `QUOTES`, `DEPTH` | 10 / 1s | `GET_ACCOUNTS`, `GET_EXECUTIONS`, `GET_INSTRUMENTS`, `GET_INSTRUMENT_DETAILS` | 2 / 1s |
| `PLACE_ORDER`, `MODIFY_ORDER`, `MODIFY_POSITION`, `DAILY_BAR`, `TRADES` | 5 / 1s | `GET_ACCOUNTS_STATE` | 4 / 2s |
| `QUOTES_HISTORY` | 3 / 1s | `GET_ORDERS`, `GET_ORDERS_HISTORY`, `GET_POSITIONS` | **1 / 1s** |

The tightest limit is **one request per second** on positions, orders and order
history. The probe reads this table from the session config before it calls
anything, and paces itself at the tightest interval (1.05s) rather than the
0.3s it used on the first run. A polling observer that fetches positions and
quotes on the same tick must budget for the 1/s ceiling, not the 10/s one.

### What is *not* verified

* **Fills reconciliation against the broker CSV.** The demo session has zero
  executions, so there is nothing to reconcile. The row stays open until the
  account has traded.
* **Equity vs summed unrealised P&L.** No open positions, so the check is not
  applicable. It is implemented and will run on the first position.
* **Stop modification.** Needs an order. `BLOCKED — awaiting authorisation`,
  unchanged by this pass.

---

## 2. Latency: measured, but not attributable

Every read-only call succeeded on the first run — and took **5–16 seconds**:

| | ms |
|---|---|
| auth (login + session) | 44,121 |
| p50 call | 6,755 |
| p95 call | 15,626 |
| max | 16,038 |

The second run was worse — `instrument_details`, `quote_bid` and `quotes` timed
out at 49–59s, and the TLS handshake to the broker never completed. Before
concluding anything about the broker, the probe measures the cost of a bare
connection:

| Target | DNS | TCP | TLS |
|---|---|---|---|
| `demo.tradelocker.com` | 2.5 ms | **6,783 ms** | **timed out** |
| `demo.tradelocker.com` (earlier) | 544 ms | 2,050 ms | 3,585 ms |
| `www.cloudflare.com` | 1,328 ms | 2,991 ms | 5,070 ms |
| `api.telegram.org` | 285 ms | 1,065 ms | 1,811 ms |

**A third measurement, twenty minutes later, contradicts the first two:**

| Target | DNS | TCP | TLS |
|---|---|---|---|
| `api.telegram.org` | 71 ms | 628 ms | **637 ms** |
| `demo.tradelocker.com` | 327 ms | 471 ms | **539 ms** |
| `www.cloudflare.com` | 262 ms | 581 ms | **515 ms** |

So the link is not *slow*, it is **unstable by an order of magnitude within the
hour**: 0.5s handshakes at best, 6.8s of TCP and a dead TLS handshake at worst.
That is worse for measurement than a consistently slow link, because a single
run cannot tell you which regime it was in. The API timings above were taken in
the bad regime, which is why they say nothing about TradeLocker.

At the best observed 0.5s handshake, this host would still spend a quarter of
the 2s reception budget on TCP+TLS alone before any API work. That is the
conclusion that survives all three measurements: **timings taken here are
link-bound, and must be taken on the deployment host.**

This is the second host-level defect found today, and it is the more serious of
the two:

1. **Clock** 19,430 s slow (`w32time` stopped) — blocks any end-to-end latency
   measurement.
2. **Network** unstable between 0.5s and 7s per connection, with timeouts under
   load — blocks any *attributable* latency measurement, and would make a live
   observer unreliable.

Both are properties of this developer machine. Neither can be fixed from here
(the time service needs elevation; the network is not ours to fix). The
consequence for the plan is the same for both: **the observer must be measured
and run on the deployment host**, which §5.3 of the plan already specifies as
the Render worker. A desktop whose connection varies by 14× within an hour
cannot be the evidence for a 2s reception budget or a broker-polling design.

`scripts/preflight.py` now checks both before anything else is measured, so the
next pass starts by discovering the host's state rather than by producing
numbers that have to be retracted.

---

## 3. What this changes

* The four read-only TradeLocker rows in §5.1 move from "not started" to
  **"surface verified; latency pending a valid host"**. Positions, orders,
  executions, quotes, account state and rate limits all return well-formed data
  on demo.
* The rate-limit row gains a concrete answer: **1 request/second** on the
  routes an observer polls most, 10/second on quotes.
* The latency rows are **not** closed by this pass, and the demo gate stays
  closed. Nothing here touches an order path.
* The 1m history row is unaffected: that fetch already ran and was verified
  (`docs/16-replay-results-1m.md`), and it does not depend on this host's
  latency, only on its bandwidth.
