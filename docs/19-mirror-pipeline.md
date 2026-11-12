# 19 — Telegram → paper mirror

Status: **built and tested locally; not deployed; no broker order capability.**

This is the record for the mirror pipeline. The retrospective replay study stays
closed (`docs/16-replay-results-1m.md`); nothing here reopens it, and no exit
policy is selected.

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
| `render.yaml` | Render blueprint (free web service, no cron, no worker) |
| `web/` | Vercel dashboard + server-side proxy functions |
| `scripts/simulate_mirror_pipeline.py` | 15-scenario reproducible simulation |
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

---

## 2. What was verified

All commands run from the repository root on 2026-11-12.

| Check | Command | Result |
|---|---|---|
| Full test suite | `python -m pytest tests/ -q` | **198 passed** in 140s (was 126 before this work) |
| Simulation matrix | `python scripts/simulate_mirror_pipeline.py --message-id 18048` | **15/15 PASS**, exit 0, evidence `docs/evidence/mirror-simulation.json` |
| Dashboard proxy | `node --test web/test/proxy.test.js` | **6/6 pass** |
| Service over HTTP | started on `:8099`, called with `urllib` | `/healthz` 200, `/api/status` 401 without token, 200 with, `paper_only: true`, `broker_writes: false` |
| Single-file test isolation | `python -m pytest tests/test_mirror_pipeline.py` | passes standalone (it did not before; see §5) |

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
* **It does not establish a latency figure.** Corrected by the measured host
  clock error (19,429.9 s behind true UTC, from an HTTP `Date` header with a
  7.5 s round trip), the update arrived about **30 s ± 4 s** after the edit. That
  is a single sample of an *edit*, and Telegram's edit propagation is not the
  same path as a new-message push. It neither proves nor disproves the 2 s
  reception budget.
* **It does show the clock is still the blocker.** Any end-to-end latency number
  from this host is unmeasurable until `w32time` runs or the measurement moves to
  the deployment host.

I did **not** start a second listener. The soak holds the Telethon session, and
the CLI refuses to start while `docs/evidence/telegram-soak.heartbeat.json` is
fresh, precisely because two readers of one session corrupt each other.

---

## 5. Deployment

### Render

Verified against `https://render.com/docs/free` (fetched 2026-11-12):

| Fact | Consequence for this design |
|---|---|
| "Free instances" exist for Web Service, Postgres and Key Value; "Other service types don't support Free instances" | **No free background worker and no free cron job.** The listener runs as a thread inside the web service (`MIRROR_RUN_LISTENER=1`). |
| A free web service "spins down … that goes 15 minutes without receiving any inbound traffic"; spinning back up "takes about one minute" | An external monitor pinging `/healthz` every few minutes prevents most spin-downs, but this is not a guarantee: Render "might restart a Free web service at any time". |
| The filesystem is ephemeral; changes are "lost every time the service redeploys, restarts, or spins down" | Supabase is the store, and the listener cursor is recovered from the database (`Runtime.restore_cursor`) rather than from a file. |
| 750 free instance hours per workspace per month | One always-on service fits; a second one does not. |

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

`supabase/migrations/0001_mirror_events.sql` is the schema of record. Apply it
with `supabase db push` or by pasting it into the SQL editor. RLS is enabled with
**no policies**, so the anon and authenticated roles can neither read nor write;
only the service-role key (backend) can. Not applied: no Supabase project
credentials in this session.

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

`MIRROR_CONTRACT_UNITS_PER_LOT` is preset to `100` from the TradeLocker XAUUSD
read-only response. It is **not confirmed by a fill**; confirm it against a demo
order before trusting the lot size.

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
2. Supabase in practice — the migration has not been applied to a real project,
   and the adapter has only been exercised against a fake PostgREST.
3. Render and Vercel deployment, including the spin-up behaviour from cold.
4. BotFather delivery — the adapter is tested against a mock transport, not the
   Bot API.
5. The XAUUSD contract size, until a demo fill confirms it.
6. Everything about exits, because no exit policy exists.
