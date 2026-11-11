# Rade Bot

Swing-direction trading bot that learns from a trader's public Telegram channel
and trades like them — slowly, directionally, over days — not scalping.

> **Status: frozen at Phase 2 research infrastructure (2026-11-11).**
> Phases 2A–2D are built (ingestion → labelling → features → backtest → models).
> `probe/`, `risk/`, `execution/` and `ops/` are empty stubs — there is no live
> trading path. The measured results are **negative** under the repo's own
> stated assumptions (see `docs/11-data-reconnaissance.md`,
> `data/entry_quality_summary.md`, `data/s0_verification_note.md`).
>
> **Work in progress on `feature/paired-exit-replay`.** A paired exit-policy
> replay has been built and reconciled against the archived S0 baseline:
>
> - [`docs/15-replay-results.md`](docs/15-replay-results.md) — the 30m results
> - [`docs/evidence/legacy-reconciliation.md`](docs/evidence/legacy-reconciliation.md) — why the archived baseline and the replay disagreed, and how it was settled
> - [`docs/14-redirect-plan.md`](docs/14-redirect-plan.md) — the frozen spec
>
> The headline: at **30-minute** resolution the pessimistic and optimistic
> bounds sit **0.43 R per setup apart** on the same data, so 30m cannot
> resolve this strategy's expectancy at all. The **1-minute** fetch is the
> decisive next step.
>
> The project is moving toward a separate observer/recovery service. This repo
> is preserved as the research archive — see
> [`archive/2026-11-11-rade-phase2-frozen/MANIFEST.md`](archive/2026-11-11-rade-phase2-frozen/MANIFEST.md).
> Restore the code with `git checkout preserve/phase2-frozen`.


## Quick links
- [Overview](docs/00-overview.md)
- [Requirements (SRS)](docs/01-requirements.md)
- [Architecture](docs/02-architecture.md)

## Run (Phase 1)
```bash
# activate venv
.venv/Scripts/activate

# fetch D1 bars
python scripts/run_fetch_bars.py

# backfill Telegram history (e.g. 5000 messages)
python scripts/run_backfill.py --limit 5000

# tests
python -m pytest tests/ -q -o pythonpath=src
```

## Run (paired exit replay)
```bash
# the reconciliation against the archived S0 baseline
python scripts/diagnose_legacy_divergence.py

# the replay, per split
python scripts/run_paired_replay.py --resolution 30m --split train --out data/replay_30m_train.json
python scripts/run_paired_replay.py --resolution 30m --split val   --out data/replay_30m_val.json
python scripts/run_paired_replay.py --resolution 30m --split test  --out data/replay_30m_test.json

# the optimistic bound (AC-7) and the legacy-matched configuration
python scripts/run_paired_replay.py --resolution 30m --split all --optimistic
python scripts/run_paired_replay.py --resolution 30m --split all --policies A --no-breakeven --pre-ladder-cap 15
```

## Quick links (docs)
- [Data model](docs/03-data-model.md)
- [Risk management](docs/04-risk-management.md)
- [Hedge probe spec](docs/05-hedge-probe-spec.md)
- [Telegram ingestion](docs/06-telegram-ingestion.md)
- [Backtesting](docs/07-backtesting.md)
- [Execution](docs/08-execution.md)
- [Operations](docs/09-operations.md)

## Non-goals (explicit)
- No scalping / no 1m/5m/10m candle trading.
- No fully autonomous live trading in Phase 1–5 (demo/paper only).
- No live hedging in v1 — hedging is simulated as a direction probe.

## Security
API keys live in `.env` (gitignored). Never commit keys. See `.env.example`.
The file `Heres the bybit api key ...txt` in the parent Documents folder contains
live credentials — rotate if it was ever shared or synced to a shared drive.
