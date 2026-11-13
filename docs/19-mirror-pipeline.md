# 19 — Telegram → paper mirror

Status: **built and tested locally; not deployed; no broker order capability.**

This is the record for the mirror pipeline. The retrospective replay study stays
closed (`docs/16-replay-results-1m.md`); nothing here reopens it, and no exit
policy is selected.

A deployment review on 2026-11-13 found five ways this could have been deployed
while looking healthy and behaving badly. All five are fixed; §9 records what
each one was and what changed.

---

## 1. What is built

| Module | Role |
|---|---|
| `src/rade/observer/mirror_pipeline.py` | strict parser, validation, paper sizing, dispositions, SQLite ledger |
| `src/rade/observer/store.py` | `SupabaseLedger` (PostgREST) behind the same interface as SQLite |
| `src/rade/observer/listener.py` | Telethon listener: new messages, edits, reconnect + gap fill, cursor, heartbeat |
| `src/rade/observer/notify.py` | BotFather adapter, silent for ignored/duplicate, one notification per event |
| `src/rade/observer/service.py` | read-only HTTP API (stdlib, no new deps) with bearer auth |
| `src/rade/observer/quotes.py` | demo-only read-only bid/ask; refuses a live environment |
| `supabase/migrations/0001_mirror_events.sql` | schema of record, constraints, RLS deny-all |
| `supabase/migrations/0002_notification_and_cursor.sql` | notification state machine + the durable cursor table |
| `render.yaml` | Render blueprint (free web service, no cron, no worker) |
| `web/` | Vercel dashboard + server-side proxy functions |
| `scripts/simulate_mirror_pipeline.py` | 16-scenario reproducible simulation |
| `scripts/telegram_paper_listener.py` | CLI entry point; refuses to run beside another listener |

### Dispositions

`paper_intent`, `ignored`, `manual_review`, `blocked`, `skipped_minimum_lot`,
`duplicate`, `conflict`. Only `paper_intent` carries an intent, and that intent
always has `filled: false` and `order_state` of `pending_limit` or
`would_fill_market`. There is no code path that produces a fill.

### The rules that are enforced, not documented

* **Idempotency is a database constraint.** `event_key` (sha256 of channel,
  message id, revision) is the primary key and `(channel, message_id, revision)`
  is additionally unique, in both SQLite and the Postgres migration. A restart,
  a redelivery, or two overlapping listeners cannot create a second intent.
* **A changed body under the same revision is a conflict, not an update.** The
  stored row is kept and the new body is routed to review.
* **An edit gets `revision + 1`** from the ledger and is never applied
  automatically, because it may be a stop move or a partial close and the exit
  policy is unresolved.
* **The target ladder is normalised once, here.** The legacy replay engine never
  did this and produced a single −36.78 R leg on one setup; the normalisation is
  recorded as `ladder_normalised` on the signal.
* **Ambiguity goes to a human.** Two entry lines, a stop or target line carrying
  more than one number, duplicate targets, a target on the wrong side, an
  instrument mismatch — all `manual_review` with no intent.
* **Missing inputs block.** No equity, no contract specification, no quote, a
  stale quote, an inverted quote, or a quote that has already crossed the stop.
  Nothing is defaulted. The per-trade risk cap is 0.5% and a larger configured
  fraction blocks rather than being clamped.
* **An unconfirmed contract size is not a contract size.** `InstrumentSpec`
  carries `units_confirmed`, default `False`. Until a human has checked the
  number against the broker's own specification, every signal is recorded as
  `blocked` and nothing is sized — including limit calls, which is where the
  unverified number was reachable.
* **A first activation never imports the channel's history.** With no cursor,
  the start point is the newest message at the moment of the first connect.
  Importing history at all takes two separate switches.
* **A hosted deployment refuses to start without a durable store.** Falling
  back to SQLite on a host that wipes its disk is refused, not accepted.
* **A failed notification is recorded as failed.** The slot is reserved as
  `pending` and completed as `sent` or `failed`; only `sent` suppresses a
  resend, and failed sends are retried.
* **A healthy service reports whether the intake is healthy.** `/healthz`
  answers 503 when the listener is expected but is not running, so an uptime
  ping cannot be mistaken for a working mirror.

---

## 2. What was verified

All commands run from the repository root. The first row was measured on
2026-11-12; the review fixes were re-verified on 2026-11-13.

| Check | Command | Result |
|---|---|---|
| Full test suite | `python -m pytest tests/ -q` | **241 passed** in 50s (was 228 after the first review round, 198 after the pipeline landed, 126 before it) |
| Simulation matrix | `python scripts/simulate_mirror_pipeline.py --message-id 18048` | **16/16 PASS**, exit 0, evidence `docs/evidence/mirror-simulation.json` |
| Dashboard proxy | `node --test web/test/proxy.test.js` | **6/6 pass** |
| Service over HTTP | started on `:8099`, called with `curl` | `/healthz` 200 with a `listener` block, `/api/status` 401 without token, 200 with, `paper_only: true`, `broker_writes: false` |
| Hosted refusals | `RENDER=true python -m rade.observer.service` | exit **3** with "hosted mode requires SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY"; exit **3** with "MIRROR_EQUITY is not set" when the listener cannot start |
| Paper-only deployment check | service + live listener against the real channel | 0 events imported, cursor `latest:18799`, `/healthz` 200 with a healthy listener — evidence `docs/evidence/deploy-check-2026-11-13.json` |
| Single-file test isolation | `python -m pytest tests/test_mirror_pipeline.py` | passes standalone (it did not before; see §3) |

### The simulation scenarios (real archived message 18048)

The base case is a genuine stored message — `XAUUSD SELL NOW 3915`, SL 3925,
TPs 3910/3905/3900 — not a fixture written to match the parser.

| Scenario | Expected | Got |
|---|---|---|
| real archived signal | paper_intent | paper_intent |
| duplicate delivery | duplicate | duplicate |
| restart replay (new ledger, same file) | duplicate | duplicate |
| same revision, changed body | conflict | conflict |
| edited signal | manual_review | manual_review |
| malformed call | ignored | ignored |
| recap post | ignored | ignored |
| news post | ignored | ignored |
| missing quote | blocked | blocked |
| stale quote | blocked | blocked |
| missing contract spec | blocked | blocked |
| unconfirmed contract units | blocked | blocked |
| risk above the 0.5% cap | blocked | blocked |
| missing equity | blocked | blocked |
| minimum lot exceeds risk budget | skipped_minimum_lot | skipped_minimum_lot |
| limit call | paper_intent, `pending_limit`, `filled: false` | same |

**This does not prove live push delivery.** Every message in the simulation comes
from the local database, which was populated by the fetch path. §4 covers the one
live observation that exists.

---

## 3. Bugs found while building this

Each of these would have looked like working software.

1. **Every live NOW signal would have been blocked as "stale".** The listener
   fetches the quote *after* the message arrives, so `received_at − quoted_at`
   is negative. The original check was `if age < 0: stale`, which is true for
   every live market call. Freshness is now `quoted_at < received_at − max_age`,
   with a separate guard for a quote timestamped implausibly far in the future.
2. **SQLite connections were never closed.** `with sqlite3.connect(...)` commits
   but does not close, so every ledger call leaked a handle. On Windows it also
   kept the database file locked and broke cleanup; in a long-running listener it
   leaks until garbage collection. Now a `_connect()` context manager closes
   explicitly.
3. **The test suite depended on collection order.** `pytest tests/` passed
   because an earlier module happened to insert `src` on `sys.path`;
   `pytest tests/test_mirror_pipeline.py` failed with `ModuleNotFoundError`. A
   root `conftest.py` now fixes it for every invocation.
4. **The simulation's "tampered" and "edited" bodies were byte-identical to the
   original.** `str(3925.0)` is `"3925.0"`, which never matches `3925` in the
   message, so two scenarios passed without testing anything. The fixtures now
   format prices the way the channel writes them and assert the body actually
   changed.

---

## 4. The live path

The four-hour soak (`docs/17-observer-step1.md`) recorded its **first
live-delivered update** at 2026-11-12T22:22Z: message `18786`, delivered through
the edit handler, one second after the channel edited it.

What that does and does not establish:

* **It establishes** that the live connection receives updates without a fetch.
  The push path is no longer purely theoretical.
* **It does not establish a latency figure.** The probe measured the host clock
  at 19,423.8 s behind true UTC for that run and corrected the sample to
  **25,524.8 ms** after the original post — about 24.5 s after the edit itself.
  The `Date` header is accurate to roughly ±1 s and the round trip was 7.5 s, so
  the estimate is good to a few seconds, not to the 2 s budget. It is a single
  sample of an *edit*, and Telegram's edit propagation is not the same path as a
  new-message push. It neither proves nor disproves the reception budget.
* **It sharpens the remaining gap.** Message 18786 was posted *inside* the soak
  window and its edit arrived, but no `NewMessage` update for it ever fired. The
  new-message push path has therefore still never been observed, and one sample
  now suggests something specific to investigate: Telegram may have collapsed the
  post and its edit into a single edit update because delivery was ~25 s late.
  That is a hypothesis, not a finding.
* **It does show the clock is still the blocker.** Any end-to-end latency number
  from this host is unmeasurable until `w32time` runs or the measurement moves to
  the deployment host.

The run ended in a network outage that disconnected the client, so the final
catch-up audit is reported as `INCOMPLETE` rather than as a set comparison — the
false-pass fixes from `docs/17` §8 holding under a real failure rather than a
constructed one. Duplicates: PASS (none). Edits: inconclusive (the refetch never
ran).

I did **not** start a second listener. The soak holds the Telethon session, and
the CLI refuses to start while `docs/evidence/telegram-soak.heartbeat.json` is
fresh, precisely because two readers of one session corrupt each other.

---

## 5. Deployment

### Render

Verified against `https://render.com/docs/free` (fetched 2026-11-12, and
re-fetched 2026-11-13 to confirm it independently):

| Fact | Consequence for this design |
|---|---|
| "You can deploy certain Render service types free of charge: Web services…, Render Postgres databases, Render Key Value instances"; "Other service types don't support Free instances"; free web services don't support "Running one-off jobs" | **No free background worker and no free cron job.** The listener runs as a thread inside the web service (`MIRROR_RUN_LISTENER=1`). |
| "Render **spins down** a Free web service that goes 15 minutes without receiving any inbound traffic… This process takes about one minute." | An external monitor pinging `/healthz` every few minutes prevents most spin-downs, but this is not a guarantee: "Render might restart a Free web service at any time." |
| "any changes to your web service's filesystem (uploaded images, **local SQLite databases**, etc.) are *lost* every time the service redeploys, restarts, or *spins down*" | This is precisely the store fix in §9.2: hosted mode refuses to fall back to SQLite, and the cursor lives in the database. |
| "Render grants **750 Free instance hours** to each workspace per calendar month" | One always-on service fits; a second one does not. |
| "Render may suspend a Free web service that initiates an uncommonly high volume of traffic over the public internet. Examples of service-initiated traffic include: **Accessing an external database**; **Invoking external APIs**." | A risk this design cannot engineer away: the mirror polls Supabase and Telegram, which is exactly the pattern Render names. A suspension would stop the intake until the service is restored or moved to a paid plan. |

**Therefore: this is not a 24/7 listener and nothing here claims it is.** The
design tolerates restarts instead of pretending to prevent them: on restart the
cursor comes from the database, the gap is refetched, and duplicates are
suppressed by the unique constraint. A message posted during a spin-down is
recovered by that gap fill.

The service is **not deployed**: there is no Render account access in this
session. `render.yaml` is complete and will deploy as-is once the secrets in §6
are set.

### Vercel

`web/` is a static dashboard plus four serverless functions that proxy to Render.
Set the Vercel project root to `web`.

The browser holds only `DASHBOARD_TOKEN` (kept in `sessionStorage`). The Render
bearer token and the Supabase service-role key stay in server-side environment
variables. The functions rebuild the query string from a whitelist rather than
passing it through, and the tests assert that the backend token never appears in
a response body.

Not deployed: no Vercel account access in this session.

### Supabase

`supabase/migrations/0001_mirror_events.sql` and
`supabase/migrations/0002_notification_and_cursor.sql` are the schema of record.
Apply them with `supabase db push` or by pasting them into the SQL editor, in
order. RLS is enabled with **no policies**, so the anon and authenticated roles
can neither read nor write; only the service-role key (backend) can. Not
applied: no Supabase project credentials in this session.

---

## 6. What you need to enter

Values only. Do not paste any of these into a chat window.

**Supabase** (project settings → API)
| Variable | Where |
|---|---|
| `SUPABASE_URL` | Render |
| `SUPABASE_SERVICE_ROLE_KEY` | Render only — never Vercel, never the browser |

**Render** (dashboard → the `rade-mirror` service → Environment)
| Variable | Notes |
|---|---|
| `MIRROR_API_TOKEN` | generate: `python -c "import secrets;print(secrets.token_urlsafe(32))"` |
| `MIRROR_EQUITY` | the paper account's equity; sizing is refused without it |
| `TELEGRAM_API_ID`, `TELEGRAM_API_HASH` | from my.telegram.org |
| `TELEGRAM_SESSION` | run `python scripts/export_telegram_session.py` locally and paste the output |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_NOTIFY_CHAT_ID` | BotFather; the chat id is where notifications go |
| `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY` | from above |
| `MIRROR_CONTRACT_UNITS_PER_LOT` | **not preset.** Read it from the broker's own specification and enter it here |
| `MIRROR_CONTRACT_UNITS_CONFIRMED` | set to `1` only once the number above has been checked against that specification. Until then every signal is recorded as `blocked`, not sized |

The contract size used to be preset to `100` in `render.yaml`, taken from a
TradeLocker read-only response and never confirmed by a fill. That preset is
gone: the value is entered by an operator, and it is not used for sizing until
it has been confirmed. `MIRROR_START_AT` defaults to `latest`, so the first
activation imports nothing that is already in the channel; set it to a message
id to import from that point, and `history` (plus
`MIRROR_ALLOW_HISTORY_IMPORT=1`) to import everything.

**Vercel** (project settings → Environment Variables)
| Variable | Notes |
|---|---|
| `DASHBOARD_TOKEN` | what the browser asks for; a separate secret |
| `MIRROR_API_URL` | `https://<service>.onrender.com` |
| `MIRROR_API_TOKEN` | the same value as on Render |

---

## 7. Safety posture

* **No broker write calls exist in the pipeline.** `quotes.py` references only
  `get_instrument_id_from_symbol_name`, `get_latest_bid_price` and
  `get_latest_asking_price`; a test asserts the listener module contains no
  `create_order`, `close_position`, `modify_position` or `place_order`.
* **The demo gate stays closed** and the stop-modification row stays BLOCKED.
  No policy was selected, so there is nothing to execute.
* **The exit policy is unresolved** and is labelled as such in the API
  (`exit_policy: "unresolved"`), in the dashboard badge, and in every
  notification footer.
* **The frozen holdout is untouched.** Nothing in this work reads or writes it.

---

## 8. What remains unverified

1. Live **new-message** push latency. One edit arrived ~30 s late; that is not a
   new-message measurement and the host clock cannot certify anything.
2. Supabase in practice — the migrations have not been applied to a real
   project, and the adapter has only been exercised against a fake PostgREST.
3. Render and Vercel deployment, including the spin-up behaviour from cold.
4. BotFather delivery — the adapter is tested against a mock transport, not the
   Bot API.
5. The XAUUSD contract size, until a demo fill confirms it. Until then nothing
   is sized, by design.
6. Everything about exits, because no exit policy exists.

---

## 9. The five deployment-review fixes (2026-11-13)

A review of commit `4ba07b2` found no exploitable vulnerability in the changed
runtime code, and five ways to deploy this while it looked healthy and
behaved badly. Each fix is enforced in code and covered by a test, not by a
convention in a runbook.

### 9.1 A fresh install replayed the channel's old history

**Was:** with no saved cursor, `gap_fill()` fetched from the beginning of the
channel. Every old call in the archive became a paper intent and, for the
notable ones, a phone notification.

**Now:** the first activation establishes a start point before the first fetch.
`Runtime.establish_baseline()` uses the newest message id in the channel at that
moment, so nothing older is imported; the floor is written to the state file
*and* to the `mirror_cursor` table, because the hosted disk is wiped on every
restart. `MIRROR_START_AT=<id>` imports from a chosen message instead, and
importing the whole history takes two switches (`MIRROR_START_AT=history` plus
`MIRROR_ALLOW_HISTORY_IMPORT=1`). A client that cannot report the newest
message id makes the listener refuse to start rather than fall back to the
archive.

### 9.2 Supabase was optional in the code

**Was:** `make_ledger()` silently returned a SQLite ledger when the Supabase
credentials were missing. On Render that is a file that is wiped on every
redeploy and spin-down — a store that forgets while looking like it works.

**Now:** `make_ledger()` raises `StorageError` when the process is hosted
(`RENDER` is set, or `MIRROR_REQUIRE_DURABLE_STORE=1`) and Supabase is not
configured. `service.main()` exits **3** with the reason instead of serving.
The SQLite path is unchanged for local work.

### 9.3 A failed BotFather send was recorded as successful

**Was:** the notification slot was reserved with `ok=True` *before* the send.
A failed send left a row that said "delivered", and nothing retried it.

**Now:** the slot has a state machine. `begin_notification` reserves it as
`pending` and counts the attempt; `complete_notification` records the real
outcome as `sent` or `failed`. Only `sent` suppresses a resend. A transient
failure is retried with backoff, a permanent Bot API error (4xx other than 429)
is not retried, and `retry_failed_notifications()` re-sends whatever is still
marked failed — the stored signal and intent are enough to rebuild the message.
The new `notification_log.status` and `.attempts` columns are in migration
`0002`; rows written by the old code are backfilled from their `ok` flag.

### 9.4 The sizing configuration was not ready to trust

**Was:** `render.yaml` preset `MIRROR_CONTRACT_UNITS_PER_LOT` to `100` — a value
read once from a broker response and never confirmed by a fill. NOW calls were
blocked (no quote provider), but LIMIT sizing could still use the number.

**Now:** the preset is gone; the operator enters the value. `InstrumentSpec`
gained `units_confirmed`, default `False`, and an unconfirmed spec produces
`blocked` with no size — for limit calls too. `MIRROR_CONTRACT_UNITS_CONFIRMED=1`
is what makes the number usable, and it is an explicit act by a human. The CLI
requires `--confirm-contract-units` for live mode.

### 9.5 The health ping could give a false sense of operation

**Was:** `/healthz` returned 200 as long as the process was up. The listener
thread could be dead and the ping would still be green, so an uptime monitor
proved nothing about Telegram intake.

**Now:** `/healthz` reports the intake and answers **503** when the listener is
expected but is not running, its heartbeat is stale, or it failed to start. The
body carries only `expected`, `healthy`, `phase`, `heartbeat_age_s` and a fixed
reason string — no channel, no account, no store. A 90 s startup grace covers
the window before the first heartbeat, and Render's `healthCheckPath` now
restarts the service when the intake dies. `MIRROR_RUN_LISTENER=0` keeps
`/healthz` a plain process check.

### Not changed by this review

Broker order capability is still absent, the exit policy is still unresolved,
the demo gate is still closed, and the frozen holdout is untouched.

### The deployment check (2026-11-13)

The review asked for a paper-only deployment check after the fixes. Hosting
credentials do not exist in this session — no Supabase project, Render account,
Vercel project or BotFather token — so the **hosted** deployment cannot be
provisioned and no claim is made about it.

What was possible was run: the same service, the same listener thread, the same
durable store, against the real Telegram channel on this host
(`docs/evidence/deploy-check-2026-11-13.json`).

| Measurement | Result |
|---|---|
| Channel | `@XAUUSD_FOREX4` |
| Newest message id at the first connect | 18799 |
| Start point recorded | `latest:18799`, in the state file **and** in `mirror_cursor` |
| Events imported | **0** — none of the channel's ~18,800 messages became an intent |
| Listener | phase `listening`, thread alive, `/healthz` 200 with `listener.healthy: true` |
| Notifications | none sent (no BotFather token), so nothing left the host |
| Sizing | refused: `MIRROR_CONTRACT_UNITS_CONFIRMED` was not set, so nothing was sized |

Before the fix, that same start would have fetched the whole archive and turned
every old call in it into a paper intent, with notifications for the notable
ones. That is the single largest behavioural difference in this work.

Two things this check does **not** establish: the live push path (no message
arrived during the window, so it is still unobserved) and the hosted path (the
durable-store refusal and the health endpoint were verified locally and by unit
test, not against a real Supabase or Render).

---

## 10. The second review round (2026-11-13)

A second review, of the commits that fixed §9, found five more gaps — places
where the first round's fix could be reached around. All five are fixed, each
with a test.

### 10.1 The history-import safeguard could be bypassed

`MIRROR_START_AT=0` — or any negative number — was accepted as a message id.
Zero is the "no cursor" sentinel, so the cursor stayed at zero and the gap
fill read the entire archive, without the history-import switch ever being
involved.

Now a start point must be a positive message id. `establish_baseline()` raises
on zero or a negative value, and the service validates `MIRROR_START_AT`
*before* the listener thread exists, so the failure is a startup refusal
(exit 3) with a readable reason rather than a thread that dies a second later.
A start point beyond the newest message is accepted but warned about, because
it silently imports nothing until the channel catches up.

### 10.2 The migration preserved false "sent" records

Migration `0002` backfilled the old rows as `sent` when their `ok` flag was
true. But the old code wrote `ok=True` *before* the send, so that flag proves
only that the slot was claimed — not that Telegram accepted the message. Those
notifications would never have been retried, which is exactly the failure the
state machine was added to fix.

Legacy rows are now backfilled as `failed`, with a detail saying the delivery
is unconfirmed, so the retry pass picks them up. A duplicate phone message is
a smaller problem than a silently lost one. The same backfill runs in the local
SQLite migration.

### 10.3 Permanent Bot API errors were retried forever

The immediate retry path skipped permanent errors (a 4xx other than 429), but
the five-minute retry pass re-sent every failed notification without looking at
why it had failed. A revoked token would have been retried every cycle, burying
the failures a retry could actually fix.

The pass now checks the stored error: a permanent one is counted as `skipped`
and left alone; a transient one is re-sent.

### 10.4 A failed reconnect could still look healthy

After a reconnect failed, the runtime kept reporting a `listening` heartbeat
while it waited out the reconnect interval, so `/healthz` stayed green through
a Telegram disconnect.

A failed reconnect now sets a `degraded` phase, which the service reports as
**503 "listener reconnect failed"**. The heartbeat carries `reconnect_failed`
and `last_reconnect_error`, and the next attempt is scheduled in 30 s rather
than after the whole interval. A successful reconnect clears the state.

### 10.5 The listener still needed a contract size to start

The service refused to start when `MIRROR_CONTRACT_UNITS_PER_LOT` was empty —
including while the contract size was explicitly unconfirmed, which is the
configuration the Render blueprint ships. It exited before it could record a
single blocked signal, so the archive stayed empty for a reason that had
nothing to do with the mirror's health.

The sizing inputs are now required only when sizing is possible: the contract
size and the equity are needed once `MIRROR_CONTRACT_UNITS_CONFIRMED=1`, and
not before. While the size is unconfirmed the listener starts, connects, and
records every message as `blocked`. The same applies to the CLI. The pipeline
now checks the confirmation *before* the equity, so the reason names the real
blocker.

### The deployment check, rerun with the new configuration

The check was rerun with `MIRROR_CONTRACT_UNITS_PER_LOT` **and**
`MIRROR_EQUITY` both unset and the contract size unconfirmed — the
configuration the Render blueprint actually ships. The service came up, the
listener connected, the baseline was set to `latest:18799`, 0 events were
imported, and `/healthz` answered 200. The refusals were measured too:
`MIRROR_START_AT=0` and `MIRROR_START_AT=-7` both exit 3 with a readable
reason.
