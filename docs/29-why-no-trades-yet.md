# Why trades get refused, and what was changed

**Updated 2026-11-16** after the edit policy was changed on the lead's decision.
The original diagnosis is kept below because the measurement is still the
measurement.

## The other blocker: the service is asleep most of the time

**This is the bigger one, and it is infrastructure, not code.**

The Render service is on the **free plan**. Render spins a free web service down
after ~15 minutes with no inbound request and cold-starts it on the next one
(~30–60 s). The mirror's listener only runs while that process is up, so a
sleeping service **silently misses every signal posted during the gap**.

The ingestion log shows it plainly. Events arrive in bursts, not on the 15-second
poll cadence:

```
received 23:00  61 events   (the initial catch-up)
received 02:00   8       04:00   1       08:00   1
received 09:00   1       11:00   7       18:00   4
received 20:00   1       22:00   1
```

Signals posted at **10:35:33** were received at **11:08:26 — 33 minutes late**.
Nothing was ingested between ~09:06 and ~11:08. Each burst lines up with
something touching the service (a deploy, a dashboard load, a status check),
which is what wakes it.

The listener itself is healthy — `reconnects: 0`, `errors: []` — because each
process is short-lived and clean while it is awake. There is nothing to fix in
the loop.

**Fixes, in order of reliability:**

1. **A paid Render plan (Starter, ~$7/mo).** The service is not suspended. This
   is the only fix that does not depend on a third party staying up.
2. **An external ping every few minutes.** A cron or an uptime monitor hitting
   `/healthz` keeps the instance warm. A Hermes cron doing exactly that has been
   scheduled (`rade_keepalive.py`, every 5 minutes) — but it only works while
   this machine is on and Hermes is running, so it is a mitigation, not a fix.
3. **Move the listener to something that does not sleep** (a VPS, a worker that
   Render keeps warm). Same effect as (1) with more moving parts.

Until one of these is in place, the bot will keep missing most of the day no
matter how the intake rules are tuned.

## The decision

**An edited call is traded as it stands.**

This channel is run by a bot that amends its own posts within **0–3 seconds**.
The copier polls every **15 seconds**. Those two facts together meant the bot
had never once seen an unedited call: 11 of 11 in the shadow study, and every
real signal in the live ledger. The amended text is the trader's final
published instruction — the original was live for ~2 s, too short for anyone to
act on — so it is now copied.

What is **still** refused:

* **An amendment to a message already stored** (`revision > 0`). That is a stop
  move, a partial close or a correction to an intent this process may already
  hold a position for, and the exit policy for an amendment is unresolved.
* **A changed body under the same revision** — a conflict; the stored copy is
  kept and the new body is not applied.
* **A signal older than 120 s**, and every other guard below.

Provenance is recorded on the signal (`first_seen_edited`,
`first_seen_edited_at`) so the evidence trail says the original was never seen
rather than silently looking like an unedited call.

The shadow study's measurement rules are **unchanged** — it collects
independently and still uses the original post time for entry timing.

## 2. What the live service has actually seen

75 channel messages ingested since the listener started. Every one is accounted
for:

| Outcome | Count | What it is |
|---|---|---|
| `ignored` — not a recognised signal | 60 | VIP advertising, empty messages, market-open hype |
| `ignored` — recap/result post | 10 | Trader result recaps, which are never trade calls |
| `manual_review` | 4 | **Real signals, edited before the bot first saw them** |
| `blocked` | 2 | Real signals refused by the contract-size gate (fixed 2026-11-16) |

Of the 60 "not a recognised signal" messages, **none** contained a signal word
(`XAU`, `GOLD`, `BUY`, `SELL`, `SL`, `TP`, `ENTRY`, `LIMIT`, `NOW`). The parser
is not missing calls. The channel genuinely spends most of its time on
marketing and recaps.

## 2. The real blocker: the channel edits every signal within seconds

All four real signals the bot has ever seen were **first seen in their edited
form**:

```
message 18836  posted 02:02:18  edited 02:02:20  -> manual_review
message 18837  posted 02:02:18  edited 02:02:20  -> manual_review
message 18807  posted 23:07:56  (edited before first sighting) -> manual_review
```

The listener polls every 15 seconds. The channel edits its calls 0–3 seconds
after posting. **The bot therefore never sees an original call** — only the
amended one.

This matches the shadow study exactly: 11 of 11 collected signals were edited
0–3 s after posting, all `original_version_unknown=True`.

`mirror_pipeline.py` used to refuse an edited call on principle: the edit may be
a stop move, a partial close, or a correction, and the exit policy for an
amendment is unresolved. That rule is the one that was changed — see the top of
this note.

### The second finding: the ceiling is measured from the *live* market

The ceiling is not "10 points = $20". It is
`|current market − stop| × $2.00`, recomputed at the quote of the moment.

So on a **SELL** with the stop 10 points above the entry:

```
market at the entry      stop risk $20.00   inside
market 2 points lower    stop risk $24.00   inside
market 3 points lower    stop risk $26.00   REFUSED
```

A **BUY** behaves the other way — a falling market moves the entry *towards* the
stop, so it keeps passing. The asymmetry is correct (it is the real risk at the
real entry), but it means an adverse move of 3 points is enough to refuse a call
this channel posts constantly.

Measured 15-second move: p50 0.635, p90 1.385, p95 1.870, p99 2.540. A 3-point
adverse move is around p97–99 — uncommon, but not rare, and it is the
second-most-likely reason a signal gets refused.

### Every other refusal path, measured

| Guard | Threshold | Effect on a real channel signal |
|---|---|---|
| **Edit (first sighting)** | any edit | **Refused 100% — now fixed** |
| **Ceiling, live-measured** | $25 | Refuses a SELL once the market moves ≥3 pts against the entry |
| Slippage | 2.0 pts | ~4.3% of 15-second moves |
| Signal age | 120 s | Never binds at a 15 s poll; binds on a delayed catch-up |
| Duplicate text | 900 s | Correctly suppresses the channel's double-posts |
| Spread | 1.0 | Essentially never binds (p99 0.75) |
| Final target reached | — | Only when the market is already at the last target |

Sizing, the flip, the guards, the connection and the parser were all checked and
are not refusing anything.

## 3. The call that prompted this

```
XAUUSD SELL NOW 3877️   Sl : 3887   Tp : 3870   Tp : 3865
posted 02:02:18   edited 02:02:20   bot saw it 02:02:28
```

It would have been sized at 0.02 lots and sent. It was held because it was
edited 2 seconds after posting. It is also **stale now**: the market is at
3871.27/3871.59, so the slippage is 5.7 points (limit 2.0) and the stop would be
15.7 points away ($31.46, over the $25 ceiling). Relaxing the edit rule could not
have saved this one — only the next one.

## 4. "1 trade per day" — what that setting actually is

There is **no per-day trade count** anywhere in the bot. The "1" that appears on
the dashboard is `MIRROR_MAX_OPEN_POSITIONS` — the maximum number of positions
held **at the same time**.

It is not a throughput knob. `docs/25` §2 records its purpose:

> Total drawdown room is **$162.73** ... At $27 risk per trade, **six losing
> trades leave $0.73 and the seventh breaches the account**. The daily breaker
> (1% ≈ $49) and the max-1-position rule are the controls that stand between a
> bad run and a terminated account.

Raising it to 3 would allow three concurrent $20 positions — up to **$60** of
simultaneous risk, more than the $48.63 daily breaker it sits alongside. That
removes one of the two documented account-protection controls.

**Not changed.** It is a one-line change (`MIRROR_MAX_OPEN_POSITIONS=3` in
Render, and `render.yaml`) and it should be an explicit decision with the
consequence understood — not a side effect of a message sent while asleep.

## 5. Everything else verified working

| Connection | State |
|---|---|
| Telegram intake | listening, heartbeat 15 s, 0 errors |
| Render service | `live_trading=true`, executor enabled, no error |
| TradeLocker destination | BLBRY / account 2346225, instrument 4231, spec verified |
| Sizing | 0.02 lots, $2.00/point, $25 ceiling → 12.5-point stops admitted |
| Supabase | events, cursor row, notifications, `mirror_control` unpaused |
| Vercel dashboard | 200, auth enforced, API proxy 401 unauthenticated |
| Kill switch | `/api/pause` and `/api/resume` both wired |

The bot is ready. The next signal it is allowed to copy will be traded.
