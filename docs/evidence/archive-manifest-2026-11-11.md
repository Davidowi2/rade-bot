# Archive manifest — Rade Bot, Phase 2 research infrastructure (frozen 2026-11-11)

> **This is the version-controlled copy of the archive manifest.** The archive
> folder itself (`archive/2026-11-11-rade-phase2-frozen/`) is gitignored
> because it contains a live Telegram session, so its `MANIFEST.md` is not
> tracked. This file is the authoritative record of what that archive holds.
> The two are kept identical by hand on 2026-11-11 — if they diverge, this
> file wins.

This is a **frozen snapshot** of the Rade Bot research repo, taken before the
project redirected to a new direction (Render + Supabase observer/recovery
service). It is preserved because the research asset inside it has real value
and the project may return to it.

## How the frozen state is protected

| Layer | Where | What it protects |
|---|---|---|
| Git tag | `rade-phase2-frozen-2026-11-11` | The exact code state at commit `f2121d9` (5,686 lines, 80 files) |
| Git branch | `preserve/phase2-frozen` | A named branch pointing at the same commit |
| This folder | `archive/2026-11-11-rade-phase2-frozen/` | The **gitignored** artifacts: `data/` research outputs, the Telegram session, and the env template |

`git checkout preserve/phase2-frozen` restores the code exactly.
`git checkout -b <new-work> rade-phase2-frozen` starts new work from it without
touching the frozen commit.

## Why this folder is not in git

`rade_session.session` is a live Telegram MTProto session — anyone holding it
can read the channel and the linked account. `archive/` is therefore listed in
`.gitignore`. This folder lives on the local/OneDrive disk only. **Do not
commit it, and do not share the folder.**

## Contents

```
.env.example            copy of the env template (no secrets — .env was NOT copied)
rade_session.session    Telegram MTProto session (SENSITIVE)
checksums.txt           sha256 of every file below, for integrity verification
data/
  rade.db               SQLite — the COMPLETE research database, 10 tables (75.6 MB)
  labeled_setups.csv    2,382 labelled setups (frozen cohort)
  features.csv          feature table, exported to CSV
  split_train_ids.json  1,198 setup ids  (2025-04-09 -> 2026-05-05)
  split_val_ids.json      388 setup ids  (2026-05-07 -> 2026-08-21)
  split_test_ids.json     404 setup ids  (2026-08-25 -> 2026-11-02)
  2d_report.json        direction + quality model OOS results
  s0_report.csv         S0 copy-baseline backtest results
  entry_analysis_report.json
  entry_quality_summary.md
  forward_returns.csv
  false_positive_review.csv
  shadow_study.csv      prospective shadow collection (11 rows, all un-matured)
  shadow_state.json
  shadow_study_review_log.csv
  tradelocker_matches.csv
  tradelocker_reconciliation.md
  s0_verification_note.md
  s0_split.json
```

### `data/rade.db` — full table inventory

The database is **complete**. It is not a bars-only file. Verified against both
the live and the archived copy on 2026-11-11:

| table | rows | contents |
|---|---|---|
| `raw_telegram_messages` | **15,027** | the raw Telegram corpus, immutable as ingested |
| `signals` | 2,396 | loose-parser output (`ingestion/telegram/parser.py`) |
| `labeled_setups` | 2,382 | strict-parser output (`labelling/setup_extractor.py`) |
| `features` | 71,460 | long-format feature rows (`message_pk` × `feature_name` × `value`) |
| `bars` | 31,323 | XAUUSD bars: 1D (2,991) · 1H (9,460) · 30m (18,872) |
| `instruments` | 1 | XAUUSD |
| `orders` | 0 | schema only — no execution code was ever built |
| `positions` | 0 | schema only |
| `probes` | 0 | schema only |
| `pnl_daily` | 0 | schema only |

**The raw Telegram corpus IS preserved.** There is no need to re-run
`scripts/run_backfill.py`. The empty tables are the schema-only stubs for the
`execution/`, `probe/` and `risk/` modules that were never implemented.

> **Correction note (2026-11-11).** An earlier version of this manifest
> claimed the database contained only the `bars` table, and that the
> 15,027-message corpus was absent and would need re-pulling. That was wrong.
> It came from a database inspection query that reused one cursor for both the
> table enumeration and the row counts, so the inner query reset the iteration
> and only the first table was ever read. **The archive itself was always a
> faithful byte copy; only this description was wrong.**

## Verify integrity

```bash
cd archive/2026-11-11-rade-phase2-frozen
sha256sum -c checksums.txt
```

## Restore

```bash
# code
git checkout preserve/phase2-frozen

# data (overwrites current data/ - move it aside first if it has new work)
cp -r archive/2026-11-11-rade-phase2-frozen/data/. data/
```

The archived database carries the full research corpus (see the table inventory
above). The CSVs in `data/` are exports of the same tables, kept flat so the
archive is readable without SQLite.

## Known state at freeze time

- 27 pytest tests passing.
- `scripts/run_backtest_s0.py` ends with a stray `PY` token (line 78) ->
  NameError at exit. Use `scripts/run_s0_report.py` for canonical S0 numbers.
- `scipy` is imported by `scripts/run_entry_analysis.py` but is missing from
  `requirements.txt`.
- The shadow collector had stalled: `data/shadow_state.json` shows
  `last_bar_ts = 2026-11-05`, 11 rows collected, all `matured=False`, all
  `review_status=pending_original_unknown` (which excludes all 11 from
  primary analysis under the frozen shadow spec).
- Market data in `rade.db` ends **2026-11-03** (1D/1H/30m).
- `src/rade/probe/`, `risk/`, `execution/`, `ops/` are 0-byte stubs.
- The backtest engine's per-trade R **omits the entry spread** — see
  `docs/14-redirect-plan.md` §4.3.1, acceptance criterion AC-3.
