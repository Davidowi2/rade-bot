# Rade Bot

Swing-direction trading bot that reads a trader's public Telegram channel and
copies the calls — slowly, directionally, over days — not scalping.

The project has two halves, and it matters which one you are looking at:

* **The research archive** (Phases 2A–2D). Ingestion, labelling, features,
  backtest, models, and a paired exit-policy replay. Its measured results are
  **negative or unresolved** under the repo's own stated assumptions.
* **The mirror service** (`src/rade/observer`, `src/rade/execution`). Reads the
  channel, parses and validates each call, sizes it against a fixed lot and a
  dollar ceiling, checks the account-level guards, and records the intent.
  Order execution exists and is wired, but is **off by default** and goes to a
  pinned demo destination only.

## Status

> **The mirror runs in paper mode against a TradeLocker demo account.**
> Telegram intake, validation, sizing and the pre-trade guards are live; no
> order is placed. `MIRROR_EXECUTE=0`, and flipping it is a deliberate,
> separately documented step — see
> [`docs/26-live-execution-spec.md`](docs/26-live-execution-spec.md) §7.
>
> **Deployment:** Render (the service, `rade-bot.onrender.com`), Vercel (the
> dashboard, `rade-bot.vercel.app`), Supabase (the ledger).
>
> **Dashboard:** [`docs/27-dashboard.md`](docs/27-dashboard.md).
>
> **Why a signal was blocked on 2026-11-15, and what was fixed:**
> [`docs/28-blocked-signal-postmortem.md`](docs/28-blocked-signal-postmortem.md).

## Research results (provisional, and mostly negative)

On 564,213 fetched 1-minute bars, all four exit policies have a **negative mean
on the validation split** (−0.060 to −0.074 R per setup), and they differ from
each other by only 0.0085 R — less than the entry spread. The runner's apparent
edge at 30 minutes reverses sign at 1 minute, so the 30m result is retracted.
**No exit policy was selected, and the runner's effect is unresolved** — its
interval on the 55 eligible validation setups is [−0.047, +0.071], wide enough
to hide an edge worth having.

**Read the intervals, not just the means.** All four validation intervals
include zero (e.g. Policy A: −0.074 R, 95% CI [−0.166, +0.018]). The correct
claim is that no policy is *demonstrated* to work on 380 setups — not that the
strategy is proven to lose, and not that the exit choice is irrelevant.

See [`docs/16-replay-results-1m.md`](docs/16-replay-results-1m.md) §1.

The mirror is not built on the replay's conclusions. It copies the channel's
calls under a fixed-lot rule and records everything, so the evidence is
prospective rather than fitted. The old research tree is preserved as the
archive — see
[`archive/2026-11-11-rade-phase2-frozen/MANIFEST.md`](archive/2026-11-11-rade-phase2-frozen/MANIFEST.md).

## Layout

| Path | What it is |
|---|---|
| `src/rade/observer/` | intake, pipeline, store, service (the running half) |
| `src/rade/execution/` | broker gateway, engine, ladder policy, guards, sizing |
| `src/rade/strategy/`, `src/rade/research/` | the research archive |
| `web/` | the dashboard (static + serverless functions) |
| `scripts/` | ingestion, diagnostics, deployment |
| `docs/` | the evidence trail — read `docs/26` before touching live trading |

## Run

```bash
.venv/Scripts/activate

# tests
python -m pytest tests/ -q

# run the service locally (reads .env; needs Supabase for a durable store)
python -m rade.observer.service
```

Deployment configuration:

```bash
# service fields + mirror env (reads the remote env back before writing)
python scripts/configure_render_service.py
python scripts/update_render_env.py --dry-run
```

## Non-goals (explicit)

- No scalping / no 1m/5m/10m candle trading.
- No live-account execution path. The gateway refuses any environment but
  `demo`, and refuses a destination that is not the pinned one.
- No live hedging in v1 — hedging is simulated as a direction probe.

## Security

Credentials live in `.env` (gitignored) and in the Render / Vercel secret
stores. Never commit them. See `.env.example` for the variable names and the
meaning of each.

The Render env holds the broker credentials; the local `.env` may hold a
different account. `scripts/update_render_env.py` reads the remote set back
before writing the union, so a stale local file cannot retarget the deployed
destination.
