# Why the morning signal was blocked

**Date:** 2026-11-16 · **Scope:** intake, execution path, deployment env ·
**Status:** config cause fixed and verified live; one risk decision left open.

## What was observed

The channel posted a valid market call and the mirror recorded it as `blocked`:

```
msg 18048 rev 0   2026-11-15T23:25:05Z
status:   blocked
reason:   contract units per lot are not confirmed against the broker
          specification; no size is computed
text:     XAUUSD SELL NOW 3915✅ / Sl : 3925 / Tp : 3910 / Tp : 3905 / Tp : 3900
```

The message was parsed correctly. The stop was on the right side of the entry.
Nothing about the signal was wrong. It was refused before any size was computed.

## Root cause

`MIRROR_CONTRACT_UNITS_CONFIRMED` was not set in the Render environment, and
neither was `MIRROR_CONTRACT_UNITS_PER_LOT`. In `mirror_pipeline.evaluate_message`
the contract-size check runs **before** equity, before the quote and before
sizing, so every market signal was refused at the same line. The check is
correct — a lot count derived from an unverified contract size is not a risk
limit — but the setting it needs was never deployed.

Render held only 20 environment variables. Everything the intake path needs was
absent: no `MIRROR_RUN_LISTENER`, no `MIRROR_QUOTE_PROVIDER`, no
`MIRROR_CONTRACT_UNITS_*`, no `MIRROR_FIXED_LOTS`, no `MIRROR_EQUITY`, not even
`PYTHONPATH`.

## The second blocker, behind the first

Clearing the contract size would not on its own have copied a trade. Two more
settings were missing, and both refuse a NOW call:

* `MIRROR_QUOTE_PROVIDER=none` — a market signal cannot be priced, so it is
  blocked with "market signal needs a fresh broker quote".
* `MIRROR_CONTRACT_UNITS_PER_LOT` — required once `CONFIRMED=1`.

Both are now set. `QUOTE_PROVIDER=tradelocker` reads the demo feed; the gateway
refuses any environment but `demo`.

## What was set, and against what evidence

| Setting | Value | Source |
|---|---|---|
| `MIRROR_CONTRACT_UNITS_PER_LOT` | `100` | broker's own `lotSize` for XAUUSD on BLBRY, read 2026-11-15 |
| `MIRROR_CONTRACT_UNITS_CONFIRMED` | `1` | the value above was checked against the broker |
| `MIRROR_QUOTE_PROVIDER` | `tradelocker` | demo quote feed |
| `MIRROR_EQUITY` | `4862.73` | broker `projectedBalance` on BLBRY 2346225, read 2026-11-15 |
| `MIRROR_FIXED_LOTS` / `MIRROR_MAX_TRADE_RISK_USD` | `0.03` / `25` | unchanged — a risk decision, not a fault |
| `MIRROR_EXECUTE` | `0` | unchanged — the live flip is still off |

The destination was **not** changed. Render holds BLBRY / 2346225 with working
credentials; the local `.env` holds a different, older account, and
`scripts/update_render_env.py` reads the remote set back before writing so a
stale local file cannot retarget it.

Verified live after the redeploy: `units_confirmed=true`, `intake_ready=true`,
`intake_problem=""`.

## Code defects found and fixed in the same pass

Each has a regression test. See `tests/test_cursor_and_quote_guards.py` and
`tests/test_execution.py`.

1. **A trade opened but never submitted was orphaned.** `execute` writes the
   ledger row before it sends the order. A restart in that window left the row
   in `validated` permanently, because the management pass walked only accepted
   and open trades. The signal was recorded and then never acted on.
2. **Order submission is now serialised.** The listener and the management pass
   can both reach an unsubmitted trade; the "does the broker already have this
   order" check is only safe if they cannot run concurrently.
3. **A crossed book reached the ladder.** `manage_trade` used the quote without
   validating it. The broker's feed was crossed at the time of writing
   (bid 3888.26 > ask 3888.09), so this is not hypothetical.
4. **`build_exit_plan` now validates geometry.** It is a second entry point: the
   engine builds a plan from the signal it was handed. A plan built from a stop
   already triggered would be sent to the broker live.
5. **The gateway's credential scrubber was dead.** `_secrets` was always empty;
   it is now populated from the credentials the factory used.
6. **A failed cursor write rolled the cursor forward**, so a message that was
   never claimed was skipped permanently.
7. **`/api/*?limit=abc` returned 500**, and a negative limit returned almost the
   whole list.
8. **The status page hid the blocker.** It now reports `units_confirmed`,
   `intake_ready` and `intake_problem` separately from the sizing rule.

## Still open — a risk decision, not a bug

`0.03` lots at `100` units/lot is **$3.00 per point**. Against a `$25` ceiling
that allows a stop of **8.33 points**. Every distinct signal in the recent
ledger carries a **10-point** stop, i.e. `$30.00` of risk — so the ceiling
refuses all of them. That is the rule working as written, not a fault.

Options, with the median stop (10 pts) costed:

| Lot | Risk at a 10-pt stop | Max stop at a $25 ceiling |
|---|---|---|
| 0.02 | $20.00 | 12.50 pts |
| 0.025 | $25.00 | 10.00 pts |
| 0.03 | $30.00 | 8.33 pts |

Raising the ceiling to `$30` keeps the lot at `0.03`. Lowering the lot to
`0.02` keeps the ceiling at `$25`. Both are the operator's call; nothing was
changed.

## Also worth knowing

* The channel emits an edit event for messages whose text is unchanged. It was
  stored as a `rev 1` row identical to `rev 0`, which inflated the stored-event
  counts and would have sent an unmodified signal to manual review. **Fixed**
  after this note was first written: the edit handler now compares the new text
  hash against the newest stored one and ignores a no-op edit. An edit that
  changed the text is still stored as a new revision.
* `scripts/diagnose_broker_state.py` placed a market order, moved its stop and
  closed it on every run — a diagnostic wrote to the destination account
  regardless of the service's paper mode. It is now read-only unless
  `--place-test-order` is passed, and the close is in a `finally` block.
* The service reports the stop width the ceiling admits (`max_stop_distance`,
  currently 8.33 pts) on `/api/status` and in the startup log, so the mismatch
  below is visible rather than silent.
* `MIRROR_EXECUTE` is still `0`. Flipping it is the live-flip runbook in
  `docs/26-live-execution-spec.md` §7, and it needs the destination pin
  confirmed first.

## Environment note

Render holds the broker credentials for BLBRY / 2346225 and they are correct.
The local `.env` holds a different, older account (PLEXY / 2509174). Nothing
reads the broker credentials from `.env` any more: `scripts/update_render_env.py`
reads the remote set back and writes the union, so a stale local file cannot
retarget the deployed destination. `scripts/configure_render_service.py` sets
only the service fields and delegates the env to it.

