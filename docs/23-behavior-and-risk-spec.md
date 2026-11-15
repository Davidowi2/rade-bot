# 23 — Behavior and risk spec

Status: **the confirmed behaviour is implemented against the TradeLocker demo
broker.** The destination adapter does not exist and no account has been
connected. No order has been placed outside the demo account.

This is the single authority for what the bot is supposed to do. `docs/22` holds
the current-state audit and the gate definitions; where the two disagree about
behaviour, this document wins.

Marking convention:

* **CONFIRMED** — stated by the account holder.
* **ASSUMPTION** — chosen by the implementer because the confirmation did not
  cover it. Flagged for review; not to be treated as agreed.
* **OPEN** — a decision is still needed.
* **UNKNOWN** — a fact that must be measured or verified, not assumed.
* **IMPLEMENTED** / **NOT IMPLEMENTED** — whether the code does it today.

---

## 1. Scope

**In scope**

* Telegram signals of type **NOW / MARKET** only, for XAUUSD.
* Exactly **0.03 lots** per copied signal, or refusal.
* Exit: hold through TP1, move the stop when price passes TP1, close the whole
  position at the final target.
* A destination adapter for the Blueberry account, **not built** — see §9.

**Out of scope**

* Pending **LIMIT** signals. Parsed, recorded and notified; never sent.
* Risk-fraction sizing as a fallback. It remains available as a mode, but it is
  not the confirmed behaviour and is not what a fixed lot falls back to.
* Multiple instruments.
* Any concealment of the activity, or any design intended to evade the firm's
  controls. See §3.

---

## 2. Confirmed requirements

| ID | Requirement | Status | Where |
|---|---|---|---|
| **C1** | Copy NOW/market signals only. LIMIT signals are recorded and never sent. | CONFIRMED · IMPLEMENTED | `engine.py` (`execute` returns early when `entry_type != "market"`) |
| **C2** | Use exactly 0.03 lots, or refuse. No resizing, rounding to another size, clamping, or silent skip. | CONFIRMED · IMPLEMENTED | `sizing.py` (`check_fixed_lot`), `mirror_pipeline.py` |
| **C3** | Hold through TP1. Do not close any quantity at TP1 or TP2. | CONFIRMED · IMPLEMENTED | `ladder.py` (`_hold_to_final_plan`), `engine.py` (`ACTION_STOP_MOVE` branch) |
| **C4** | Move the stop once price passes TP1. | CONFIRMED · IMPLEMENTED | `ladder.py` (`ACTION_STOP_MOVE` leg, triggered at the first target) |
| **C4a** | *Where* the stop moves to — **TP1**. | **CONFIRMED** | `ladder.py` (`STOP_TARGET` = first target) |
| **C5** | Close the remaining position at the final target (TP3 when present). | CONFIRMED · IMPLEMENTED | final leg `ACTION_BROKER_TARGET`; the broker's attached TP closes it |
| **C6** | Refuse rather than adjust when the size is invalid. | CONFIRMED · IMPLEMENTED | ten refusal conditions in `check_fixed_lot` |
| **C7** | No concealment, no evasion of the firm's controls. | CONFIRMED · HOLDS | nothing in the codebase disguises the activity |
| **C8** | Two targets: same rule at TP1, close at TP2. One target: close whole, no invented stop trigger. | CONFIRMED · IMPLEMENTED (stop level: A1) | `_hold_to_final_plan` |

### What the code now does, in one paragraph

A signal is parsed, aged, quoted and sized. If the fixed lot is valid for this
broker and inside the ceiling, an order is placed with the initial stop and the
**final** target attached at the broker, so the position is protected from the
moment it exists and the exit survives a monitoring outage. Nothing is closed at
TP1. When price reaches TP1 the stop is moved to TP1 and one notification is
sent. When price reaches the final target the broker's attached take profit
closes the whole position, and the manager records it. TP2 is not an exit and
produces no event.

---

## 3. Destination decision — recorded, not approved

The account holder has decided to build the mirror for the Blueberry account,
knowingly accepting the risk stated in the firm's published rules.

**This is the account holder's decision to proceed. It is not Blueberry's
approval, and it is not written consent.** No approval or consent exists. Nothing
in this document, `docs/22`, or the repository should be read as implying one.

**Intended mode: automatic mirroring.** The bot reads the channel and places the
order; there is no human decision per trade.

The published rules name that activity directly:

> Using copy trading software that mirrors trades from an external source into
> your Blueberry Funded account.

and place it among the strategies where "Engaging in any of them is a **Hard
Breach** and will result in immediate account closure or denial of payout."

The expected consequence is termination of the account, not a warning. It is
recorded as a known and accepted cost.

The same rules prohibit *Bad Faith Trading*, defined as behaviour "designed to
game the evaluation", including "any behaviour that misrepresents your trading
activity or works around the intent of our rules". **No component of this project
conceals what it is** — no randomised sizes, no altered correlation tags, no
splitting to dodge pattern detection, no obfuscation of the strategy identifier.
This is a hard constraint on the design.

---

## 4. Exit policy

The trigger is confirmed; the level the stop moves **to** is assumed (A1). The
distinction is kept here rather than smoothed over, because the two halves have
different standing.

For a signal with two or more targets:

1. Enter at market. The initial stop and the **final** target are attached to the
   order at the broker.
2. Hold the full position through TP1. **Nothing is closed at TP1.**
3. When price passes TP1, move the stop **to TP1**. **Nothing is closed.**
4. Hold through TP2. **Nothing is closed, and no event is emitted.**
5. The whole position closes at the final target, via the broker's attached take
   profit.

For a single target: close the whole position there. There is no earlier target
to pass, so no earlier stop move is invented.

### The consequence, stated plainly

Reaching TP1 earns **nothing**. There is no partial at TP1, so the only P&L
outcomes are the stop (initial or moved) or the final target. Reaching TP1
removes risk; it does not bank profit.

Whether that is desirable depends on how often the final target is reached. If it
usually is, this captures more than a three-way ladder. If TP1 is often reached
and the final target often missed, this returns breakeven where a ladder returns
a third of the move. **This is a property of the implemented policy, not an
objection to it.**

### One benefit

The earlier policy split 0.03 into three closes of 0.01, which worked only while
the broker's `min_lot` was 0.01. The implemented policy removes every partial, so
that fragmentation problem no longer exists.

---

## 5. Assumptions — flagged for review

These are the points where the confirmation did not decide, and the implementer
did. **They are not agreed, and they should be confirmed or overruled.**

### A1 — RESOLVED: the stop moves to **TP1**, not to breakeven

The instruction is "move the stop there after price passes TP1" — *there* is
TP1. That is the implemented policy.

An earlier revision of this section recorded breakeven as the decision, on the
strength of `scripts/compare_exit_policies.py`. **That measurement was wrong.**
The script signed the stop-loss branches as `entry - stop`, so a long that was
stopped out was booked as a **+1R win**. The error inverted the ranking. It was
found by an external review on 2026-11-14 and fixed; the script now has tests
that pin a stopped-out long to −1R (`tests/test_exit_policy_comparison.py`).

Corrected measurement over the 1,972-setup archived cohort (30-minute bars,
pessimistic tie-breaking):

| Policy after TP1 | Mean R | Win rate | Total R | Exits |
|---|---|---|---|---|
| **Stop to TP1** | **+0.160** | **58.0%** | **+315** | 834 initial-SL, 992 stopped at TP1, 141 final TP |
| Stop to breakeven | −0.021 | 16.2% | −42 | 834 initial-SL, 823 breakeven, 291 final TP |

TP1 wins clearly. Breakeven gives up 992 small locked profits for 291 shots at
the final target, and the trade is worse for it.

**Caveat:** a 30-minute-bar model using the published stop and entry, not real
fills. Re-run on 1-minute data before treating it as settled.

### A2 — "Price passes TP1" means the exit-side quote touches it

The code advances when the price a close would actually get reaches the level:
the bid for a long, the ask for a short (`target_reached`, `ladder.py`). At a
15 s delivery interval a brief touch can be missed entirely, and a touch that
immediately reverses still counts.

**Alternative:** require a close beyond the level on a 1-minute bar. That needs a
bar feed the service does not have, so it is not a small change.

### A3 — TP2 produces no event at all

The instruction said not to partially close at TP2, but did not say whether
passing it should be recorded. Implemented as **nothing**: no order, no
notification, no ledger row. The minimal reading of the instruction.

**Alternative:** a notification-only marker. One line, and it makes an open trade
legible on the dashboard between entry and exit.

### A4 — A fill already past the final target stops management

If the fill is at or beyond the final target, there is no move left to capture,
so the trade is flagged for review and left protected by the broker's stop and
target. A fill past **TP1** but before the final target is accepted, and the stop
move fires on the first observation. This follows from C3 rather than from an
explicit instruction.

### A5 — Defaults taken on the minor items

| Item | Taken | Note |
|---|---|---|
| A refused stop move | Retried on the next pass, bounded by `max_stop_attempts`, then a human is asked | The position stays protected by the stop already attached throughout |
| A price gap straight past TP1 to the final target | The final target closes it; the stop move is not attempted | Avoids a modification racing the broker's own exit |
| Spread and slippage limits | Left at the blueprint's `5` price units | Far too wide for gold, and deliberately **not** guessed at — see S3 |

---

## 6. Sizing and risk

### S1 — The fixed-lot rule · IMPLEMENTED

The rule is: the configured lot is valid for this signal, or the signal is
refused. There is no third outcome. It refuses, naming the condition, when:

1. the broker's instrument specification is not verified;
2. **any of the four specification numbers is not finite** (`NaN`/`inf`);
3. the specification is incomplete (units, step, min or max unusable);
4. the configured lot is not a usable number;
5. **no per-trade USD ceiling is configured**;
6. the ceiling is not a positive number;
7. the lot is not representable at the broker's lot step;
8. the lot is below the broker's minimum;
9. the lot is above the broker's maximum;
10. the stop distance is not usable;
11. the resulting USD risk is not a finite dollar amount;
12. the resulting USD risk exceeds the ceiling.

It never rounds to a nearby size, never clamps, never substitutes, never skips
silently. The fixed-lot mode also refuses to accept any signal without a ceiling
— a missing ceiling is a configuration error, not a reason to fall back to
risk-fraction sizing.

**Why condition 2 exists.** Every comparison against `NaN` is false, so
`nan <= 0` does not reject a NaN contract size and `risk > ceiling` does not
reject the NaN risk it produces. The first version of this rule therefore
accepted `0.03` lots against a NaN contract size and a $25 ceiling, and returned
success. Finiteness is now checked **before** any arithmetic, and the refusal
names the field that was not finite. `describe_fixed_lot` applies the same test
to the configured lot and the ceiling, so a rule that cannot work never reports
`ready`.

### S1b — The engine rechecks the order it is about to send · IMPLEMENTED

The pipeline sizes the intent; the engine is what calls the broker. It therefore
does not take `size_lots` on trust. Immediately before submission, with the
quote it already read for the guards, it re-applies the same functions from
`sizing.py`:

* `check_broker_lot` on the exact size — so a size the broker cannot trade is
  refused, and a non-finite contract size cannot reach the broker;
* exact equality with the configured lot;
* `check_fixed_lot`, including the ceiling.

A refusal blocks the trade and records the reason; nothing is sent. This is a
second application of one implementation, not a second implementation: the
arithmetic still lives in `sizing.py` only.

### S1c — No fixed lot, no engine · IMPLEMENTED

`fixed_lots` and `max_trade_risk_usd` are **required** constructor arguments with
no defaults, validated finite and positive. An engine that cannot state its size
and its ceiling cannot be built.

They used to be optional, with `None` meaning "fall back to risk-fraction
sizing". That was the confirmed rule's opposite: an executor started without
`MIRROR_FIXED_LOTS` would have sized each order from equity instead of refusing.
The fallback is gone from the execution path.

`start_executor_if_configured` refuses to start the executor when either variable
is unset, and says which one, rather than starting a copier that either sizes
wrongly or refuses every signal while reporting itself healthy. `/api/status`
reports `execution_sizing_ready` and marks the sizing rule as blocking execution,
so the dashboard cannot describe a "risk fraction" mode that the executor
refuses to run in. The pipeline
keeps risk-fraction sizing for the paper mirror, which observes and does not
place orders.

### S2 — The maximum dollar risk for a 0.03-lot trade · **OPEN**

**This number does not exist yet, and nothing in the code invents one.** Until it
is set, every signal is refused with a named reason. That is the intended
fail-closed behaviour.

The arithmetic, using the standard 100 oz per lot for gold — **the contract size
must be verified, not assumed**:

```
0.03 lots  = 3 oz
risk_usd   = stop_distance × 3        ($1.00 of gold stop = $3.00 of risk)

stop $5   → $15
stop $8   → $24
stop $10  → $30
stop $15  → $45
stop $20  → $60
```

Options for the ceiling:

| Option | Meaning | Consequence |
|---|---|---|
| **S2a** | A fixed dollar amount, e.g. $25 | Simplest to reason about and test. Does not adapt if the account or instrument changes. At $25, any signal with a stop wider than $8.33 on gold is refused — which may be most of them. |
| **S2b** | A percentage of account equity, e.g. 0.25% | Adapts automatically. Moves the ceiling as equity moves, so "the same signal" behaves differently on different days. |
| **S2c** | Derived from the firm's daily loss limit: `daily_limit × safety / expected_max_consecutive_losses` | Ties the per-trade risk to the rule that actually ends the account. Requires the firm's daily limit as a verified number. |

**RECOMMENDED:** S2c as the primary derivation with S2a as a hard cap — take the
lower of the two.

**Two caveats that apply to any choice:**

1. The ceiling is a **pre-trade estimate of stop risk**, not a maximum loss. A gap
   or slippage can exceed it. At a 15 s delivery interval on gold, slippage is
   real and is not bounded by the stop.
2. It depends on the account currency. A non-USD account needs conversion, and
   the rate at the time of the trade is a further variable.

**Required before this can be set:** `contract_units_per_lot`, `lot_step`,
`min_lot`, `max_lot`, account currency, account equity, and the firm's daily and
maximum loss limits. All **UNKNOWN**.

### S3 — Spread and slippage limits · **OPEN**

Both are in **price units** and both are `5` in the blueprint. Gold's normal
spread is about 0.2, so the limit accepts a book roughly 25× wider than normal.
It is not a filter.

The value has been left as it is rather than narrowed to a guess. A number
invented here would look like a verified one, and this is the guard that decides
whether a bad fill is taken.

### S4 — The daily-loss breaker · unchanged

It exists and is expressed as a fraction of the day's opening equity. With a
fixed lot the per-trade risk is constant, so the number of losses a day the
breaker tolerates is `daily_limit / per_trade_risk`. Whether that is acceptable
is a risk decision that follows from S2.

---

## 7. Code against the spec — status

| # | Spec | Before | Now | Where |
|---|---|---|---|---|
| **M1** | C2 — exactly 0.03 or refuse | risk-fraction sizing only | **closed** — fixed-lot mode with ten refusal conditions | `sizing.py` |
| **M2** | C6 — never clamp | `lots = max_lot` silently reduced the size | **closed** — above-maximum is a refusal | `sizing.py` |
| **M3** | C2/C6 — named refusal | below-minimum returned a *skip* | **closed** — below-minimum is a named refusal in fixed-lot mode | `sizing.py` |
| **M4** | C3 — hold through TP1 | TP1 closed one third | **closed** — the TP1 leg closes nothing | `ladder.py`, `engine.py` |
| **M5** | C3 — no exit at TP2 | TP2 closed another third | **closed** — TP2 has no leg | `ladder.py` |
| **M6** | C4 — stop move without a close | no such leg existed | **closed** — explicit `ACTION_STOP_MOVE` leg | `ladder.py`, `engine.py` |
| **M7** | C8 — one target | single unmanaged leg, no stop move | **closed, and confirmed correct** | `ladder.py` |
| **M8** | C8 — two targets | closed half at TP1 | **closed** — stop at TP1, close at TP2 | `ladder.py` |
| **M9** | A4 — fill beyond TP1 | refused beyond *any* target | **closed** — refused only beyond the final target | `mirror_pipeline.py`, `engine.py` |
| **M10** | C5 — final closure | already correct, fill inferred | **unchanged** | `ladder.py` |
| **M11** | Destination adapter | none exists | **still open** — see §9 | — |
| **M12** | Destination guard | `assert_demo_environment` refuses any non-demo host | **unchanged and must stay** — the destination adapter needs its own guard | `gateway.py` |
| **M13** | Duplicate suppression | 900 s text window | **unchanged** — still to be reconciled with the firm's 10-minute same-idea rule | `render.yaml` |
| **M14** | Spread/slippage | 5 price units | **unchanged** — labelled unverified, see S3 | `render.yaml` |
| **M15** | S1 cond. 2 — non-finite numbers | a NaN contract size passed the ceiling check | **closed** — finiteness checked before arithmetic, refusal names the field | `sizing.py` |
| **M16** | S1b — engine-side recheck | the engine trusted `size_lots` | **closed** — lot, exact-size and ceiling checks before submission | `engine.py` (`_verify_order_size`) |
| **M17** | Config loaders | `inf` passed `value > 0` and was accepted | **closed** — finite-and-positive required for the lot and the ceiling | `service.py` |
| **M18** | S1c — the missing-lot fallback | an engine built without `MIRROR_FIXED_LOTS` sized from equity | **closed** — both numbers required by the engine; the executor refuses to start without them | `engine.py`, `service.py` |

### One bug found while implementing this

`_apply_stop` returns `{"stopped": True}` from `_stop_management` without an
`error` key. The first version of the new stop-move branch only checked `error`,
so a stop move that was **refused for widening the risk** was recorded as applied
and the step advanced. Caught by
`test_a_stop_that_would_widen_the_risk_stops_management`; the branch now returns
on either key.

A second, older bug was fixed on the way: `ManagedTrade.from_row` read
`lot_step` and `min_lot` from the trade row instead of the plan, so every restart
rebuilt the plan with `0.01` regardless of the broker. It now reads them from
where they were written.

### A third bug, found after the suite had already gone green

`mirror_pipeline.py` now imports `rade.execution.sizing`. Importing a submodule
executes the package `__init__`, which imported the engine eagerly; the engine
imports `rade.observer.mirror_pipeline` back for the pipeline's result types.
So when `mirror_pipeline` was the **first** module a process imported, it
failed with `ImportError: cannot import name 'STATUS_MANUAL_REVIEW' from
partially initialized module`.

The full suite did not catch it, because an earlier test module had already
imported `rade.execution` and left the package initialised — a passing suite
that depended on file order. It surfaced only when the pipeline tests were run
as a pair on their own. The engine re-exports are now resolved on first use
(PEP 562) instead of at package import, so `mirror_pipeline` can be imported
first. `test_the_pipeline_can_be_the_first_module_imported` runs the import in
a fresh interpreter, since no in-process test can observe the ordering.

Worth stating plainly: this was a real defect that my earlier "417 passing"
report did not reflect. The number was true and the conclusion was wrong.

---

## 8. Staged path — status

| Stage | What it needs | Status |
|---|---|---|
| **A — Spec freeze** | every item confirmed, no OPEN remaining | **not frozen** — A1-A5 need review, S2 and S3 are open |
| **B — Sizing and exit change** | the mismatch table closed; tests per refusal | **code complete, 484 tests passing**; the refusal cases are covered at unit, pipeline and engine level |
| **C — Shadow on the real channel** | real quotes, no orders, five trading days | **not started** — blocked on a quote provider; `MIRROR_QUOTE_PROVIDER` is `none` |
| **D — Demo execution matrix** | the full case list on the private channel | **not run against the new policy** — the code path is tested, the live demo run is not |
| **E — Deployment readiness** | kill/restart drill, pause drill, alerts | **not started** |
| **F — Blueberry adapter** | the destination platform and its specification | **blocked** — see §9 |
| **G — Funded connection** | the recorded decision plus verified account facts | **not permitted yet** |

The distinction that matters: stage B is *code*, and code is what has been
verified here. Stages C-F are *evidence against a real feed and a real broker*,
and none of it exists.

---

## 9. What has and has not been validated

The destination adapter exists and has been used **read-only** against the
Blueberry account: the instrument specification, account id, balance and
positions were read on 2026-11-14. **No order has ever been placed.** The
account has been connected to, never traded.

What has **not** been demonstrated:

* that a signal arrives by live push (all measured deliveries came from the 15 s
  poll);
* that the confirmed exit policy behaves as intended against a real feed;
* that a 0.03 lot is even representable on the destination;
* that any of this is profitable. **No claim of profitability is made.**

The only claims in this document are about what the code does, each tied to a
file, and about what the account holder has decided.

---

## 10. What I need from you

1. ~~**A1 — confirm the stop moves to TP1, not to breakeven.**~~ **RESOLVED:
   the stop moves to TP1.** See §5 A1 for the corrected measurement.
2. **S2 — the per-trade USD ceiling.** Cannot be set until the destination's
   contract size, lot step and account currency are verified, plus the firm's
   daily loss limit.
3. **S3 — the correct spread and slippage limits for gold.** The current values
   are effectively disabled.
4. **A2, A3 — accept or overrule** the touch-based trigger and the silent TP2.

Two facts worth measuring before stage C, because they change the answers:
how often this trader publishes fewer than three targets, and the actual
distribution of his stop distances — a ceiling that refuses most of his signals
is a ceiling that has silently disabled the bot.
