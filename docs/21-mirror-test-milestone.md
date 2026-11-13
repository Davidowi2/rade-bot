# 21 — The pre-deployment test milestone

This is the record of the milestone that had to pass before the mirror pipeline
touches the trader's channel: a private fake-signal channel, a signal-age guard,
both execution modes exercised, and every failure case driven through the real
code with its ledger status recorded.

It is written in the order the work was done, because the order was the point.
Two things were found while doing it — an edited signal that would have been
copied, and a notification kind that swallowed the second and third legs — and
both are described where they were found rather than tidied into a summary.

| | |
|---|---|
| date | 2026-11-13 |
| environment | TradeLocker **demo** only; the adapter refuses any other |
| test channel | `@rade_mirror_test_20261113` (private, fake signals only) |
| trader channel | untouched, and no process was listening to it |
| account at the end | 0 positions, 0 open orders |

## 1. Before anything else: was something already armed?

A local listener had been left running against the **real trader channel** with
execution enabled. A fake signal posted into that channel during setup would have
been copied to the demo account, and the resulting order would have been
indistinguishable from a real one in the ledger. So this was checked first, and
the check is recorded here rather than assumed:

| check | result |
|---|---|
| listener on port 8099 (the armed one) | connection refused — stopped |
| open positions on the demo account | 0 |
| open orders on the demo account | 0 |
| orders created that day | 47, all in terminal states (`not in a terminal state: 0`) |
| equity | 101,034.17 |

Only then was a test channel created.

## 2. The private test channel

`Rade mirror TEST channel - fake signals only`, username
`@rade_mirror_test_20261113`.

Two properties matter more than the name:

* **It is not the trader's channel.** `scripts/test_channel.py` refuses to touch
  whatever `TELEGRAM_CHANNEL` points at, and refuses any chat that is not the
  configured test channel. A test message in the real channel cannot be
  distinguished from a real call by anything reading it, so the tool has no way
  to post one.
* **It has a username.** A private channel can only be resolved by bare id when
  the session already has it cached; a cold session — which is what a fresh
  deployment is — fails with `Cannot find any entity corresponding to "-100…"`.
  Referencing it by username is what makes the test channel reachable after a
  restart, so that is how both environment files and the posting tool refer to
  it.

Posting uses a second session file (`data/post_session.session`). The listener's
session file is single-writer: while the listener holds it, posting through it
fails with `database is locked`. The copy is deliberate, not a workaround for a
bug — one process owns the listener session.

## 3. The signal-age guard

Documented in [20 §12](20-tradelocker-execution.md#12-signal-age); the summary
here is only what the milestone needed.

`MIRROR_MAX_SIGNAL_AGE_S` (default `120`) refuses a NOW signal whose age at
intake exceeds the limit. It is separate from `MIRROR_QUOTE_MAX_AGE_S` ("is the
price current") and from `MIRROR_MAX_SLIPPAGE` ("has the market run away"),
because neither of those answers "is the instruction current" — a signal
recovered by polling, catch-up or restart arrives with a perfectly fresh quote.

Four details that the tests pin down:

* **Age is measured to `received_at`, not to the wall clock.** `received_at` is
  stamped when the envelope is built, so the difference *is* the lateness, and a
  replay of a stored envelope is judged by the `now=` it is given. Measuring
  against `datetime.now()` would make every stored envelope look fresh at the
  moment it is replayed.
* **A missing or implausible timestamp is refused**, not treated as zero age. A
  timestamp more than 120 s in the future is refused too, while a small clock
  disagreement is tolerated.
* **`stale` is its own disposition**, not folded into `blocked`. "The intake was
  late" is a different fact from "a guard said no", it is counted separately,
  and it notifies separately.
* **`0` disables the limit but not the timestamp checks.** A deployment that
  switches the limit off still cannot trade a message with no date.

The service warns at startup when `MIRROR_RECONNECT_S > MIRROR_MAX_SIGNAL_AGE_S`,
because a reconnect interval longer than the age limit means every signal
recovered by catch-up is stale by construction — a deployment in that
configuration copies nothing, and should say so rather than appear healthy.

## 4. Mode A — execution disabled

`MIRROR_EXECUTE=0`, so the service never constructs the execution engine at all.
The only broker interaction is a read-only quote.

| message | what it was | ledger status | broker |
|---|---|---|---|
| 3 | valid signal, but the demo feed published a crossed book | `blocked` | no order |
| 4 | valid signal | `paper_intent` (0.52 lot) | no order |
| 5 | deliberately malformed | `manual_review` | no order |
| 6 | valid signal | `paper_intent` (0.57 lot) | no order |

Notifications: `[PAPER INTENT]` and `[MANUAL REVIEW]`, one each, `attempts: 1`.
The account was unchanged: 0 positions, 0 orders.

The crossed book on message 3 is worth keeping in the record. It is not a bug in
the pipeline: the demo feed briefly published `ask < bid`, the intake refused it,
and the refusal now names the values (`bid=…, ask=…`) because "invalid quote"
with no numbers cannot be diagnosed after the fact.

## 5. Mode B — execution enabled, demo only

`MIRROR_EXECUTE=1`, `MIRROR_RISK_FRACTION=0.0003` — the smallest size that can
still be split three ways at the broker's 0.01 lot step, and a fraction that can
only *lower* the 0.5% ceiling, never raise it. One signal was posted, at the live
price, and copied exactly once.

```
XAUUSD SELL NOW 3885.18   Sl 3894.18   Tp 3884.18 / 3883.18 / 3882.18
```

| time | stage | status | detail |
|---|---|---|---|
| 17:10:38 | validated | `validated` | ladder: close 0.01 at 3884.18, then stop to breakeven; close 0.01 at 3883.18, then stop to 3884.18 |
| 17:10:43 | risk | `guard_passed` | all pre-trade checks passed |
| 17:10:46 | submit | `submitted` | market 0.03 lot sell with SL 3894.18 and TP 3882.18 |
| 17:10:47 | accept | `accepted` | the broker accepted order 216172782144422063 |
| 17:10:52 | fill | `open` | position 216172782118683552 open at 3885.51 |
| 17:14:02 | tp1 | `partial_closed` | closed 0.01 lots at 3884.18 |
| 17:14:07 | tp1 | `stop_updated` | the stop moved to 3885.51 |
| 17:15:35 | tp2 | `partial_closed` | closed 0.01 lots at 3883.18 |
| 17:15:40 | tp2 | `stop_updated` | the stop moved to 3884.18 |
| 17:16:09 | close | `closed` | the position closed at the broker after 2 ladder steps; the remainder was taken by the stop or target attached at the broker |

One order, one position, protected from the moment it existed (stop 3894.18 and
target 3882.18 attached at submission, both confirmed in the open-order feed),
managed through two partial closes with a verified stop move after each, then
closed. The account was 0 positions / 0 open orders afterwards.

Two things about this run are worth stating plainly, because the table alone
looks cleaner than the run was:

* **The ladder ran correctly; the notifications did not.** Both legs wrote the
  bare kind `"partial"`, and the notification ledger dedupes on
  `(event_key, kind)`, so TP2's close and TP2's stop move were recorded as
  duplicates and never sent. See §9.
* **A later run of the same ladder exposed a worse defect.** The second Mode B
  attempt lost its position to a `429` on `/positions`, which the manager read as
  a close. That is §8, and it is the reason this milestone was worth doing.


## 6. Failure and polling cases

Every case below was driven through the same functions the live listener calls,
and the status was read back **from the ledger**, not from the return value: a
refusal that is decided but not persisted is not a refusal. Two separate readers
are used because intake refusals are stored in `mirror_events` — a record of a
message — while executed trades are stored in `mirror_trades`. Reading the wrong
one finds nothing, which is correct and initially produced a false failure in the
harness.

The broker is wrapped so that `place_market`, `modify_position` and
`close_quantity` raise. If a guard ever fails to fire, the case raises and the
script fails; it cannot quietly submit. The account is counted before and after,
so "no order" is measured rather than asserted.

| case | expected | observed | order |
|---|---|---|---|
| delayed signal, recovered 10 min late | `stale` | `stale` — "posted 10m before it reached the pipeline, beyond the 2m limit" | none |
| stale quote (60 s old, 5 s budget) | `blocked` | `blocked` — "broker quote is stale" | none |
| crossed book | `blocked` | `blocked` — "quote is invalid (bid=3901.97, ask=3862.41)" | none |
| quote timestamp from the future | `blocked` | `blocked` — "in the future beyond tolerance" | none |
| market already through the published stop | `blocked` | `blocked` — "market has already crossed the published stop" | none |
| edited signal, live edit event | `manual_review` | `manual_review` — "never amend an existing intent" | none |
| malformed signal, no stop | `manual_review` | `manual_review` — "entry, stop, and at least one target are required" | none |
| excessive spread (limit below the live spread) | `blocked` | `blocked` at `max_spread`; the other five checks passed | none |
| excessive slippage (entry 3.00 from market, limit 0.01) | `blocked` | `blocked` at `max_slippage`; the other six passed | none |
| pause enabled | `blocked` | `blocked` — "execution is paused"; refused at the first check | none |

Repeated runs, and the account was 0 positions / 0 open orders before and after
each. Evidence: `docs/evidence/intake-cases-*.json`.

One case in that table is only half the story. "Edited signal" as the probe
drives it is a message with `revision 1` — an edit the pipeline *saw arrive*.
The edit that actually mattered is the one it never saw arrive, and it is §7.

The remaining cases were exercised in the running service rather than by the
probe, because they depend on delivery rather than on a single evaluation:

| case | how | result |
|---|---|---|
| duplicate text, different message id | two identical posts, ids 10 and 11, in Mode B | msg 10 → trade; msg 11 → `not_copied`, "an identical signal was already copied as trade RADE-232b… (message 10); this is the same call posted again, so it is not copied twice". Size 0.0: nothing was sized. |
| replay after restart | a stored backlog replayed under a 5 s limit | messages 4 and 6 → `stale` ("posted 15m / 11m before it reached the pipeline"), 5 → `manual_review`; notifications `stale ×2`, `review ×1` |
| reconnect and catch-up | the listener reconnects every 20 s in Mode B | every post was picked up by catch-up; no message was processed twice |
| position closed while the process was down | the duplicate-test position was closed deliberately, then the service restarted | startup reconcile: `{'considered': 1, 'open': 0, 'closed': 1, …}`; the trade became `closed` with "the position closed while this process was not running"; one `closed` notification, no invented transitions |

The duplicate case is the one that matters most, because the real channel has
already produced it: `18760/18761` and `18788/18789` were the same call posted
twice. The protection is text-hash based, not id based, so a new message id does
not buy a second position.

## 7. The defect this milestone found: an edited signal that was copied

The failure list includes "edited signal", and the existing guard covered the
live case: a Telegram edit event carries `revision + 1`, and any revision above
zero is refused as `manual_review`.

That guard had a hole, and the hole is exactly the shape of the signal-age bug.
`revision` only rises when the pipeline *saw the original*. A message edited
before the pipeline first saw it — after a restart, or while a catch-up was still
behind the cursor — arrives with `revision 0` and an edit timestamp, and was
therefore treated as a first-class call.

It was found by driving a real edit through the real delivery path: message 12
was posted, edited eleven seconds later, and the listener was then started with a
cursor before it. Before the fix, the same body evaluated without the edit
timestamp produces a 0.03-lot intent:

```
as an edit   -> manual_review | intent: False
as a fresh   -> paper_intent  | intent: True | lots: 0.03
```

After the fix the same delivery produces:

```
catchup id=12 rev=0 -> manual_review (this message was edited after it was posted
and was first seen in its edited form (posted 2026-11-13T17:40:45+00:00, edited
2026-11-13T17:40:56+00:00); the original call is unknown, so it is not copied)
```

The fix checks `edited_at` as well as `revision`, and says which of the two it
saw, because the operator's next question is different in each case. It also
runs **before** the age check: an edit is refused on principle, and reporting
only "stale" would suggest that a fresher copy of an edit would be acceptable.

## 8. The second defect: a read failure was treated as a close

This one is the most serious thing the milestone found, and it was found by
reading the log rather than by a test.

During the ladder run the broker returned **HTTP 429 Too Many Requests** on
`/positions`:

```
could not read the broker's positions: reading positions failed: HTTPError:
'{"status":429,"error":"Too Many Requests","path":".../positions"}'
management: {'considered': 1, 'closed': 1, ...}
```

The manager recorded the trade as `closed`, sent a `[CLOSED]` notification, and
stopped managing it — **while the position was still open at the broker**, with
its stop and target attached. The next read, seconds later, showed it plainly:

```
POSITIONS NOW: [(216172782118684407, 'buy', 0.03)]
ledger:        state='closed'
```

The cause was a single line: `_position_for` caught `BrokerError` and returned
`None`, and `None` already meant "the broker answered and there is no position".
Two different facts collapsed into one value, and the more dangerous of the two
won. This is the same class of mistake as trusting a fresh quote for a fresh
signal: **an absent answer is not a negative answer.**

The fix introduces `POSITIONS_UNREADABLE`, distinct from `None`, and every
caller now has to say what it does with it:

| caller | before | now |
|---|---|---|
| `manage_trade` | closed the trade, notified | leaves it open, returns an error, retries next pass |
| `reconcile_trade` | could write the trade off | leaves it untouched, reports `unreadable` |
| close verification | `after.qty` on `None` | the ladder does not advance past an unverified close |
| `confirm_fill` | — | still accepts the order feed as evidence of a *fill* |

`manage()` also counts the refusal in `errors`, which it previously dropped on
the floor: an outcome with neither `closed` nor `partial` nor `stopped` was
counted as nothing at all.

### 8.1 The divergence it left, and the detector it needed

The false close left the ledger saying `closed` for a live position, and
reconciliation could not find it, because reconciliation only walks trades that
are still *active*. A trade wrongly marked terminal is invisible by
construction.

`orphaned_positions()` now looks the other way round: broker positions whose
correlation tag maps to a trade the ledger holds as finished. It is reported at
startup and counted in the reconciliation summary, not repaired — deciding which
side is right is a human's job, and a repair that guessed would be worse than
the alarm. Run against the divergent ledger it reported exactly what had
happened:

```
ORPHANED POSITION: the broker still holds 0.03 lots (position 216172782118684407)
for trade RADE-534bad7e0d6d8a0e0faf8626, which the ledger records as 'closed'.
Nothing is managing it.
execution reconciliation on startup: {..., 'orphaned': 1}
```

The position was then closed by hand and the account confirmed flat.

## 9. The third defect: a notification kind that swallowed legs

The notification ledger dedupes on `(event_key, kind)`. Both ladder legs used
the bare kind `"partial"`, so TP2's close and TP2's stop move were written as
duplicates of TP1's and never sent. The Mode B ladder in §5 therefore produced
one `partial` and one `stop` for a trade that had taken two partial closes — the
ledger was right and the operator was told half the story.

The kinds are now leg-specific — `partial:tp1`, `partial:tp2`, `stop:tp1`,
`stop:tp2` — and the test drives TP2 and asserts a second `STOP MOVED` rather
than asserting that "a notification happened". This is the same class of mistake
as deduplicating on the trade alone: a key that is too coarse turns a delivery
mechanism into a silent loss.

The fixed behaviour is proven by that test. A live re-run to completion was
attempted after the fix and did not finish inside the session: the market did not
reach the first target, and the attempt was closed by hand rather than left
running. The ladder *mechanics* are evidenced live by §5; only the
per-leg notification delivery rests on the test, and that is stated here rather
than implied.

## 10. What was not verified: live push

**An edit sent while a listener is connected produced no event the listener
saw.** This was checked on the test channel and, separately, on Saved Messages:
`events seen: []` in both cases. Catch-up works, and every case in this record
that needed an edit was delivered by catch-up.

That is not a defect in the pipeline — it is a property of this environment's
Telegram session — but it has a deployment consequence that must not be papered
over: **if live push does not arrive in production either, the pipeline runs on
catch-up alone**, and the latency of every copy is the reconnect interval. That
is why the service prints `intake limits` at startup with
`catchup_can_exceed_age_limit`, and warns when the reconnect interval is longer
than the signal-age limit. A deployment in that configuration does not copy
late signals; it copies nothing, and should say so.

Until live push is observed in the target environment, `MIRROR_RECONNECT_S` is
the effective delivery interval and must stay well under
`MIRROR_MAX_SIGNAL_AGE_S`.

## 11. State at the end

* No listener on the trader channel: every service port refuses connections and
  no service process remains.
* Demo account flat: 0 positions, 0 open orders.
* The two Mode B ledgers (`data/test_b.sqlite`, `data/test_b2.sqlite`) and the
  replay ledger are test artefacts and are not committed; the evidence exports
  in `docs/evidence/` are.
* `MIRROR_EXECUTE` stays `0` on the trader channel until the remaining behaviour
  in the task list is added and this milestone's checks are re-run against it.

## 12. Reproducing it

```bash
# the intake and guard cases; refuses anything but demo, cannot submit
python scripts/probe_intake_cases.py

# Mode A: parse, record, notify, no broker order
. data/test_env_a.sh && python -m rade.observer.service

# Mode B: one order, full ladder, demo only
. data/test_env_b2.sh && python -m rade.observer.service
python scripts/post_test_signal.py --side buy

# the fake signals themselves
python scripts/test_channel.py --post "XAUUSD BUY NOW …"
python scripts/test_channel.py --edit 12:"XAUUSD BUY NOW …"

# the ledger, read-only
python scripts/export_mirror_evidence.py --db data/test_b2.sqlite
```
