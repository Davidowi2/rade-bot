# Implementation report — behavior and risk spec

**Date:** 2026-11-14 (review round 2)
**Branch:** `feature/tradelocker-demo-execution`
**State:** nothing committed, nothing deployed, no account connected, no order placed.
**Authority for behaviour:** `docs/23-behavior-and-risk-spec.md`. Where this report
and that document disagree, that document is correct.

> Test counts in this report are **as of its date**. The current suite is
> **502 Python tests and 15 web tests**. The exit policy described in §A1 was
> later corrected — see `docs/23` §5 A1.

---

## 0. Review round 2 — the four findings

All four were real. None was disputed.

### 0.1 CRITICAL — a non-finite contract size bypassed the ceiling · FIXED

Reproduced before fixing:

```
check_fixed_lot(lots=0.03, spec=LotSpec(nan, 0.01, 0.01, 50.0),
                stop_distance=5.0, max_risk_usd=25.0)
  -> (0.03, '')            # accepted, risk NaN, ceiling never compared
```

Every comparison against `NaN` is false, so `nan <= 0` did not reject the spec,
`risk > ceiling` did not reject the NaN risk, and the rule returned success. The
same class of bug was worse in one direction: a **NaN `lot_step`** did not return
a wrong answer, it raised `ValueError: cannot convert float NaN to integer` from
`round()` inside `_representable`.

Fix, in `sizing.py`:

* finiteness of all four spec numbers is checked **before any arithmetic**, and
  the refusal names the field (`contract units per lot nan`);
* `_representable` treats a non-finite ratio as "not on the step" rather than
  letting `round()` raise — a finite step of `1e-320` overflows to infinity;
* the computed `risk_usd` is checked for finiteness before the comparison;
* `describe_fixed_lot` refuses a non-finite or non-positive configured lot, so
  the status page cannot report `ready` for a rule that cannot work;
* `service.py`'s `MIRROR_FIXED_LOTS` and `MIRROR_MAX_TRADE_RISK_USD` loaders now
  require finite-and-positive. `inf > 0` is true, so `MIRROR_FIXED_LOTS=inf` was
  previously accepted at startup.

Verified against eight bypass paths (NaN/±inf on each of the four spec fields),
all refused. 22 new sizing tests, 4 new config tests.

### 0.2 Spec contradicted itself on the stop level · FIXED

`docs/23` C4 said the TP1 level was CONFIRMED while A1 called the same choice an
assumption. C4 is now split: **C4** is the confirmed trigger ("move the stop once
price passes TP1"), **C4a** is the level, marked **ASSUMED, NOT CONFIRMED — see
A1**. §4 states the split at the top and marks step 3 inline. C8 and the
"partials" note no longer say "confirmed" about the level.

A separate error in A1's own prose was corrected: it said moving to breakeven
"would make reaching TP1 a no-op on the P&L but would never lock in a loss-free
exit", which contradicts itself — breakeven does exactly that.

### 0.3 The engine did not independently recheck the ceiling · FIXED

The pipeline sized the intent; `engine.execute` read `size_lots` and sent it.
New `_verify_order_size`, called from `_pre_trade_check` immediately before
submission with the quote already read for the guards:

* `check_broker_lot` on the exact size — always, in both sizing modes;
* fixed-lot mode: exact equality with the configured lot, then
  `check_fixed_lot` including the ceiling;
* risk-fraction mode: the ceiling, when one is configured.

A refusal blocks the trade, records the reason, and sends nothing. The arithmetic
is still in `sizing.py` only — this is a second *application*, not a second
implementation. The pipeline's `check_fixed_lot` and the engine's now share
`check_broker_lot` and `stop_risk_usd`.

The tests give the pipeline and the engine **different** configuration on
purpose (pipeline ceiling $50, engine $25). A matching configuration would pass
whether or not the engine checked anything, which is the trap this finding was
about.

### 0.4 Duplicate test definitions · FIXED

Four test functions in `tests/test_execution.py` were defined twice, in two
byte-identical consecutive blocks (`test_a_stop_move_leg_survives_a_restart`,
`test_a_trade_resumes_after_a_restart_and_closes_at_the_final_target`,
`test_the_plan_step_is_preserved_from_the_plan_not_the_trade_row`,
`test_manage_counts_a_stop_move_separately_from_a_close`). Python kept the later
definitions, so the earlier four never ran.

Removed the dead copy. Definitions in that file now equal collected tests: **58
= 58**. The reported number was not inflated by this — pytest never collected the
dead four — but the file said 62 tests and ran 58, which is exactly the kind of
discrepancy that makes a count untrustworthy.

### 0.5 Verification, honestly

The reviewer's run disagreed with mine and was right to. What I can now state:

| Suite | Command | Result |
|---|---|---|
| Python | `./.venv/Scripts/python.exe -m pytest tests/ -q` | **471 passed** in 98.44s |
| Python, per file | each touched file run alone | 44 / 76 / 43 / 58 / 23 / 30 — all pass standalone |
| Node | `cd web && node --test test/*.js` | **12 pass, 0 fail** |

The per-file run matters: the circular import fixed last round only appeared when
a file was run alone, so "the suite passes" is no longer treated as sufficient.

**The two Node failures you saw are real and I can explain them.** Exactly two
tests in `web/test/proxy.test.js` bind a loopback server and make a real HTTP
request to it (`startServer` at line 28): *"proxies with the backend token and
never returns it"* and *"a write forwards only the rebuilt body to the fixed
path"*. In a sandbox without loopback networking they fail; here they pass. The
other ten make no socket calls. That is an environment difference, not a code
difference — but it means those two assertions are unverified in your
environment, and I am not going to call them green on your behalf.

**Your `WinError 10013` is the same story, and it is worth naming.** 23 service
tests bind a real socket on an ephemeral loopback port. Windows refuses
`bind()` on 127.0.0.1 with `WSAEACCES` when a firewall policy or a security
product blocks it — a permission decision, not a code defect. So in your
environment the service suite cannot run, and my 468 is not independently
confirmed by you. That is a real limit on this report, not a formality.

---

## 0.6 Review round 3 — the missing-lot fallback · FIXED

You are right, and this was the same defect as the last round wearing a
different coat.

If `MIRROR_FIXED_LOTS` was unset, `_fixed_lots()` returned `None` and the engine
fell through to **risk-fraction sizing**: each order sized from equity. That is
the exact behaviour "exactly 0.03 lots or refuse" forbids. The blueprint sets
0.03, but a blueprint value is an intention — the code path was still there.

The fix is structural rather than a check:

* `fixed_lots` and `max_trade_risk_usd` are now **required** constructor
  arguments with **no defaults**. There is no `None`, and therefore no fallback
  to remove later. `_require_size` rejects `None`, non-numbers, `NaN`, `inf`,
  zero and negatives at construction.
* The risk-fraction branch is **deleted** from `_verify_order_size`. The engine
  has one sizing mode, and it is the confirmed one.
* `start_executor_if_configured` refuses to start the executor when either
  variable is unset, names which one, and logs it.

**A consequence you should know before deploying.** With the ceiling still empty,
the executor now **does not start** rather than starting and refusing every
signal. Same fail-closed outcome, earlier and louder. The dashboard shows
`executor: not started` with the reason instead of a healthy-looking copier that
never trades. `render.yaml` says so next to both variables.

The pipeline keeps risk-fraction sizing, because the paper mirror observes and
does not place orders. That is the one place the mode is still legitimate, and it
is now the only place.

## 0.7 The stale test count · FIXED

`docs/23` §8 said **417** while `docs/24` said 454. Both were true when written
and neither is now. §8 says **468**, measured with the command printed beside it
in `docs/24` §0.5. The general lesson — a number in prose rots — is why the
report quotes the command next to every count, so a stale figure is obviously
stale rather than quietly wrong.

---

## 1. What was built

Two confirmed behaviours, implemented in the TradeLocker **demo** path only.

**The fixed-lot rule.** `src/rade/execution/sizing.py` — new. `check_broker_lot`
answers the broker question (is this lot tradeable here); `check_fixed_lot` adds
the ceiling (may this trade lose this much). Twelve refusal conditions, each
naming the number that failed. It never rounds, clamps, substitutes or skips.
`describe_fixed_lot` reports `ready` / `problem` for `/api/status` and the
startup log.

**Hold to final.** `ladder.py` builds an exit plan whose first leg *moves the
stop* and closes nothing, whose second target does not exist as a leg at all,
and whose final target is attached to the order at submission so the broker
closes the position even if monitoring dies. Legs carry an explicit `action`
(`close` / `stop_move` / `broker_target`), because a stop-move leg has
`quantity == 0` and would otherwise be read as "close the remainder".

The old thirds ladder still exists and is tested, but it is opt-in via
`exit_policy="ladder"`. The default is `hold_to_final`.

---

## 2. Files changed

| File | What |
|---|---|
| `src/rade/execution/sizing.py` | new — `check_broker_lot`, `check_fixed_lot`, `stop_risk_usd`, `describe_fixed_lot` |
| `src/rade/execution/ladder.py` | hold-to-final plan, leg actions, old policy retained |
| `src/rade/execution/engine.py` | `exit_policy`, `fixed_lots`, `max_trade_risk_usd`, `_verify_order_size`, stop-move branch, plan round-trip |
| `src/rade/execution/__init__.py` | re-exports; engine resolved lazily (PEP 562) |
| `src/rade/observer/mirror_pipeline.py` | fixed-lot sizing mode, refusal path, final-target rule |
| `src/rade/observer/service.py` | config loaders (finite-positive), engine wiring, status reporting |
| `render.yaml` | `MIRROR_FIXED_LOTS`, `MIRROR_MAX_TRADE_RISK_USD`; blueprint-vs-deployed header |
| `web/index.html` | position-size card: the lot and ceiling, or `NOT READY: <problem>` |
| `tests/test_sizing.py` | new — 44 tests |
| `tests/test_execution.py` | 76 tests; duplicates removed; engine size-check and constructor refusals |
| `tests/test_mirror_pipeline.py` | 43 tests; fixed-lot pipeline refusals |
| `tests/test_signal_age.py`, `tests/test_guards.py` | 23 + 30; engine constructions updated for the required rule |
| `tests/test_mirror_service.py` | 58 tests; config, sizing and execution status, executor startup refusal |
| `docs/22-architecture-and-gates.md` | new — audit and gate definitions |
| `docs/23-behavior-and-risk-spec.md` | new — the single authority for behaviour |

Baseline before this work: 376 Python tests. Now 471.

---

## 3. What this evidence is, and is not

**Is:** deterministic test doubles — a fake broker, scripted quotes, an in-memory
ledger — plus the money math asserted numerically. A 0.03 lot on gold with a
10.00 stop is $30.00 of risk and the tests say so.

**Is not:** a simulation against market data, or evidence that the strategy makes
money. Nothing here observes a real quote or a real fill.

---

## 4. Defects found, and who found them

1. A refused stop move was recorded as applied (`_stop_management` returns
   `{"stopped": True}` with no `error` key). Mine, caught by an existing test.
2. `ManagedTrade.from_row` read the lot step from the trade row, so every restart
   rebuilt the plan at `0.01`. Pre-existing.
3. A circular import that a passing suite could not see. Mine; found by running
   the pipeline tests alone. Fixed with a lazy engine import and a subprocess test.
4. **The NaN bypass.** Mine, found by review. The rule's headline property — fails
   closed — was false for numbers that were not numbers.
5. **The engine trusting `size_lots`.** Mine, found by review.
6. **Duplicated test definitions.** Mine, found by review.

Items 4–6 have one thing in common: each was invisible to the evidence I
presented last round. A suite that passes is not the same as a system that holds.

---

## 5. What is ready

* The fixed-lot rule, with the finiteness gate, as a library with 44 tests.
* The hold-to-final exit policy, default in the engine, exercised through
  submission, management, restart and reconciliation.
* The pipeline refuses rather than sizes when the rule cannot be met.
* The engine re-verifies the lot and the ceiling against the broker's spec before
  every order.
* The engine **cannot be built** without a fixed lot and a ceiling, so the
  risk-fraction fallback no longer exists on the execution path at all.
* `/api/status` and the dashboard show whether sizing is ready, and name the
  problem when it is not.
* Startup refuses nonsense in `MIRROR_FIXED_LOTS` and `MIRROR_MAX_TRADE_RISK_USD`.

All of it against the TradeLocker demo. `assert_demo_environment` is unchanged
and still refuses every host but `demo.tradelocker.com`.

---

## 6. Blockers

### Blocked on your decision

| # | Question | Why it blocks |
|---|---|---|
| **A1** | Does "move the stop there" mean **TP1**, or breakeven? | Implemented as TP1, and now marked as an assumption rather than a confirmation. TP1 gives back the distance from entry to TP1 on every reversal; breakeven gives back nothing. |
| **S2** | The per-trade USD ceiling | Without it, `0.03` is a size, not a risk limit. The executor now refuses to **start** without it, so nothing runs at all until it is set. |
| **S3** | Spread and slippage limits for gold | Current values are effectively disabled. |
| **A2, A3** | Touch-based TP1 trigger; silent TP2 | Taken as assumptions, flagged in `docs/23` §5. |

### Blocked on facts I do not have

* **The destination platform is unknown.** No Blueberry adapter, and zero
  references to the platform in the repository. Needed: MT4/MT5/cTrader/other,
  whether an API exists, gold contract size and lot step, account currency, and
  the firm's daily-loss and maximum-lot rules.
* ~~**No quote provider.** `MIRROR_QUOTE_PROVIDER=none`, so every market signal
  is blocked before it reaches sizing.~~ **Resolved 2026-11-15** — the provider
  is set; see `docs/28`.
* **Live push is still unproven.** All measured deliveries came from the 15-second
  poll.

---

## 7. The simulation question

Unchanged by this round, and still the largest gap.

`src/rade/replay/` runs an exit policy over a bar series and reports R per setup.
It cannot model the policy now in the engine:

| Replay policy | Behaviour |
|---|---|
| `A` | close everything at the final target — **no stop move at all** |
| `C` | `A` plus the stop moves to TP1 **after TP2** |
| `breakeven_after_tp1` | a separate flag moving the stop to **breakeven** after TP1 |

The implemented policy is *hold to final with the stop moved to TP1 after TP1* —
none of the above. Adding it is one constant, one branch and one assumption flag,
and running it against the archived cohort would produce the R distribution for
**TP1 stop vs breakeven stop**, settling A1 with data instead of argument.

Not done, because `docs/14-redirect-plan.md` §3–§4 is a frozen spec with
acceptance criteria AC-1..AC-16 and published outputs. Still waiting on your word.
