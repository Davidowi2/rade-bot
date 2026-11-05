# 12 — Phase 2 design: from raw channel to a validated decision engine

Status: **proposed, awaiting lead approval.** Nothing in Phase 2 is built yet.

## 0. Why this phase exists

Phase 1 gave us the raw material: 15,027 messages, 2,396 parsed setups, 18,861
30m bars. Phase 2 turns that into a **decision engine we can measure**, before
a single order is placed.

The deliverable of Phase 2 is not "a bot that trades". It is:

> a labelled dataset of the trader's behaviour, a backtest harness that is
> proven not to cheat, a learned direction model with human-readable rules, a
> measured hedge probe, and a written rulebook — plus an honest verdict on
> whether this edge exists at all.

If the numbers say the edge is not there, that is a legitimate Phase 2 output
and we stop before spending money.

## 1. Scope (in / out)

**In**
- XAUUSD only, Tradelocker demo, D1 bias, 15h max hold (probe *and* trade).
- Deterministic labelling of the trader's setups from market data.
- Feature engineering on D1 + 1H context.
- Direction/quality model with walk-forward validation.
- Simulated hedge probe, measured for edge.
- Event-driven backtest with a real cost model.
- A written, human-readable rulebook.

**Out (later phases)**
- Any live order. Bybit/MT5. News filter. Multi-instrument. ML beyond
  gradient boosting. Anything on real money.

## 2. Sub-phases, deliverables, acceptance criteria

### 2A — Labeling engine  `src/rade/labelling/`
**What:** extract clean setups from raw messages → group into episodes →
resolve each against 30m bars under a limit-fill model.

Components:
- `setup_extractor.py` — strict grammar `XAUUSD (BUY|SELL) (NOW|LIMIT) <px>`
  + `Sl:` + `Tp:` lines. Reject anything else (14/2,396 = 0.6% currently).
- `episode_builder.py` — a setup opens an episode; result messages attach to
  the nearest open episode ≤48h; used only for cross-checking, never as truth.
- `recap_filter.py` — a setup whose entry was only reachable *before* the post
  is a historical re-post → excluded (477 = 20% of setups).
- `labeler.py` — limit-fill (24h touch window), 15h max hold, spread 0.25 on
  fill, TP ladder evaluated nearest-first.
- `ambiguity.py` — when SL and a TP are touched in the same bar: **base case =
  SL wins** (pessimistic), optimistic bound also recorded.

**Acceptance**
- Reproduces the reconnaissance numbers within tolerance: TP-first ≈ 57%,
  R median ≈ +0.59, resolved ≈ 2,013 of 2,382.
- Unit tests: geometry validation, recap exclusion, ambiguity handling,
  no-lookahead (labeler may only read bars ≥ post time).
- Output table `labeled_setups` with: entry, fill_px, fill_ts, outcome, exit_px,
  exit_ts, R, hours_held, ambiguity flag, features snapshot.

### 2B — Feature engineering  `src/rade/features/`
**What:** turn each labelled setup into a feature row describing the market
state *at the moment he posted*, so we can learn *what he was reacting to*.

Feature families (all computed from data available at post time only):
- **D1 bias** — EMA20/50 slope and stack, D1 close vs EMA20, RSI14.
- **D1 structure** — higher-high/higher-low sequence, distance to last D1 swing.
- **Volatility** — ATR14 (D1 and 1H) as % of price, D1 range percentile (252d).
- **Levels** — distance to prior D1/D1-week high/low, to round numbers
  (…00, …50), to the prior session's high/low.
- **Context** — UTC hour bucket, weekday, minutes since D1 open, spread.
- **Setup geometry** — his own |entry−SL| in ATR units, TP count, TP distance in R.

All distances normalized (ATR or % of price) — never absolute dollars, because
our feed ≠ his feed.

**Acceptance**
- Feature store table `features` keyed by (instrument, ts).
- Tests: no feature reads a bar with ts > post time (a "future leak" test that
  scans the feature code for unsafe rolling windows).

### 2C — Backtest harness  `src/rade/backtest/`
**What:** one engine, three strategies, identical cost model, so comparison is fair.

- Event-driven, bar-by-bar on 30m, no lookahead, deterministic (seeded).
- Cost model: spread 0.25 (observed) + configurable slippage + optional
  commission; positions sized by the risk model; 15h hard cap; daily/weekly
  limits and kill switch active in the backtest too.
- Strategies compared:
  - **S0 — Copy baseline**: take his setup verbatim (entry, SL, TP ladder),
    limit-fill. This is the benchmark his channel implies.
  - **S1 — Learned model**: our own entries from learned rules (2D).
  - **S2 — Model + probe**: learned rules, gated/filtered by the probe score.
- Metrics: expectancy in R, profit factor, hit rate, max DD, Sharpe, avg hold,
  per-year breakdown, per-regime breakdown, and the S0-vs-S1-vs-S2 delta.
- Validation: walk-forward (train/validate/test 60/20/20, rolling), plus a
  cost stress test at ±50% spread/slippage.

**Acceptance**
- Deterministic: same seed + same data → identical metrics (test).
- Leak test: shifting the label start by one bar changes results (test).
- Report table produced for S0/S1/S2 over ≥2 years.

### 2D — Direction model  `src/rade/strategy/`
**What:** learn *when he goes long vs short*, and *which of his setups are good*.

Targets (trained on the 2A labels):
- `direction` — BUY vs SELL (1,726 / 670 → imbalanced; use class weights).
- `quality` — TP-first vs SL-first (57% / 43%) → i.e. which setups to take.

Models: logistic regression / gradient boosting on the 2B features, with
walk-forward validation. **Plus** a shallow decision tree (depth ≤ 4) mined
purely for a human-readable rulebook — the lead wants rules he can inspect,
not a black box.

**Acceptance**
- OOS AUC ≥ 0.55 on direction (above 0.5 baseline) and ≥ 0.58 on quality.
- A written rulebook: 8–15 bullet rules, each with the measured lift it gives.
- Rules must be expressible without the model (fallback if the model is dropped).

### 2E — Hedge probe v1 (simulated)  `src/rade/probe/`
**What:** measure whether the simulated opposite-side limit probe actually
predicts direction. If it doesn't, it gets cut — that is a valid outcome.

Definition (concrete, in 05-hedge-probe-spec.md):
- At a candidate signal, place in **simulation** two resting limits:
  buy limit at `mid − k·ATR1H`, sell limit at `mid + k·ATR1H` (k default 0.5).
- Which side is touched first, and how long price holds beyond it, is the
  observation.
- **Sign convention (do not get this backwards):** a *sell* limit above the
  market fills when buyers cross the spread → buyers are aggressive → the
  touched-**sell** side = **bullish** pressure. A *buy* limit below fills when
  sellers hit the bid → sellers aggressive → touched-**buy** side = **bearish**.
- Window: 15h (the hard cap). Aggregation: score ∈ [−1, +1] from which side
  was touched and how long price held there.
- Probe never leaves a net position: pure simulation in Phase 2.

**Acceptance**
- Probe score vs forward outcome measured on the labelled set: AUC + edge table.
- Decision recorded either way: "probe has edge, weight = X" or "probe cut".
- Probe result becomes a feature consumed by 2C/S2.

### 2F — Rulebook + verdict  `docs/13-rulebook.md`
- The final human-readable trading rules (from 2D + 2E).
- The honest verdict: expected expectancy, DD, and the conditions under which
  the bot should not trade.
- The Phase 3 promotion checklist (paper/demo → live) with numbers.

**Acceptance:** lead signs off on the rulebook and the promotion checklist
before any execution code is written.

## 3. Phase 2 risk register

| Risk | Impact | Mitigation |
|---|---|---|
| Trader's own edge is too thin to copy | High | S0 baseline quantifies it early; success = beat S0, not match it |
| Label noise from 17.9% intra-bar ambiguity | Med | Pessimistic base case + optimistic bound; both reported |
| 20% of setups are recaps | Med | Excluded in 2A |
| Overfitting 2,396 samples | High | Walk-forward; shallow tree for rules; freeze params before OOS |
| Feed difference vs his broker | Med | Normalized features; $0.25 spread charged |
| Slippage on limit fills underestimated | Med | Cost stress ±50%; fill only when price *touches* level |
| Model drift as the trader's style changes | Med | Refit cadence defined in rulebook; recency weighting considered |
| Probe turns out to have no edge | Med | Explicitly allowed outcome in 2E; S2 then ≈ S1 |

## 4. Sequenced build order (each step independently verifiable)

1. `labelling/` + tests → produces `labeled_setups` (recon numbers reproduced)
2. `features/` + leak test → produces `features`
3. `backtest/` + determinism/leak tests → runs S0 on labelled set
4. `strategy/` model + tree → rules; backtest S1
5. `probe/` → probe score; backtest S2
6. `docs/13-rulebook.md` + verdict → lead review

## 5. Open decisions for the lead (needed before 2A)

1. **Learning objective** — do we ship S0 (copy his setups), S1 (learned model
   trades on its own), or both? Recommendation: build all three, ship whichever
   wins OOS, but require S1 to beat S0 by a stated margin.
2. **Our entry policy** — limit-first (like him) or market-on-signal? Limit
   first fills better but leaves some setups unfilled (15% in his data).
3. **Exit policy** — replicate his TP ladder with partial exits, or single
   target? Partial exits change R materially.
4. **Live-copy option** — should the bot also be able to trade his *live*
   posts in real time, or purely learn and act on its own?
5. **Success thresholds** — the numbers that promote Phase 2 → Phase 3
   (e.g. OOS expectancy ≥ +0.2R, PF ≥ 1.3, max DD ≤ 10%, ≥ 150 OOS trades).
6. **LLM in the loop?** — his posts contain narrative analysis ("gold testing
   3000 as support"). Extracting his *stated* levels/bias with an LLM could
   add features the numbers don't show. Worth a phase-2.5 spike?
