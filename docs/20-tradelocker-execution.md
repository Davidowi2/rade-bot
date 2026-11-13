# 20 — TradeLocker DEMO execution path

**Status: step 1 of the execution spec — the safeguard audit — is complete.
The broker write surface did not exist before this work; §1 records what was
already enforced and what was missing.**

This document is the design and evidence record for the TradeLocker **demo**
execution path. It extends the paper mirror; it does not replace it. Every
control the paper mirror enforces is still enforced, and the demo path adds
the broker-side protections the paper path never had.

Scope, stated once and not restated:

* **Demo only.** The execution adapter refuses any environment other than
  `demo`. There is no live-account code path to enable.
* **LIMIT is out of scope.** A LIMIT signal is recorded as *not copied* with
  the reason. It is never sent to the broker.
* **Edited messages are never executed.** A revision produces a review record,
  never an order.
* **Nothing is guessed.** Contract units, symbol ids, route ids, lot steps and
  minimum quantities come from the broker or the order is blocked and the
  reason recorded.

---

## 1. Safeguard audit (spec step 1)

The question this section answers: *before a single broker order existed, what
would have protected the account, and what would not?*

### 1.1 Enforced in the running service

These are real code paths in the deployed service, not conventions in a
runbook. Each one is cited to the file and line that implements it.

| # | Safeguard | Where | Behaviour |
|---|---|---|---|
| S1 | Strict entry grammar | `src/rade/observer/mirror_pipeline.py:62-76` | One entry line, one stop line, one number per line. Zero or several entry lines never becomes a signal. |
| S2 | Recap suppression | `mirror_pipeline.py:81-84`, `:281-284` | A post whose text carries `closed`, `pips`, `hit tp`, `recap` is `ignored`, not a trade. |
| S3 | Side and price sanity | `mirror_pipeline.py:312-320` | Prices must be finite and positive; the stop must be on the correct side of entry; every target must be on the correct side of entry. |
| S4 | Target ladder normalised nearest-first | `mirror_pipeline.py:136-137` | The ladder is sorted by distance, and `ladder_normalised` records that a reordering happened. |
| S5 | Edited messages never executed | `mirror_pipeline.py:324-332` | `revision > 0` ⇒ `manual_review`, with the entry/stop change called out. |
| S6 | Ambiguity goes to a human | `mirror_pipeline.py:286-304` | Multiple entry lines, two stop lines, duplicate targets, missing stop or target ⇒ `manual_review`. |
| S7 | Unconfirmed contract size blocks sizing | `mirror_pipeline.py:334-344` | `units_confirmed` defaults to `False`; an unconfirmed spec blocks the message and computes no size. Checked *before* equity so the recorded reason names the real blocker. |
| S8 | Risk cap that config cannot raise | `mirror_pipeline.py:52-53`, `:347-352` | `MAX_RISK_FRACTION = 0.005`. A configured fraction outside `(0, 0.005]` is `blocked`. |
| S9 | Risk-based sizing from the published stop | `mirror_pipeline.py:398-402` | `risk_per_lot = |effective_entry − stop| × contract_units`; lots = `risk_budget / risk_per_lot`. |
| S10 | Quantity rounded to the broker step | `mirror_pipeline.py:403-404` | Rounded **down** to the lot step, never up. |
| S11 | Minimum lot is a skip, not a shrink | `mirror_pipeline.py:405-410` | A size below the broker minimum is `skipped_minimum_lot` — the trade is dropped, never reduced to fit. |
| S12 | Fresh quote required for a market call | `mirror_pipeline.py:359-376` | No quote, an invalid quote, a stale quote, or a quote timestamped in the future beyond tolerance ⇒ `blocked`. |
| S13 | Market already crossed the stop | `mirror_pipeline.py:379-381` | If the live price has already passed the published stop, the trade is `blocked`, not chased. |
| S14 | Market already crossed a target | `mirror_pipeline.py:382-384` | ⇒ `manual_review`. |
| S15 | Idempotency enforced by the database | `mirror_pipeline.py:477-494`, `store.py:103-137` | `event_key` primary key and `UNIQUE(channel, message_id, revision)`. A redelivery is a duplicate; the same revision with a different body is a `conflict`, not an update. Enforced by the store, so a restart or a second listener cannot double-count. |
| S16 | Durable store required on a hosted host | `store.py:338-370` | `hosted_mode()` ⇒ `make_ledger()` raises `StorageError`; `service.py:403-409` exits 3. The SQLite fallback is refused where the disk is wiped. |
| S17 | Cursor, not replay | `listener.py:251-289` | First activation takes the newest message id as the floor unless a start id is configured; importing history needs a second switch. A non-positive id is refused. |
| S18 | Notification state machine | `notify.py:158-239` | The slot is reserved `pending` before the send and completed with the real outcome after. Only a real `sent` suppresses a resend. |
| S19 | Permanent errors are not retried | `notify.py:143-155`, `:292-298` | A 4xx other than 429 will fail again; it is skipped instead of retried forever. 429, 5xx and transport errors are retried with backoff. |
| S20 | Health reflects the listener | `service.py:129-153`, `:179-196` | `/healthz` returns 503 when the listener is dead, stale, stopped or degraded. `connecting` is an honest transient 200. |
| S21 | A failed reconnect is visible | `listener.py:216-223` | A failed reconnect sets `reconnect_failed`, and the heartbeat phase becomes `degraded` rather than `listening`. |
| S22 | Read-only HTTP service | `service.py:287-290` | Every non-`GET` method is refused with 405. |
| S23 | Secrets stay server-side | `notify.py:63-76`, `store.py:55-68` | The bot token and the Supabase key are read from the environment. `__repr__` on both transports deliberately omits the secret, and Telegram errors are scrubbed of the token before being surfaced. |
| S24 | Demo-only quotes | `src/rade/observer/quotes.py:25-26` | The quote provider refuses a non-demo environment. Paper sizing against a live account would be a live-money decision made by accident. |
| S25 | Read-only probe disarms writes | `scripts/verify_tradelocker_readonly.py:41-50` | Every mutating method is replaced with one that raises before the probe calls anything. |

### 1.2 Research-only: implemented in the backtest/replay engines, absent from the service

These exist and are exercised by the research code. **None of them runs in the
observer service.** Moving to the broker without porting them is a gap, and
§1.2 is the list to port.

| Control | Where | Present in the service? |
|---|---|---|
| Max hold hours (15h default) | `src/rade/backtest/engine.py:39`, `:158` | **No** |
| Daily-loss breaker | `src/rade/backtest/engine.py:42`, `:112` | **No** |
| Max open positions | `src/rade/backtest/engine.py:44`, `:115` | **No** |
| Max drawdown breaker | `src/rade/backtest/engine.py:43`, `:56` | **No** |
| Spread / slippage cost model | `src/rade/backtest/costs.py:9-19` | **No** — sizing carries `estimated_stop_risk_before_slippage_and_fees` (`mirror_pipeline.py:444`), which names the omission rather than correcting it |
| Commission | `src/rade/replay/assumptions.py:25` | **No** |

The risk cap (S8) and the sizing arithmetic (S9–S11) *are* shared: the service
uses the same 0.5% rule as the research code, and the backtest's `risk_pct=0.005`
default is the same number. That is the one piece of research logic that
already transferred.

### 1.3 Absent everywhere: no code exists at all

Verified by grep across `src/`: no `session_filter`, `news_filter`,
`volume_filter`, `correlation_cap` or `cooldown` symbol exists in any module,
research or service.

| Missing control | Consequence for a demo execution path |
|---|---|
| Trading-session filter | A signal published outside market hours is executed at the next open, against a price that may have moved through the stop. |
| News filter | A signal published around a scheduled release is executed into the volatility spike that caused the signal. |
| Cooldown after a loss | Consecutive signals from the same channel stack into correlated exposure. |
| Correlation / exposure cap | Two XAUUSD positions are the same trade twice; the risk cap is applied per trade, so total risk can exceed 0.5%. |
| Volume/liquidity filter | Not checkable from the free read surface; recorded as a known limitation rather than a silent omission. |
| Daily-loss breaker | The account can lose more than one trade's risk in a session. |
| Max open positions | Unbounded concurrent positions on one instrument. |

### 1.4 Broker write capability that did not exist

Before this work, `src/` contained **no broker write path at all**.

* `src/rade/execution/` and `src/rade/risk/` were 0-byte packages — empty
  directories with an `__init__.py` and nothing else.
* The only TradeLocker calls in the codebase were
  `get_instrument_id_from_symbol_name` and `get_price_history`
  (`src/rade/ingestion/marketdata/tradelocker.py:34-36`) plus the quote calls
  in `quotes.py:38-41`.
* `create_order`, `modify_position`, `close_position` and the rest appeared in
  the repository **only as forbidden strings** — the disarmed method names in
  the read-only probe (`scripts/verify_tradelocker_readonly.py:41-50`) and in
  one test assertion (`tests/test_mirror_listener.py:336`).

So there was nothing to disable and nothing to leak: the demo execution path in
§2 onward is built from scratch rather than unlocked.

### 1.5 The lot specification was hardcoded, not broker-sourced

`service.py:367` built the instrument spec with literals:

```python
instrument=InstrumentSpec("XAUUSD", units, 0.01, 0.01, 50.0,
                          units_confirmed=confirmed),
```

The lot step `0.01`, the minimum `0.01` and the maximum `50.0` are written
into the source rather than read from the broker. They happen to be correct —
the demo instrument confirms `lotStep: 0.01`, `minLot: 0.01`, `maxLot: 50` —
but a hardcoded value cannot notice when the broker changes it, and the
contract size `units` was supplied by an environment variable that no code
path verified against the broker.

§2 replaces all four with broker-sourced values and a cross-check.

### 1.6 The exit policy was unresolved and is now specified

`service.py:165` reported `"exit_policy": "unresolved"`, and the paper
pipeline produced an intent with `"order_state": "would_fill_market"` and
`"filled": False`. Nothing in the codebase could close, reduce or amend a
position, because there was no position to manage.

The agreed exit policy, which §5 implements:

| Targets | Policy |
|---|---|
| 3 or more | Close 1/3 at TP1 → stop to breakeven → close 1/3 at TP2 → stop to TP1 → close the rest at TP3. Targets beyond TP3 are ignored and the ignore is recorded. |
| 2 | Close half at TP1 → breakeven after TP1 → close the rest at TP2. |
| 1 | Close all. |

Stops only ever move in the risk-reducing direction, and every stop update is
verified against the broker before it is treated as applied.

### 1.7 Summary of the audit

* 25 safeguards are enforced in the running service today (S1–S25).
* 6 controls exist in the research engines only, and are a porting list.
* 6 controls exist nowhere at all.
* The broker write surface did not exist and had to be built.
* The lot specification was hardcoded and is now broker-sourced with a check.
* The exit policy was unresolved and is now written down and implemented.

The audit's practical conclusion: the **intake** half of this system was
built to a standard that is worth keeping, and the **execution** half had to
be built to that same standard from nothing. That is why §2 through §9 are
mostly new code rather than edits to old code.

---

## 2. The execution adapter (spec step 2)

`src/rade/execution/gateway.py` is the only module in the project that talks
to the broker. It is built around three refusals.

### 2.1 It refuses any environment but demo

`assert_demo_environment` checks **both** the configured environment name and
the resolved host:

```python
if env != "demo":
    raise BrokerRefused("refusing a non-demo TradeLocker environment ...")
host = urlparse(base_url).hostname
if host != DEMO_HOST:            # "demo.tradelocker.com"
    raise BrokerRefused("refusing a TradeLocker base URL that is not the demo host")
```

The name alone is not enough. `TRADELOCKER_ENV` and the base URL are separate
settings, and a configuration that says `demo` while pointing at
`live.tradelocker.com` is a configuration that must fail rather than be
believed. There is no live-account code path to enable.

### 2.2 It refuses to guess

Every number that could be guessed is read from the broker and then checked.

| Value | Source | Never from |
|---|---|---|
| instrument id | `get_instrument_id_from_symbol_name("XAUUSD")` | a constant |
| trade route id | `get_trade_route_id(instrument_id)` | a constant |
| account number | `get_trade_accounts()` matched by id, then the SDK's login resolution | an assumption |
| contract units per lot | `lotSize` in `get_instrument_details` | a config default |
| quantity step | `lotStep` | `0.01` in source |
| minimum quantity | `minLot` | the SDK's `_MIN_LOT_SIZE` |
| maximum quantity | `maxLot` | a constant |
| tick size | `tickSize[0].tickSize` | a constant |

The broker's answer is then **cross-checked** against the value a human
confirmed. `BrokerSpec.verified` is false when:

* the broker reports no contract size, no step, no minimum, no usable maximum,
  no route id or no account number;
* `MIRROR_CONTRACT_UNITS_CONFIRMED` is not set, so no human has checked
  anything;
* the broker's `lotSize` disagrees with the confirmed value;
* the symbol is not `FULLY_OPEN`.

An unverified spec cannot place an order: `place_market` raises
`BrokerRefused` before it builds a request.

### 2.3 What the demo account actually reports

Read from the connected demo account (`docs/evidence/demo-execution-*.json`):

| Field | Value |
|---|---|
| environment | `demo` (`https://demo.tradelocker.com`) |
| account id / number | `2509174` / `2` |
| symbol | `XAUUSD` |
| instrument id | `1808` |
| trade route id | `816807` |
| `lotSize` (contract units per lot) | **100** |
| `lotStep` | **0.01** |
| `minLot` | **0.01** |
| `maxLot` | **50** |
| `tickSize` | **0.01** |
| `symbolStatus` | `FULLY_OPEN` |
| leverage | `31.25` |
| partial close supported | `tradingRules.supportPartialClosePosition: true` |
| trailing stop supported | `tradingRules.supportTrailingStop: true` |

Two findings came out of doing this rather than assuming it:

1. **`GET /trade/accounts` does not carry `accNum`.** It returns the account
   id, name, type, currency, status and the trading rules — but not the number
   the `accNum` header needs. The number is resolved by the SDK at login from
   the fuller account table. A lookup that matched the account id and then
   read `account["accNum"]` returned **0**, which made the whole specification
   unverified. The gateway now matches the id, and falls through to the SDK's
   value when the list has no number, instead of returning zero.
2. **The SDK's `_MIN_LOT_SIZE = 0.01` is a hardcoded class constant** with a
   `## TODO: this should probably be fetched per-instrument from BE` next to
   it. It happens to agree with this instrument, but it is not the authority,
   so the gateway uses `minLot` and the ladder uses that.

### 2.4 Where the broker actually keeps the live stop

This one cost a demo run to learn, and it is the difference between verifying a
stop move and *believing* one.

A position's stop and target are published in **two** places, and only one of
them is live:

| Where | Field | Behaviour |
|---|---|---|
| the filled entry order, in the **history** feed | `stopLoss` / `takeProfit` | **Frozen at submission. Never changes.** |
| the linked `stop` and `limit` orders, in the **open-order** feed | `stopPrice` (stop) / `price` (limit) | The live level. Changes when the position is modified. |

The position points at the second pair through `stopLossId` and
`takeProfitId`, and those ids appear in `GET /trade/accounts/{id}/orders`, not
in `ordersHistory`. Measured directly (`scripts/diagnose_broker_state.py`, demo
account, one 0.01-lot order, `modify_position(stop_loss=…)` between snapshots):

```
before  open orders   {id: …133, type: stop,  stopPrice: 3879.04}
after   open orders   {id: …133, type: stop,  stopPrice: 3889.04}   <- moved
before  history       {id: …132, type: market, stopLoss: 3879.04}
after   history       {id: …132, type: market, stopLoss: 3879.04}   <- stale
```

So the entry order's `stopLoss` is a receipt, not a state. Reading it would
have confirmed a stop move that the broker never applied — the one failure this
project cannot tolerate, because the whole point of the broker-side stop is
that it protects the position when this process is not running.

`_attached_levels` therefore reads the **open-order** feed and resolves the two
ids the position names. It also distinguishes *absent* from *unknown*: if the
feed cannot be read, the levels come back `known=False`, and nothing acts on
them — a verification fails, and reconciliation stops for review rather than
declaring the position unprotected.

---

## 3. Market entry (spec step 3)

`gateway.place_market` sends one market order with the stop and the final
target **attached at entry**:

```python
request = {
    "qty": qty, "side": side, "type": "market", "validity": "IOC",
    "tradableInstrumentId": spec.instrument_id,
    "routeId": spec.trade_route_id,
    "stopLoss": stop_loss, "stopLossType": "absolute",
    "takeProfit": take_profit, "takeProfitType": "absolute",
    "strategyId": strategy_id,
}
```

The contract was checked against the official reference for
`POST /backend-api/trade/accounts/{accountId}/orders`: `qty`, `routeId`,
`side`, `validity`, `type` and `tradableInstrumentId` are required; market
orders must use `IOC`; `stopLoss`/`stopLossType` and `takeProfit`/
`takeProfitType` attach the protective orders; the response is
`{"d": {"orderId": "123"}, "s": "ok"}`.

The point of attaching both at entry is the monitoring outage. If the process
dies between the fill and the first management tick, the position is still
protected by the broker's own stop and target. That is what makes the outage
survivable at all.

### 3.1 LIMIT signals are not copied

A LIMIT call is recorded as `not_copied` with the reason *"LIMIT orders
excluded by current policy"*, and no order is built:

```python
if entry_type != "market":
    return self._not_copied(result, message, trade_id,
                            "LIMIT orders excluded by current policy")
```

### 3.2 The correlation tag

Every order carries a `strategyId` derived from the event key:

```python
def trade_id_for(event_key: str) -> str:
    return f"RADE-{event_key[:24]}"[:31]
```

It is deterministic, so a restart recomputes it without reading the database,
and it is visible on `GET /orders`, `GET /ordersHistory` and `GET /positions`,
which is what makes reconciliation possible. It is 29 characters, under the
31-character limit the reference documents and the 32 the SDK enforces.

---

## 4. Persistence (spec step 4)

Two tables, added by `supabase/migrations/0003_trades.sql` and mirrored in the
local SQLite ledger:

* `mirror_trades` — one row per trade, holding the **current** state: the
  size, the published entry, the broker's fill price, the current stop, the
  targets, the plan, the ladder progress, the broker order id and the broker
  position id, plus the Telegram message id and revision.
* `mirror_transitions` — an **append-only** history. Every state change writes
  a row; nothing is ever overwritten, so the sequence survives.

The stages recorded are:

| Stage | Meaning |
|---|---|
| `validated` | the signal passed validation and a ladder was built |
| `policy` | recorded as `not_copied` (LIMIT) |
| `spec` | blocked because the broker specification is unverified |
| `submit` | the order request was sent |
| `accept` | the broker returned an order id |
| `fill` | the position exists at the broker |
| `tp1`/`tp2`/`tp3` | a partial close, a stop move, or a failed attempt at either |
| `target` | the final take profit was re-attached after a reduction |
| `reconcile` | the trade was brought back into agreement with the broker |
| `management` | automatic management stopped and a human was asked |
| `close` | the trade is closed |
| `cleanup` | the validation script closed a position deliberately |

Every row carries `broker_order_id`, `broker_position_id`, `message_id` and
`revision`, so a transition can be traced back to both the Telegram message
that caused it and the broker object it acted on.

RLS is enabled on both tables with no policies: the backend uses the
service-role key, which bypasses RLS, and the anon key can read nothing. The
dashboard never reads these tables directly.

---

## 5. The exit ladder (spec steps 3 and 6)

`src/rade/execution/ladder.py` is pure arithmetic: no broker calls, no I/O, so
it is tested exhaustively before it is trusted.

### 5.1 The policy, implemented literally

| Targets | Legs |
|---|---|
| 3 or more | close 1/3 at TP1, stop → breakeven; close 1/3 at TP2, stop → TP1; the rest closes at TP3 |
| 2 | close 1/2 at TP1, stop → breakeven; the rest closes at TP2 |
| 1 | close everything at that target |

Targets beyond TP3 are ignored **and recorded**: `ExitPlan.ignored_targets`
holds them and `description()` names them, so the ignore is never silent.

The final leg is `managed=False`. It is closed by the broker's attached take
profit, not by this process — which is exactly what keeps the position
protected through an outage.

### 5.2 Quantities are rounded down and never adjusted

```python
def round_down_to_step(value, step):
    return round(math.floor((value + 1e-12) / step) * step, 8)
```

Rounding down is the only safe direction: rounding a close quantity up would
close more than the plan permits. A quantity that rounds below the broker's
minimum is a **block**, not a smaller close:

```python
if quantity < min_lot:
    raise LadderError(f"the {index+1}th close would be {quantity:g} lots, below "
                      f"the broker's minimum of {min_lot:g}; the ladder is "
                      "blocked rather than changed")
```

For a 1-lot position the thirds are `0.33 / 0.33 / 0.34` — the last leg takes
whatever is left rather than a computed third, so the ladder always sums to
the position exactly.

### 5.3 Stops only move towards the price

`stop_move_is_risk_reducing` refuses a move that widens the risk or puts the
stop on the wrong side of the market:

* a long stop may only move **up**, and must stay **below** the price;
* a short stop may only move **down**, and must stay **above** the price.

"Breakeven" is resolved at execution time to the position's **actual average
fill price**, not the published entry. A published entry of 3915 that filled
at 3915.20 puts breakeven at 3915.20.

### 5.4 Every move is verified

`modify_position` returning `True` is not treated as proof. The engine re-reads
the position, resolves the attached stop order by id **from the open-order
feed** (§2.4 — the entry order's `stopLoss` never updates), and compares the
level:

```python
def _verify_stop(self, trade, position_id, expected) -> tuple[bool, str]:
    position = self._position_for(trade.trade_id)
    levels = self._attached_levels(position)
    if not levels.known:
        return False, "the broker's open orders could not be read"
    if levels.stop is None:
        return False, f"the broker publishes no stop order for position {position.id}"
    if abs(levels.stop - expected) > 1e-6:
        return False, f"the broker reports a stop of {levels.stop:g}, not {expected:g}"
    return True, f"the broker reports a stop of {levels.stop:g}"
```

The verdict carries the broker's actual number, so the ledger and the
notification say what went wrong rather than only that something did. A move
that cannot be confirmed is **retried on the next pass** and only then escalated
(§5.4.1).

#### 5.4.1 A refused stop move is retried, never skipped

`legs_done` counts closes. A close can succeed while the stop move that should
follow it is refused — the price moved between the two calls — and advancing
the ladder would lose that move permanently. The engine therefore computes the
stop the last completed leg should have left behind and, if `current_stop` does
not match it, applies that move *before* the next leg. The retry is bounded by
`max_stop_attempts`, after which a human is asked and the note says the position
is still protected by the stop that is attached.

### 5.5 The final target is maintained, not assumed

A partial close can drop the attached target. After every leg the engine
checks that the position still carries the final target and re-attaches it if
not. A position must never be left with a stop and no target.

### 5.6 A fill beyond the first target is not traded

The plan is built from the published entry, and the fill is not obliged to
match it. The demo account demonstrated the consequence: a published entry of
`3887.59` filled at `3888.33`, which is *past* a TP1 of `3887.79`. Walking that
ladder would have moved the stop to TP1 after TP2 — a level *behind*
breakeven — and the risk-reducing check correctly refused it two legs in.

The refusal was right, but it came too late. `confirm_fill` now tests the fill
against the first target immediately, and a fill at or beyond it stops
management before any leg runs:

```python
passed = (fill >= first if side == "buy" else fill <= first)
```

The reason recorded says the ladder cannot be walked as written, names the
slippage, and states that the broker-side stop and target remain attached. The
policy is not rewritten to accommodate the fill, because a policy that quietly
adapts to whatever the broker did is not a policy.

---

## 6. Reconciliation and safe retries (spec step 5)

### 6.1 Never resubmit

Before an order is sent, the engine asks the broker whether an order carrying
this trade's tag already exists:

```python
existing = self._existing_order(trade.trade_id)
if existing is not None:
    self.log(f"trade {trade.trade_id}: the broker already has order "
             f"{existing.id}; not resubmitting")
    return self.reconcile_trade(trade)
```

The trade's own ledger row is also opened with `INSERT OR IGNORE` on a primary
key, so a second delivery of the same event reconciles instead of executing.
Between the two, a restart, a retry, a duplicate delivery and two overlapping
processes all resolve to the same outcome: adopt, never re-place.

### 6.2 Reconciliation on startup

`reconcile()` runs **before** the listener starts, so a restart cannot race a
new signal. For each trade in a non-terminal state:

| Broker state | Action |
|---|---|
| a position with this tag exists, and its stop and target match the plan | resume management |
| a position exists but the attached target is not the plan's final target | **stop management**, flag for review |
| a position exists but the attached stop is not the expected stop | **stop management**, flag for review |
| a position exists with a different quantity and no leg has run | **stop management**, flag for review |
| no position, but a filled order with this tag | record `closed` with the fill price |
| no position and an unfilled order | record `accepted` and wait for the fill |
| no position and no order | record `validated`; it may be submitted once |

The ownership test is the tag. A manual position in the same account has no
tag and is never touched — there is a test for exactly that.

### 6.3 "TP ordering cannot be established" means stop

The spec asks that if the target ordering cannot be established after an
outage, automatic management stops and the trade is flagged. That is the
second and third rows above. The position is left protected by the broker's
own stop and target, and a `review` notification says so.

---

## 7. Notifications (spec step 7)

Each lifecycle event is its own notification, sent through the same slot
machinery the paper mirror already had (`notify_event` in `notify.py`), so a
failed send is recorded as failed and retried without re-sending the ones that
arrived.

| Kind | When | Says |
|---|---|---|
| `signal` | the ladder was built | `[SIGNAL] SELL XAUUSD market 0.05 lot; SL …; final TP …; Plan: …` |
| `accepted` | the broker returned an order id | `[ACCEPTED] TradeLocker (demo) accepted order 1234 … Awaiting the fill confirmation.` |
| `filled` | the position exists | `[FILLED] TradeLocker (demo) confirms position 5001 …` |
| `partial` | a leg closed | `[TP1 HIT] Closed 0.01 lot(s) … Remaining 0.04 lot(s).` |
| `stop` | a stop move was verified | `[STOP MOVED] TP1 done: the broker-side stop moved to 3915 …` |
| `closed` | the position is gone | `[CLOSED] …` |
| `review` | management stopped | `[REVIEW] … The broker-side stop and target are still in place.` |
| `blocked` / `rejected` / `not_copied` | no order | the reason |

**"Copied" is never said before the broker confirms.** The word does not
appear in any notification the engine sends; `filled` is the event that means
the position exists, and there is a test asserting that no message containing
`COPIED` is sent before it.

---

## 8. The dashboard (spec step 8)

Two new read-only routes and one new panel group.

* `GET /api/trades` — the trade list, with the broker's own numbers: the
  position id, the order id, the fill price, the stop the broker holds, the
  final target, and the ladder progress.
* `GET /api/trades/{id}` — one trade plus its full transition history.

Vercel proxies them through `web/api/trades.js` and `web/api/trades/[id].js`.
The trade id is validated against `^[A-Za-z0-9_-]{1,64}$` **before** it is put
into the upstream path, so a crafted id cannot escape the `/api/trades`
prefix. The browser still never sees `MIRROR_API_TOKEN` or the Supabase key:
the proxy holds them server-side.

The page shows a mode badge (`demo execution` or `paper only`), a
`live trading disabled` badge that is never removed, the broker specification
and whether it is verified, a **Broker position** panel with the live position,
a **Trades** table, and a click-through **Trade detail** with every transition.

There is still no write endpoint: `POST`/`PUT`/`PATCH`/`DELETE` return 405.

---

## 9. What this does not do

Every one of these is a limit of the delivered system, not a roadmap item.

**It does not trade a live account, and it cannot be pointed at one.** The
gateway checks the environment name *and* the resolved host against
`demo.tradelocker.com`; anything else raises before a client is built. There is
no configuration flag that turns this off.

**Nothing here is a profitability claim.** No result in `docs/evidence/` is a
backtest, and none of them was selected for being a win. A demo validation that
ends with the stop taking the position out is recorded exactly as that, because
the point of the run is to prove the *mechanics*, not the edge.

**The ladder assumes target spacing it does not control.** One management pass
costs several REST round-trips — roughly 10 s against this host, dominated by
provider latency, not by this code. A ladder whose targets are spaced more
tightly than the price travels in one pass will be consumed by the broker's own
attached final target before the engine can act on the first leg; the engine
then records "closed at the broker before any ladder step ran", which is true
and is not the same as a completed ladder. This is why the published exit policy
is a *policy* about targets and not a promise that every signal produces three
closes.

**Monitoring stops when the host stops.** On Render's free tier the service
sleeps after ~15 minutes of inactivity, and there is no free worker or cron, so
the listener and the manager run as threads inside the web process. While it is
asleep nothing is managed. The position stays protected because the stop and the
final target are attached at the broker, which is the reason they are attached
there and not simulated locally.

**After an outage the ladder may not be recoverable.** If the attached levels
cannot be read on restart, management stops and the trade is flagged rather than
guessed at. The position is still protected; it is simply no longer managed
automatically, and a human is expected to look.

**Some controls from the research code are still not enforced on this path.**
A maximum hold time, commission and a correlation cap between instruments do
not exist here (audit §1.2). Session filters, news filters, cooldowns and a
volume filter do not exist anywhere in the repository (audit §1.3). The daily
loss breaker, the open-position limit, the drawdown stop and the spread and
slippage limits **are** now enforced (§11), and the 0.5 % per-trade cap was
already enforced.

**One instrument, one account, one position per signal.** XAUUSD only, the
configured account only. The correlation tag is derived from the Telegram event
key, so two signals that arrive as one message are one trade; the same signal
posted twice as two messages is caught by the text-hash check in §11.4.

**Notifications depend on the Telegram configuration.** Without it every event
is still written to the ledger with its outcome; it is recorded as failed, and
a failed send is retried, never silently dropped.

---

## 10. Evidence from the demo account

Everything below was produced by `scripts/validate_demo_execution.py` against
the connected demo account. The JSON files are in `docs/evidence/`. Failed runs
are kept, not deleted: the failures are where the defects were found.

### 10.1 A complete ladder

`docs/evidence/demo-execution-2026-11-13T15-20-54Z.json` — trade
`RADE-0686329c6134de20a278b15e`, closed by the broker, not by the script
(`closed_by_script: false`).

| Event | What the broker did | Verified how |
|---|---|---|
| entry | accepted order `216172782144392576`, position `216172782118679204` at `3887.39` | the position exists, not merely the ack |
| TP1 | closed `0.16` at `3885.40` | the position's quantity dropped |
| TP1 stop | `3898.40` → `3887.39` (breakeven at the **fill**, not the published entry) | the open `stop` order's `stopPrice` |
| TP2 | closed `0.16` at `3882.40` | the position's quantity dropped again |
| TP2 stop | `3887.39` → `3885.40` (TP1) | the open `stop` order's `stopPrice` |
| final | the remaining `0.18` closed at the broker | the position disappeared; the engine did not close it |

The plan was `0.16 + 0.16 + 0.18 = 0.50` lots, rounded down to the broker's
`0.01` step, with the last leg left to the broker's own attached target. The
stop moved only towards the price, twice, and each move was read back from the
broker before it was recorded.

### 10.2 A restart adopts instead of resubmitting

`docs/evidence/demo-reconcile-2026-11-13T15-24-50Z.json` — trade
`RADE-360e05b5613bd7f2598ab608`.

The restart is a real one: a **new login** (new client, new session) and a
**new engine** over the **same** durable ledger, which is exactly what a process
restart leaves behind.

```
before_restart   orders 387, positions 1
after_restart    orders 387, positions 1   -> order_delta 0
reconcile        {considered: 1, open: 1}
transition       "the position 216172782118679648 matches the plan"
```

`resubmitted: false`. The order count is unchanged across the restart, so no
second order was sent.

### 10.3 Two defects the demo account found that the tests had not

1. **A stop move that was applied but never recorded.** The first runs showed
   `tp1 stop_failed`, then a ladder that had advanced past a stop that was
   never applied. Fixed by retrying the pending stop before the next leg
   (§5.4.1); the retry path is visible in
   `demo-execution-2026-11-13T15-12-17Z.json` as `stop_retried: tp1`.
2. **Verification was reading a field that never changes.** Detailed in §2.4.
   This one would have made every stop move look verified while the broker held
   the old level, and it was only visible because the demo run disagreed with
   the code.

A third finding was not a defect but a limit: the demo feed publishes a crossed
book (bid above ask) for a moment, and intake blocks it. One validation run was
recorded as `blocked: broker quote is invalid` for exactly that reason
(`demo-execution-2026-11-13T15-09-54Z.json`), which is the safeguard working.

---

## 11. Pre-trade risk controls

The per-trade cap answers one question: how much can *this* signal lose. It
says nothing about the day, the account, or whether the market has moved since
the channel posted. Section 1.2 recorded those as absent; this section is what
closed them.

Two rules shape all of it:

* **A guard only ever prevents a new order.** It never closes a position, never
  moves a stop, and never abandons an open trade. A daily-loss breaker that
  also abandoned open risk would turn a bad day into an *unmanaged* bad day,
  which is worse than the loss it is trying to cap. The broker-side stop and
  target keep protecting the position either way.
* **Unknown is not safe.** If the equity, the risk state, the pause flag or the
  duplicate lookup cannot be read, the answer is "do not trade", with a reason
  naming which read failed.

### 11.1 What is enforced, and where

| Control | Default | Measured against | Env |
|---|---|---|---|
| Open positions | 3 | trades in an active state, excluding the one being opened | `MIRROR_MAX_OPEN_POSITIONS` |
| Daily loss | 2 % | the equity the UTC day opened at | `MIRROR_DAILY_LOSS_FRACTION` |
| Drawdown stop | 5 % | the highest equity ever recorded | `MIRROR_MAX_DRAWDOWN_FRACTION` |
| Spread | 5.0 | `ask − bid` at the moment of the order | `MIRROR_MAX_SPREAD` |
| Slippage | 5.0 | the distance from the published entry to the price the order would get | `MIRROR_MAX_SLIPPAGE` |
| Same call twice | 900 s | the text hash of every trade opened | `MIRROR_DUPLICATE_WINDOW_S` |

The checks run in that order, most categorical first, so the reason recorded
names the most serious problem rather than whichever ran first. A drawdown
breach is reported before a daily-loss breach because it is the wider condition.

**Set any of them to `0` to switch it off.** The service logs which are off at
startup, and `/api/status` reports them under `limits.disabled` rather than
omitting them, so a switched-off control cannot be read as a passing one. The
dashboard renders each as "enforced" or "disabled by configuration" for the same
reason.

### 11.2 The state that has to survive a restart

The daily breaker needs the equity the day opened at, and the drawdown stop
needs the peak. Both are rows in `mirror_risk_state`, not process variables: on
a host that restarts, a breaker that resets on every deploy is decorative.
`mirror_control` holds the pause switch for the same reason.

The day is UTC. The first evaluation after midnight opens the new day at
whatever the equity is then; the peak is *not* reset, because drawdown is a
property of the account and not of the calendar.

### 11.3 Equity is read for every order

`RiskConfig` now takes an `equity_provider`. With the executor running it is
called for every signal, so a process that has been up for days sizes against
the account as it is now. A provider that raises blocks the signal — the
startup number is never substituted, because a stale balance silently changes
the risk actually taken.

### 11.4 The same call posted twice

This one came from the channel, not from a test. Its two most recent signals
were each posted **twice**, as two messages with two ids and identical text:

```
18760  XAUUSD SELL NOW 3926✅ ...      18761  (identical)
18788  XAUUSD BUY NOW 3909️ ...        18789  (identical)
```

`event_key` is a hash of the channel, the message id and the revision, so the
database's uniqueness constraint sees two distinct events — correctly, from its
point of view. Nothing downstream would have noticed, and a live copier would
have placed two orders for one idea, doubling the risk on a single view of the
market.

Every trade now records the `text_sha256` of the message it came from, and an
identical text inside `MIRROR_DUPLICATE_WINDOW_S` is recorded as *not copied*
with the trade it duplicates named. The check reads the trade table rather than
the event table, so it works however `execute` was reached — through the
listener or directly from a script — and a lookup that fails blocks rather than
guesses.

### 11.5 The pause switch

Pausing has to survive a restart and has to be flippable without a redeploy, so
it is a row (`mirror_control`), not a variable or a file on an ephemeral disk.

* `POST /api/pause` requires a reason and sets it.
* `POST /api/resume` clears it.
* Both need the same bearer token as the read routes, and both are the *only*
  writes the service accepts. Every other `POST`/`PUT`/`PATCH`/`DELETE` is still
  `405`, and neither route can place, modify or close anything at the broker:
  the entire surface is one boolean.

A pause stops management as well as entries. An operator who pauses is
intervening by hand, and having the manager move stops underneath them is the
opposite of what they asked for. The position stays protected by the stop and
target attached at the broker.

The asymmetry is deliberate: a control read that *fails* does not stop
management, because management is risk-reducing work; it only stops entries.

### 11.6 The controls run against the live account, proved without an order

The unit tests prove the arithmetic against a broker double. They cannot prove
that the numbers being compared can be read from the real account, or that the
state they depend on can be written where it will survive a restart. So
`scripts/probe_guards.py` does exactly that against the demo account and is
built so that it cannot place an order:

* The signal it drives is **synthetic and labelled as such**. It exists to carry
  the guards, not to claim a signal was copied.
* Each case is configured so a guard must refuse before submission — the pause
  case at the first check, the spread case at the sixth. Anything other than a
  blocked outcome is reported as a failure, because it would mean a control did
  not fire.
* The ledger is a throwaway file, and it checks the broker afterwards for
  positions and orders it must not have created.

The run on 2026-11-13 (`docs/evidence/guard-probe-2026-11-13T16-09-39Z.json`)
reported:

```
live quote: bid 3878.36 ask 3878.9; equity 101,034.17
open positions: 0; open orders: 0

paused:  blocked, "execution is paused: probe: pause switch check"
spread:  blocked, "the spread is 0.16, wider than the 0.0001 limit"
risk state persisted: {'day': '2026-11-13',
                       'day_start_equity': 101034.17, 'peak_equity': 101034.17}
broker order id: None   position id: None   positions after: 0
```

That is the first time the day's opening equity and the peak were written from
the real account rather than from a fixture, and the first time a guard refusal
was recorded against a trade row built by the real pipeline. It is still not
evidence that a signal was copied: no signal was.
