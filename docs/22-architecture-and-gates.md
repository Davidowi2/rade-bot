# 22 — Architecture and the gates to a verified mirror

> **SUPERSEDED on the exit ladder.** The ladder row in §"What exists" describes
> a partial-exit ladder (thirds at TP1/TP2, stop to breakeven) and already
> carries a note that it does not match the recorded preference. The
> implemented policy is **hold the full position, move the stop to TP1 after
> price passes TP1, close nothing until the final target** — see
> `docs/23-behavior-and-risk-spec.md` §5 A1.

Status: **plan only**. Nothing in this document has been implemented, deployed or
committed as a change to behaviour. It records what the repository does today,
what the intended behaviour is, and the ordered evidence required to close the
gap between the two.

Every claim is tagged:

* **verified** — read in the repository at the cited path, or produced by a
  command whose output is quoted.
* **inferred** — follows from verified code but has not been exercised.
* **unknown** — not established by anything in the repository.

The branch inspected is `feature/tradelocker-demo-execution` at `2cd1990`,
working tree clean, 10,039 lines of Python under `src/`, 376 Python tests under
`tests/`.

**Nothing in this document is evidence that a service is deployed or running.**
The only proof of a deployment would be a live `/healthz` and `/api/status`,
which are not included here. Where a value is cited from `render.yaml` it is a
**blueprint setting** — an intention, not an observation.

## Revision 2 — changes from review

| # | Change | Section |
|---|---|---|
| 1 | Blueprint settings are no longer described as deployed state. | throughout |
| 2 | The exit policy is no longer described as agreed. The recorded preference **differs** from the implemented ladder, and the open details are enumerated. | §1.1 |
| 3 | Shadow validation no longer claims to reconcile against the trader's fills. It reconciles the message, the parse and the quote. | Gate 2 |
| 4 | The fixed-lot rule is specified to **fail closed**, and two existing non-fail-closed behaviours are named. | Gate 1 |
| 5 | The destination gate records the published rule text, the account holder's decision, and the two entry modes it separates. | Gate 5 |

## Revision 3 — decision recorded, spec split out

| # | Change | Section |
|---|---|---|
| 1 | The decision record now states what the decision **is**: the account holder's decision to proceed, not Blueberry approval and not written consent. The intended mode is recorded as **automatic mirroring**. | §5b |
| 2 | The earlier suggestion to default to the assistive mode is withdrawn — the choice has been made, and the document records it rather than recommending against it. | §5b |
| 3 | An imprecise claim was corrected: the ladder already triggers on a quote observation, not on a fill. What the confirmed policy removes is the close at TP1, not the trigger. | §1.1 |
| 4 | The exit policy is no longer "not agreed". It is confirmed and moved into `docs/23-behavior-and-risk-spec.md`, which is where the unresolved mechanics are settled. | §1.1, `docs/23` |

---

## 1. Current state, by area

| Area | What the code does | Tag | Where |
|---|---|---|---|
| Execution environment | Refuses any environment name other than `demo`, **and** any host other than `demo.tradelocker.com`. Both are checked; a config saying `demo` while pointing at a live host is refused. | verified | `src/rade/execution/gateway.py:67-86` |
| Live-account path | There is none. `MIRROR_EXECUTE=1` reaches the TradeLocker **demo** account and nothing else. | verified | `gateway.py:76-80` |
| Destination platform | Zero occurrences of `blueberry`, `mt4`, `mt5`, `ctrader`, `metatrader` anywhere in the repository. | verified | `grep -rni` over the tree |
| Broker adapters | Exactly one: TradeLocker. `src/rade/execution/` contains `broker.py` (protocol), `gateway.py` (the only broker I/O), `engine.py`, `guards.py`, `ladder.py`. | verified | directory listing |
| Quote provider | One provider, `tradelocker_quote_provider`, and it also refuses a non-demo environment. The blueprint sets `MIRROR_QUOTE_PROVIDER` to `"none"`, which would block every market (NOW) call at intake. | verified (code) / blueprint (setting) | `src/rade/observer/quotes.py:1-38`, `render.yaml:77-78` |
| Sizing | Two modes. `risk_fraction` (the earlier default): `risk_budget = equity × risk_fraction`, floored to the broker's lot step, capped at `MAX_RISK_FRACTION = 0.005`. `fixed_lots` (the confirmed mode): exactly the configured lot or a refusal, with ten named refusal conditions and no clamping. | verified | `src/rade/execution/sizing.py`, `mirror_pipeline.py:556, 613-628` |
| Risk limits | `max_open_positions` 3, `daily_loss_fraction` 0.02, `max_drawdown_fraction` 0.05, `max_spread` 5.0, `max_slippage` 5.0. Setting one to `0` disables it and it is reported under `disabled`, so a switched-off check cannot be read as a passing one. | verified | `src/rade/execution/guards.py:31-90` |
| Guard scope | A guard only ever prevents a **new** order. It never closes, never moves a stop, never abandons an open trade. Unreadable input blocks rather than fails open. | verified | `guards.py:9-17, 121-210` |
| Spread/slippage units | Both are **price units**, not points or pips. The blueprint sets `5`, and its own comment notes XAUUSD's normal spread is ~0.2 — a 25× tolerance, too permissive to count as a meaningful filter on this instrument. | verified (units) / blueprint (value) | `render.yaml:147-150`, `guards.py:43-44` |
| Signal age | `DEFAULT_SIGNAL_MAX_AGE_S = 120`, measured against `received_at` vs `posted_at`, not wall clock. `stale` is its own disposition. The edit guard runs **before** the age guard, so a message first seen in edited form is `manual_review`, not merely `stale`. | verified | `mirror_pipeline.py:360-363, 496-512` |
| Telegram delivery | A live push has never been observed. `catch_up=True` replayed backlog ids 15-17 on connect but never delivered id 18, posted seconds later. The poll (`catchup_every = 15.0`) is therefore the delivery path, and it is an idempotent gap fill. | verified | `src/rade/observer/listener.py:228-240`, `docs/21-mirror-test-milestone.md:340-352` |
| Reconnect | `MIRROR_RECONNECT_S = 900` in the blueprint. Alone it would always exceed the 120 s age limit, which is why the poll exists. Both are reported at startup and on `/api/status`. | verified / blueprint | `render.yaml:97-100`, `docs/21:391-395` |
| Exit ladder | 3+ targets: a third at TP1 → stop to breakeven; a third at TP2 → stop to TP1; the remainder closed by the **broker's attached take profit at TP3**. 2 targets: half, breakeven, rest at TP2. 1 target: whole position. Targets beyond TP3 are ignored and the ignore is recorded. Quantities floor to the step. Stops move only in the risk-reducing direction. **This does not match the recorded preference — see §1.1.** | verified | `src/rade/execution/ladder.py:1-43, 53-82` |
| NOW-only | LIMIT signals are parsed and recorded but never sent: the engine returns early when `entry_type != "market"`. | verified | `src/rade/execution/engine.py:442, 454-455` |
| Position management | A failed position read returns a `POSITIONS_UNREADABLE` sentinel, not `None`. All four callers handle it. `orphaned_positions()` reports trades the reconciliation cannot see because they were wrongly marked terminal. | verified | `src/rade/execution/engine.py` |
| Persistence | `mirror_events`, `mirror_trades`, and a single-row pause table. Idempotency is enforced by the store on `event_key`, not by callers. SQLite locally, Supabase hosted. | verified | `mirror_pipeline.py:678-790, 1144-1198`, `src/rade/observer/store.py` |
| Kill switch | `POST /api/pause` and `/api/resume`, bearer-authenticated, reason and name required, persisted so it survives a restart. It blocks new orders only. There is deliberately no "flatten everything" endpoint. | verified | `src/rade/observer/service.py:416-449` |
| Dashboard | One static page, 550 lines, reading `/api/status`, `/api/trades`, `/api/events`, `/api/intents`. Shows the intake limits and a delivery-interval card that warns when the interval sits outside the age limit. | verified | `web/index.html` |
| Hosting | The blueprint targets Render's free tier, whose own comment records that it spins down after 15 minutes without inbound traffic, has an ephemeral disk, and runs the listener as a thread inside the web process. Whether such a service is actually deployed is **unknown**. | blueprint / unknown (deployment) | `render.yaml:1-18` |
| Tests | 376 Python tests across 15 files; Node tests under `web/test/`. | verified | `grep -c` over `tests/` |

### 1.1 Exit policy: recorded preference versus the implemented ladder

The account holder's stated preference:

> keep the position through TP1, move the stop after price passes TP1, and close
> the remainder at TP3.

The implemented ladder does something different: it closes a third at TP1 and a
third at TP2, moving the stop after each partial fill, and leaves only the last
third to TP3. **These are not the same policy, and the difference is material** —
one takes profit three times and one takes it once.

So the exit policy is **not agreed in code**. It is now confirmed as the
intended behaviour (`docs/23`), and the code still implements the other policy.
That gap is the first thing the build has to close.

**A correction to the first version of this section.** It said the current
ladder moves a stop "because a leg filled". That was imprecise. The ladder
advances on a *quote observation* — `target_reached` compares the bid (long) or
the ask (short) against the leg's target (`engine.py:974`) — and only then closes
a quantity, verifies it, and applies the stop move (`engine.py:1001-1049`). So
the trigger is already a price observation, and the preference does not replace
the mechanism. What it removes is the **close at TP1**. That makes the change
smaller than it first appeared.

**The unresolved mechanics are not enumerated here.** The first version of this
section carried its own list, D1-D8. That list has been withdrawn: it now
duplicates `docs/23-behavior-and-risk-spec.md §5`, which states the same questions
as E1-E7 with concrete options and their consequences. Two lists of open questions
in two documents is the drift this project is trying to eliminate, so
**`docs/23` §5 is the only authority** for what is undecided about the exit.

The short version: the trigger, the stop level, the rule for signals with fewer
than three targets, TP2's role, gap handling, and fills beyond TP1 are all open,
and none of them will be invented by the implementer.

**Until `docs/23` §5 is settled, no execution code is written.** The answers
belong in the spec, not in a code review.

### What this means for the intended behaviour

| Intended | Status | Note |
|---|---|---|
| Copy NOW/market signals only | **already true** | `engine.py:454-455` |
| Fixed 0.03 lot | **does not exist** | sizing is risk-fraction; needs a new mode, not a new value |
| 0.03 accepted only when the broker spec and risk checks confirm it | **partly true** | the spec check exists; the risk check is per-fraction, not per-lot; and the failure behaviour is not fail-closed (§Gate 1) |
| Manage the stop as agreed | **confirmed, not implemented** | the policy is frozen in `docs/23`; the ladder implements the other one — §1.1 |
| Close the remainder at TP3 | **true, with a caveat** | the final leg is closed by the *broker's* attached TP, not by our order, so we never observe that fill directly — we infer it from the position disappearing |
| Pending LIMIT signals out of scope | **already true** | recorded as `pending_limit`, never sent |

---

## 2. Data flow

```
Telegram channel
  │  push: never observed          poll: every 15 s (MIRROR_CATCHUP_EVERY_S)
  ▼
MirrorListener ................ listener.py
  │  baseline / cursor / heartbeat / reconnect / duplicate suppression
  ▼
evaluate_message .............. mirror_pipeline.py
  │  parse → edit guard → age guard (120 s) → quote freshness (2 s)
  │  → geometry (stop crossed, target crossed) → size → intent
  ▼
Ledger ........................ SQLite | Supabase
  │  mirror_events · mirror_trades · pause row
  │  idempotency on event_key; identical-text window 900 s
  ├─────────────► ExecutionEngine ...... engine.py   [only if MIRROR_EXECUTE=1]
  │                 guards.evaluate() → gateway.place_market() → ladder
  │                 → manage loop every 5 s → TradeLocker DEMO only
  └─────────────► Notifier ............ notify.py → Bot API → Telegram
                                        one message per lifecycle event
  ▼
HTTP service ................. service.py
  /healthz · /api/status · /api/trades · /api/events · /api/intents
  /api/review · POST /api/pause · POST /api/resume
  ▼
Dashboard .................... web/index.html (static, Vercel)
```

Two properties of this shape matter for every gate below:

1. **The ledger is the system of record, not the process.** A restart re-reads
   the cursor and the trades from the store, reconciles against the broker, and
   only then resumes. Evidence for a gate therefore comes from the ledger and
   the broker, never from a log line.
2. **The executor is optional and separable.** With `MIRROR_EXECUTE=0` the same
   intake path runs and records intents without ordering. That is what makes
   Gate 2 possible without new code — once a quote provider is wired.

---

## 3. Gates

Each gate has one pass condition and one artefact. A gate is closed only when
the artefact exists and a second person can reproduce it from the commands in
`docs/21 §12`.

### Gate 1 — Behaviour and risk spec (freeze before touching money math)

Tasks, in order:

1. Write the spec as a document, not as defaults in code: entry filter, sizing,
   exits, loss limits, and the full refusal list.
2. **Settle the exit policy** (`docs/23` §5): answer E1-E7, then change the
   ladder. The decision list lives in the spec, not here.
3. **Add an explicit fixed-lot mode.** It must be a distinct mode, not a
   `risk_fraction` that happens to produce 0.03.
4. **Make the fixed-lot rule fail closed.** This is a requirement, not a
   preference.

#### The fixed-lot rule refuses; it never adjusts

The rule is: the configured lot is valid or the signal is refused. There is no
third outcome. It refuses, with a named reason recorded in the ledger and sent
as a notification, when any of the following holds:

* the lot is not representable at the broker's step (`lot / lot_step` is not an
  integer);
* the lot is below the broker's `min_lot`;
* the lot is above the broker's `max_lot`;
* the resulting USD risk — `lot × contract_units × stop_distance` — exceeds the
  configured per-trade ceiling;
* the contract size or instrument specification is not verified.

It must never round to a nearby valid size, never clamp, never substitute
another size, and never drop the signal silently. **0.03 lots is not itself a
risk limit**: the same lot is a different amount of money on every signal,
because the stop distance changes. The ceiling is what makes it a limit, and the
ceiling has to come from verified account information — not from a default.

Consequently the fixed-lot mode must **refuse to start** without a ceiling
configured. A missing ceiling is a configuration error, not a reason to fall
back to risk-fraction sizing.

Two existing behaviours contradict this and are to be changed (**verified**):

* `mirror_pipeline.py:626-627` — `if lots > instrument.max_lot: lots =
  instrument.max_lot` silently reduces the size, so the risk actually taken is
  not the risk that was computed. This is the fail-open case.
* `mirror_pipeline.py:620-625` — `lots < min_lot` returns
  `STATUS_SKIPPED_MIN_LOT`. A skip is not a named refusal; under the fixed-lot
  rule the reason must state which of the five conditions failed, so it is
  visible in the ledger and in the notification.

Account-specific values that still need verification (all **unknown** until
checked against the destination broker, not against the demo account):

| Value | Why it matters |
|---|---|
| `contract_units_per_lot` for the destination symbol | converts 0.03 lots into USD risk |
| `lot_step`, `min_lot`, `max_lot` | whether 0.03 is even representable |
| exact symbol name and suffix | `XAUUSD` on one broker is `XAUUSD.pro` on another |
| account currency and conversion to USD | a non-USD account changes every risk number |
| the per-trade USD ceiling | it cannot be set without the four values above |
| daily loss limit, max loss, min trading days | the firm's numbers, not ours |
| news-window and weekend-holding rules | a rule breach voids the account regardless of P&L |

**Pass:** `docs/23` exists with no `OPEN` item left; every refusal case has a
demonstrating test; no value is inherited from the demo account without being
marked as needing re-verification.
**Evidence:** the document, plus `pytest` output listing the refusal tests.

### Gate 2 — Shadow validation (the simulation)

What runs: the real channel, a working quote provider, `MIRROR_EXECUTE=0`.
Every market signal produces a row with the quote it saw, the size it computed,
the stop and targets, and either an intent or a named refusal. No order is sent.

**Blocking prerequisite (verified gap):** with `MIRROR_QUOTE_PROVIDER=none`
every market signal is blocked at intake for want of a quote, so a shadow run
today would produce refusals rather than intents. A demo TradeLocker login, or a
recorded-quote replay, is required first.

#### What a shadow run can and cannot establish

It **can** establish, from the ledger:

* receipt — the source (`push` or `catchup`), `posted_at`, `received_at`, and the
  age at which the signal reached the pipeline;
* parse fidelity — side, entry, stop and targets, spot-checked by a human
  against the message text;
* the quote at receipt — bid, ask, and its age against the freshness budget;
* the decision — intent or refusal, with the named reason;
* the size — whether 0.03 would have been valid, and the USD risk that implies;
* consistency — the same input produces the same decision on replay;
* completeness — no market signal was dropped without a row;
* that nothing was sent — the broker's order count is unchanged.

It **cannot** establish, and this document will not claim:

* what the trader actually filled, at what price, or where his stop really sat in
  his own broker;
* whether his published stop was ever actually hit;
* whether copying him would have been profitable;
* any edge at all.

Reconciling against his fills would require his broker records. We do not have
them and should not assume we can get them. If he publishes results, that is his
claim, not evidence available to us.

**Pass:** over at least five trading days, every market signal has a row; the
parse matches the message text on human spot-check; the quote is within budget
or the refusal names why not; the broker's order count is unchanged before and
after.
**Evidence:** `scripts/export_mirror_evidence.py` output, the broker order count
before and after, and the spot-check sheet.

Five days is a **consistency sample**, not a profitability proof. It answers
"does the pipeline record this trader's signals correctly and consistently",
which is the question that has not yet been answered.

### Gate 3 — Isolated demo validation

Environment: the private channel `@rade_mirror_test_20261113` and the existing
demo broker path. This is a re-run of the `docs/21` matrix against the **new**
fixed-lot spec and the settled exit policy, because changing either changes every
money number and every exit.

Cases, each with an expected ledger status, an expected notification set, and an
expected change in the broker's order count:

duplicate text with a new message id · edit after posting · signal older than
120 s · reconnect gap · quote stale · crossed book · market already through the
stop · broker-rejected order · stop move after TP1 · TP3 closure · restart
mid-trade · unreadable position read · orphaned position · lot not representable
· lot below minimum · lot above maximum · stop risk above the ceiling.

**Pass:** every case matches its expected triple, and no case leaves a trade in
an unverified state.
**Evidence:** one ledger row per case, the notification transcript, and the
broker order count delta per case.

### Gate 4 — Deployment readiness

Known constraint (blueprint, **verified as an intention**): the free Render tier
spins down after 15 minutes idle and wipes its disk. For a paper mirror that is
acceptable. **For a copier holding a position it is not**: a spin-down during an
open trade stops the ladder being managed. This must be resolved before Gate 5,
and the options are a paid always-on instance, an external pinger with the
residual gap accepted in writing, or a host that does not spin down.

To specify and then drill:

* **Monitoring:** `/healthz` already returns 503 on a stale heartbeat, which
  makes Render restart the service. Alerts on: stale heartbeat, failed
  notification, listener error, `orphaned_positions` non-empty, and any trade
  sitting in an unverified state.
* **Secrets:** already `sync: false` in the blueprint and read from the
  environment; nothing secret is committed. Confirm the destination's
  credentials follow the same rule before they exist.
* **Restart recovery:** cursor from the store, reconcile against the broker
  before the listener may act, adopt rather than resubmit. Already implemented;
  needs a drill.
* **Kill switch:** `POST /api/pause` with a reason. It stops new orders and
  leaves open trades managed — correct behaviour, but it means a pause is *not*
  a flatten. A written runbook must cover closing by hand.

**Pass:** a drill in which the process is killed mid-trade and restarted,
producing no duplicate order and a resumed ladder; plus a pause drill showing
new orders refused and the open trade still managed.
**Evidence:** the drill transcript and the ledger before and after.

### Gate 5 — Destination-account gate

This gate has two parts, and they are different in kind. The technical part is
work. The rule part is a decision, and it has been made — recorded below.

#### 5a — Technical (open)

| Item | Status |
|---|---|
| The platform, and whether it exposes an API usable by this bot | **unknown** |
| The symbol specification, account currency, leverage | **unknown** |
| Whether the destination is TradeLocker at all | **unknown** |

If the destination is MT4/MT5/cTrader rather than TradeLocker, then the entire
`src/rade/execution/` layer does not apply and a new adapter is required. In that
case `assert_demo_environment` must be *replaced by an equivalent destination
guard*, never deleted: the reason it exists is that a configuration error should
fail loudly rather than spend money.

#### 5b — The rule conflict (known, and adverse)

Blueberry Funded's published rules — *Prohibited Strategies (After March 12,
2026)*, published 8 October 2026 — list, under the strategies prohibited at every
account stage:

> **Copy Trading with Third Parties** … Copy trading with external traders, or
> having your account managed by a third party, is prohibited at all stages.
> **These activities will result in immediate account termination.**

and explicitly:

> Using copy trading software that mirrors trades from an external source into
> your Blueberry Funded account.

The consequence stated is a **Hard Breach**: immediate account closure or denial
of payout. There is no written approval on file, and nothing in this repository
changes that.

The same page also defines what *is* permitted, and the distinction is precise:

> **Non-proprietary automated trading** — Automated trading software is permitted
> only when it is proprietary. That means three things at once: you built it, it
> runs your own strategy and risk decisions, and you disclosed it to us and
> received our consent before running it.

> **Allowed:** … Utility tools that only assist with trades you decide on, such
> as position sizers, risk calculators and partial-close managers.

**That distinction is "who decides the trade".** A tool that sizes and manages a
trade the account holder decided on is permitted. A tool that decides the trade
by mirroring an external source is the prohibited activity. This is not a
loophole and it is not a technicality — it is the line the rule actually draws.

That distinction separates two categories of software:

* **assistive** — the account holder decides the trade; software sizes it,
  attaches the stop and manages the exit. The page lists "utility tools that only
  assist with trades you decide on" as allowed.
* **mirror** — software decides the trade by reading an external source and
  placing the order. The page names this as a Hard Breach.

**The account holder has chosen mirroring**, and that choice is recorded in the
decision table below. It places the intended behaviour in the prohibited
category. The expected consequence is termination of the account, not a warning.
This document records the decision; it does not argue it again, and it does not
describe the choice as compliant.

Also relevant to the design, from the same page: *"Opening a new position in the
same direction within 10 minutes of closing a losing trade is treated as part of
the same trade idea."* Our duplicate-text window is 900 s. The two interact and
should be reconciled rather than left to coincidence.

One further design constraint, stated once: the rules prohibit *Bad Faith
Trading*, defined as behaviour "designed to game the evaluation", including "any
behaviour that misrepresents your trading activity or works around the intent of
our rules". No component of this project will disguise what it is — no randomised
sizes to look manual, no altered correlation tags, no splitting to dodge pattern
detection. A pipeline that has to hide is a pipeline that has already failed, and
building concealment would convert a rule breach into something considerably
worse.

#### Decision record

| Field | Value |
|---|---|
| Date | 2026-11-14 |
| Decision | Build the mirror for the Blueberry account, knowingly accepting the risk stated in the published rules. |
| Made by | The account holder. |
| **What this decision is** | **The account holder's decision to proceed.** It is not Blueberry's approval and it is not written consent. No approval or consent exists, and none is implied by this document or by anything in this repository. |
| **Intended mode** | **Automatic mirroring.** The bot reads the channel and places the order; there is no human decision per trade. This is the category the rule names as a Hard Breach. |
| Explicitly excluded | Any design that conceals the activity or evades the firm's controls — see the Bad Faith Trading note above. |
| Expected consequence | Account termination or denied payout, per the published rule. Accepted as a known cost, not carried as an unknown risk. |
| Effect on this plan | Gate 5 is not "blocked pending approval". The adapter may be built behind a gate. Connecting the funded account still requires the destination facts in 5a to be verified first. |
| Not changed by this decision | The rule text, the stated consequence, the absence of approval or consent. |

**Pass:** a named destination guard in code that refuses anything but the
approved account and environment, and a recorded mode choice. Because the chosen
mode is mirroring, no consent requirement applies to it — consent is what the
permitted category needs, and this choice does not sit in that category.

---

## 4. Decisions I need from you

1. **The per-trade USD ceiling** that 0.03 must satisfy. It cannot be set until
   the destination's contract size, lot step and account currency are verified.
   See `docs/23` §6.
2. **The three exit mechanics** in `docs/23` §5 — the stop trigger and level
   after TP1, the rule for signals with fewer than three targets, and whether
   TP2 has any role.
3. **Correct spread and slippage limits** for the instrument. The blueprint
   values are effectively disabled.
4. **Is a spin-down-tolerant host acceptable, or must the process be always on?**
5. **If the destination platform is not TradeLocker, adapter or the broker's own
   copier?**
4. **Correct spread and slippage limits** for the instrument. The current values
   are effectively disabled.
5. **Is a spin-down-tolerant host acceptable, or must the process be always on?**
6. **If the destination platform is not TradeLocker, adapter or the broker's own
   copier?**

---

## 5. Risks and assumptions

* **The rule conflict is real and accepted, not resolved.** In the chosen mode —
  automatic mirroring — the expected outcome is termination. Nothing in the code
  prevents that, and nothing should: the account holder's decision is recorded
  above, not engineered around.
* **Latency is the poll, not a push.** 15 s on gold is real slippage, and the
  age limit of 120 s is a chosen cutoff, not a measured delivery time. Whether a
  second Telegram identity fixes push is **unknown**; nothing here depends on it.
* **A free-tier spin-down is an unmanaged position.** The largest operational
  risk in the current design, and not solved by code.
* **The sizing change is money math.** Fixed-lot mode touches the one
  calculation that decides how much is at risk; it needs its own review gate
  before it runs anywhere real.
* **The duplicate-text window is a heuristic.** It catches the channel's known
  habit of reposting one call as a second message. It is not a proof of identity.
* **The trader's stop distances are unknown.** At a fixed lot the USD risk per
  trade therefore varies, and a wide-stop signal may breach a ceiling that has
  not yet been set.
* **No claim of reliability or profitability is made here.** The only claims in
  this document are about what the code does, each tied to a file and a line.
