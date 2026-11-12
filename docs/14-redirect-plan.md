# 14 — Redirect plan: exit-policy freeze + observer/recovery foundation

Status: **lead decisions incorporated. Ready to build on approval.**
Date: 2026-11-11

Answers the narrow planning question raised at handoff: *the exact Policy B
trailing rule, and whether TP2 moves the stop to TP1*. Both are now settled
(§2.2, §2.3). Extends `docs/12-phase2-design.md`.

---

## 0. Where this repo now sits

Frozen as the **Phase 2 research archive**:

| Protection | Handle |
|---|---|
| Git tag | `rade-phase2-frozen-2026-11-11` → commit `f2121d9` (the original 5,686-line state) |
| Git branch | `preserve/phase2-frozen` → same commit |
| Artifact archive | `archive/2026-11-11-rade-phase2-frozen/` (gitignored — contains a live Telegram session) |

`main` is `f2121d9` **plus three metadata-only changes** — `.gitignore` (the
archive rule), `README.md` (the status line), and `docs/14-redirect-plan.md`
(this document). The research source is byte-identical to the freeze tag:
`git diff rade-phase2-frozen-2026-11-11 HEAD --stat` shows exactly those
three files and nothing else. `main` is **not** a metadata-only branch — it
still carries the whole research source, unchanged.

The branches and artefacts named in §1 were created on 2026-11-11:
`feature/paired-exit-replay` (branched from `main`, which carries the
research source byte-identical to the freeze tag plus the metadata files) and
`cohort/v1/` — the frozen cohort, the split id files, and a `MANIFEST.json`
carrying the SHA256 of every exported file. The observer repo pins
`cohort/v1` by that manifest; it never regenerates it.

---

## 1. Repo topology — **decided: new sibling repo**

```
rade-bot/            (this repo, frozen)
  └─ branch feature/paired-exit-replay   <- task 4: paired exit-policy replay
     uses the existing backtest engine + labelled cohort

rade-observer/       (new sibling repo)
  └─ the Render service: Telegram listener, Supabase writer, health endpoint
     imports the frozen cohort as a read-only, versioned artifact
```

**The contract between them.** `rade-observer` never imports `rade-bot` code.
It receives the cohort as a data file:

```
cohort/v1/labeled_setups.csv   +  cohort/v1/MANIFEST.json
```

`MANIFEST.json` carries the cohort definition, the split ids, the SHA256 of
every file, the assumption set, and the git commit it was produced from. The
observer pins a cohort version; it never regenerates the cohort. If the
research repo re-derives a cohort, that is a new `v2` with a new manifest, and
the observer's pinned version is deliberately left alone.

**Exported 2026-11-11** by `scripts/export_cohort.py --version v1`:
1,972 cohort rows; 1,173 / 383 / 387 in the train / val / test splits;
1,889 with ≥2 TPs, 1,211 with ≥3.

**Why not one repo.** The observer service has a completely different
dependency surface (Render, Supabase, Telethon-as-a-service, HTTP health
checks) and a different failure domain than a backtest. Mixing them means the
research repo's test suite starts depending on a network. The contract file
keeps the coupling auditable without the coupling.

---

## 2. The exit-policy decision

### 2.1 Two policies, one variable

Both arms run on **identical** entries, identical initial stops, identical
sizing, identical price path, identical costs. They differ in exactly one
place: what happens to the **final tranche** after the trader's TP ladder is
exhausted.

| | Policy A | Policy B |
|---|---|---|
| TP1 leg | close 1/n | close 1/n |
| Stop after TP1 | → cost-adjusted breakeven | → cost-adjusted breakeven |
| TP2 leg | close 1/n | close 1/n |
| Stop after TP2 | **no change** | **no change** |
| Final TP leg (FTL) | close the **entire remaining** position | close **half** the remaining, trail the other half |

### 2.2 Decision: Policy B trailing rule — **1R behind the favourable peak**

**Definition.** For a setup with initial risk `R = |entry − published stop|`:

```
peak          = best favourable price reached since fill
               long  -> highest price traded (bar high)
               short -> lowest price traded  (bar low)

trail_stop    = peak − 1R   (long)      peak + 1R   (short)

effective_stop = max( ratcheted_stop, trail_stop )      (long)
                 min( ratcheted_stop, trail_stop )      (short)
```

The trailing stop is **activated** when the final TP level is touched — the
runner exists from that moment. Before then the stop sits at cost-adjusted
breakeven.

Properties, each of which matters:

- **The stop may tighten, but never move backward.** The ratchet is explicit:
  a newly computed trail stop replaces the old one *only* if it is tighter.
- **Best favourable price, not a mid.** For a long the peak is the highest
  price actually traded; for a short the lowest. The trail is measured against
  what was achievable, not a theoretical price.
- **Cost-adjusted breakeven floor.** `BE = entry ± (spread + slippage)` =
  `entry ± 0.50`. A raw-entry stop would quietly bleed ~$0.50/unit after
  costs. The floor is *set* to cover the modelled round-trip cost, so a
  runner stopped at the floor is approximately **0R under the frozen cost
  model**. That is an assumption about the model — it depends on the assumed
  spread and slippage and on the stop filling at its level — not a guarantee
  that the runner cannot lose. The replay reports realised cost drift
  alongside every result so the assumption is visible rather than buried.
- **Zero free parameters** beyond the multiplier `1.0`.
- **Ambiguity is marked, never assumed favourable.** When a stop and a target
  are touched inside the same bar, the stop wins (pessimistic). This is the
  repo's existing convention and it is preserved at every resolution.

**1R, not ATR.** Measured on this repo's own data:

| Quantity | Value |
|---|---|
| ATR14, 1H bars (median) | $15.65 |
| ATR14, 30m bars (median) | $10.66 |
| ATR14, D1 bars (median) | $23.16 |
| Trader's own risk distance (entry→SL, median) | **$9.00** |
| Trader's risk ÷ ATR1H | **0.575** |

The trader's own stop distance is 0.575 × ATR1H. A trail of `1R` is *the same
number as* `0.575 × ATR1H` — it is his own geometry, expressed back to him.
Expressing it as 1R is better because it needs no timeframe choice, no ATR
plumbing and no feed-dependent constant, and because it stays correct for
outliers (this cohort's *mean* risk is $44.48 because of a few absurd stops —
the median $9 is the honest statistic, and R-normalisation handles both).

**1R is a baseline, not a claim of optimality.** It is pre-registered as the
primary rule. A variant comparison (0.5R / 1R / 1.5R) may be run, but
**selection happens on the validation split**, never on train — see §3.2. The
holdout is then touched once for the final evaluation.

### 2.3 Decision: does TP2 move the stop to TP1? — **No**

Reasons:

1. **It contaminates the comparison.** Policy B exists to test whether a
   runner earns its keep. Moving the stop to TP1 truncates the runner — the
   exact thing under test. Applied to both arms it destroys the contrast at
   the tail; applied to one arm it stops being a single-variable A/B.
2. **Breakeven already covers the intent.** The stated purpose of a TP2 stop
   move is capital protection. TP1 → cost-adjusted breakeven already does
   that, and does it better — breakeven is the number that actually matters,
   because TP1 is often closer than TP2 and the trade should stop risking
   capital after the first leg.
3. **It stays testable.** It becomes a **third arm (C)**: both policies with
   TP2 → stop-at-TP1. If it is ever adopted it must beat both A and B on
   **validation**, with the holdout reserved for one final evaluation. No
   change to the A/B comparison.

So the frozen stop ladder, **identical for both policies**:

```
on TP1 fill  ->  stop := cost-adjusted breakeven
on TP2 fill  ->  stop := unchanged (still breakeven)
on FTL fill  ->  ARM A: close everything
                ARM B: close half the remaining, trail the other half from here
```

### 2.4 Three gaps in the original handoff framing

Found while grounding the decision in the data. Each has a resolution folded
into the frozen spec.

**Gap 1 — "TP3" is undefined for 38.6% of the cohort.**

The ladder length varies. Of the 1,855 cohort setups with ≥2 TPs:

| TP count | 1 | 2 | 3 | 4 | 5 | 6 | 7 |
|---|---|---|---|---|---|---|---|
| setups | 83 | 678 | **1,032** | 148 | 24 | 1 | 6 |

Only 1,032 have exactly three. "Close at TP3" is not defined for the 678
two-TP setups (or the 83 one-TP setups).

**Resolution — generalise to FTL (final TP in the ladder).** FTL reduces to
"TP3" for the modal case and is defined for every setup with ≥2 TPs. The
policy statement becomes *"when the ladder is exhausted, close everything vs.
keep a runner"*, which is what the question actually means. The
TP3-restricted subset is reported as a sensitivity.

**Related landmine — CONFIRMED, and it was a live defect.** `setup_extractor.py`
preserves raw message order for TPs, and `backtest/engine.py` consumes
`take_profits` **verbatim** — while `labelling/labeler.py` normalises
nearest-first. Measured on the cohort: **10 setups have TPs in non-nearest-first
order, and 17 have a TP on the wrong side of entry.**

This was not a hypothetical. The legacy engine filled an unreachable target
**instantly**, at that target's price. On `pk=452` the raw ladder was
`[2367.0, 3380.0, 3400.0]` with an entry of 3360.0 — the first target sat
**$993 below the entry** — and the setup alone produced **−37.46 R**. Across
the cohort the defect moved the baseline by **+0.0378 R per setup**, 97% of
it carried by 15 setups. The full arithmetic is in
`docs/evidence/legacy-reconciliation.md`.

**Resolution — the replay normalises the ladder** (drop wrong-side TPs, sort
nearest-first) and records how many were altered (22 on the filled cohort: 12
wrong-side, 10 reordered).

**Gap 2 — sizing is not currently paired.**

`engine.py` sizes each setup off *current* equity (`risk_pct × equity / risk_price`),
and equity changes as the backtest runs. Arms A and B would therefore take
**different position sizes on later setups**, contaminating the comparison
with a sizing effect.

**Resolution — freeze sizing.** Every setup in both arms is sized off a
**constant reference equity** (the initial $100,000), at 0.5% risk. Both arms
hold byte-identical positions on byte-identical entries; the only difference in
the results is the exit.

**Gap 3 — Policy B's runner is unbounded; Policy A is not.**

A ends when the ladder ends. B keeps running. Comparing an unbounded arm to a
bounded one is not a fair comparison.

**Resolution — bound the runner.** The runner closes at the earliest of: the
trailing stop, **48h from fill**, or the end of available data. The pre-ladder
phase gets a **24h hard cap**.

On the archived 30m cohort the 24h cap binds on **24 setups** (of 1,969) and
the result moves by less than 0.009 R across a 12× range of cap lengths (6h
→ 72h). The cap length is **not** what drives the result.

> **Correction (2026-11-11).** An earlier draft of this section said the 24h
> cap *"provably non-binding"*. That was an overclaim: it binds 24 times on
> the 30m cohort, and the binding count is now a reported output per AC-8
> rather than an assumption. The measured sweep is in
> `docs/15-replay-results.md` §4.1.

> **Correction (2026-11-11).** This section previously claimed the maximum
> observed hold was 9.5h. That figure came from the *labeller's* resolution
> and predates the replay. The replay's own maximum hold on 30m is longer,
> because the caps fire at the first bar at or after the boundary — and the
> 30m series has 73 weekend gaps of ~49.5h, so a Friday-evening fill sees no
> bar at all until Sunday.

**Window-end handling.** Any position still open when data ends is closed at
the final bar's close with `exit_reason = window_end`, and the count is
reported per arm. It is never silently dropped.

> **Correction (2026-11-11).** The first implementation stopped scanning at
> the last bar *before* the cap boundary. Across a weekend gap that closed a
> Friday fill at Friday's stale close and mislabelled it `window_end`. A cap
> is a wall-clock rule, so it now binds at the **first bar at or after** the
> boundary, and `window_end` means the end of available data. Fixed and
> pinned by `tests/test_replay_reconciliation.py`.

---

## 3. Data resolution — 1-minute primary, 30-minute as sensitivity

The replay must resolve stop/target ordering at the finest available
granularity, and ambiguity must be **marked**, never resolved favourably.

**Evidence is saved, not asserted.** The capability probe was run against the
TradeLocker **demo** account (read-only, no orders) and its output committed:

- `docs/evidence/1m-availability.md` — human-readable record
- `docs/evidence/1m-availability.json` — raw probe record
- `scripts/probe_1m_availability.py` — the script, re-runnable

| Probe | Result |
|---|---|
| 1m resolution available | **Yes** — 13,772 rows for a 14-day window |
| 1m bar step | 1 minute, no gaps within the probed windows |
| Request speed | ~2.5–5s per request |
| Row cap | **Enforced at 30,000** — a 21-day 1m request raises `ValueError: No. of requested rows (30240) larger than max allowed (30000)` |
| 14-day chunk | Works — 13,772 rows, ~4.4s |
| 21-day chunk | **Fails** (30,240 rows) — confirms 14 days as the chunk size |
| Instrument | XAUUSD id `1808`, server PLEXY, demo `https://demo.tradelocker.com` |

**Fetch plan.** 2025-04-01 → 2026-11-03 in **14-day chunks** (≈42 requests,
≈4 minutes, max ~13.8k rows per request). Chunk windows do not align exactly
to the API's returned span, so chunks are **upserted on `ts`** and the merged
series is **verified for gaps > 1 minute** before use. Any setup whose
evaluation window contains an unresolved gap is excluded and counted.

### 3.1 What changes on 1m, and what does not

- **Cohort membership does not change.** Inclusion (parsed, valid, non-recap,
  filled) is a property of the message and the market, not of bar resolution.
  The cohort stays the one frozen in `cohort/v1`.
- **Fills and exits are re-derived on 1m inside the replay.** The fill anchor
  becomes precise. The 17.3% intra-bar ambiguity measured on 30m is expected
  to fall — but the fall is **measured and reported**, not assumed, because
  that number quantifies how much of the archived S0 ambiguity was a 30-minute
  artefact rather than a property of the setups.
- **30m stays as a reported sensitivity**, so the replay remains comparable
  with the archived S0 numbers in `data/s0_report.csv`. If 1m and 30m agree
  materially, the archived results stand; if they disagree, the 1m result is
  the one that counts and the discrepancy gets reported.

**Feature rows are not recomputed.** Features are D1/1H-context features at
post time; resolution does not change them, and the archived `features.csv`
stays valid for the cohort.

### 3.2 Intra-bar event ordering — an explicit modelling assumption

**A 1-minute touch is not proof of a fill, and a 1-minute bar does not reveal
the order of events inside it.** A bar carries OHLC only — no path. The
replay therefore states its ordering rules as assumptions and reports how
often each one binds. These are the rules:

**Fill.** The first 1m bar whose range brackets the limit level
(`low ≤ entry ≤ high`) is a fill, at the limit price, with zero slippage on
entry (the entry spread is charged separately as a cost, per §4.3). If the
bar's **open** is already at or beyond the level, the limit would have filled
at the open rather than at the limit — the bar brackets the level without the
touch being visible. The replay still fills at the limit price (the
pessimistic assumption), flags `open_gap=True`, and reports the count.

**Stop vs target inside one bar.** If the stop and any target are both touched
within the same 1m bar, the order is unknowable. **The stop wins.** This
extends the repo's existing 30m convention down to 1m and is the pessimistic
reading. Every such bar is flagged `ambiguous=True`, the optimistic bound is
computed alongside, and both are reported.

**Entry and stop inside the same bar.** If the fill bar also touches the
stop, the pessimistic reading is fill-then-stop: R = −1 for that setup.
Flagged `entry_bar_stop=True` and counted. (The optimistic bound is that the
fill never happened, so the setup is excluded.)

**Entry and target inside the same bar.** Pessimistic reading is fill, then
stop if the stop was also touched, else the nearest target. Consistent with
the stop-first rule above.

**One target per bar.** A bar that gapped through several targets records only
the nearest one; the next target waits for a later bar. This matches the
existing engine's behaviour at 30m.

**Reporting.** The replay publishes, as headline output: the number of 1m bars
where the stop and a target were both touched; the number of fill bars that
also touched the stop; the number of open-gap fills; and the total R impact
of the optimistic bound. If the ambiguity rate is still material at 1m, that
is a finding about the data, not something to tune away.

### 3.3 What 1m does *not* fix

Finer bars shrink but never eliminate the ambiguity: they replace a 30-minute
guess about path with a 1-minute guess about path. The honest claim is that
1m **reduces** the ambiguity rate by a measured factor, not that it removes
it. Any conclusion that depends on the ordering inside a bar is reported with
the ambiguous cases listed, and the optimistic bound is always published next
to the pessimistic base case.

---

## 4. The frozen replay protocol

Written down **before** running, so the holdout can be touched once.

### 4.1 Cohort

```
2,382  labelled setups (all)
  − 410  recaps / unfilled / invalid
  ──────
1,972  filled & non-recap
  −  29  no split assignment (purged at the boundaries)
  ──────
1,943  in the purged train/val/test split
        of which 1,855 have ≥2 TPs  (policy-relevant)
        of which 1,340 reach the final target PRICE within 48h of fill
        of which   282 still had a position open when they got there
```

> **Correction (2026-11-11).** The FTL figure was previously quoted as 1,235
> (631 / 296 / 308). That came from an ad-hoc calculation that was never
> committed as code and cannot be reproduced by any of the four definitions
> tested. The replay now computes it explicitly and publishes the definition:
> **the final normalised ladder level is touched at any point inside 48h of
> fill, independent of the exit policy** — 701 / 297 / 317 over the split ids
> (1,315), or 1,340 over the whole filled cohort. The replay's figure
> supersedes the earlier estimate.

> **Correction (2026-11-12) — that definition counts a price, not a trade.**
> Touching the final target's *price* does not mean the position was still
> open: 1,340 setups reach the level, but only **282** have a live position at
> that moment (163 / 38 / 73 over the splits). The rest had already stopped
> out or hit the pre-ladder cap. The first figure is an upper bound on where a
> runner could have mattered; the second is the **runner-eligible** population.
> Both are now reported separately, and the subgroup is called "final target
> touched" rather than "FTL-reaching" to stop the two being confused.

> **Correction (2026-11-11).** The replay fills 1,969 of the 1,972, not
> 1,972. Three setups (`pk=9730/9731/9732`, three copies of one post whose
> *entire* ladder sits below the entry) have no reachable target at all and
> are marked **invalid** rather than given an invented exit.

### 4.2 Splits — reuse, do not rebuild; and the selection rule

The existing purged split is reused verbatim (`data/split_{train,val,test}_ids.json`):

| Split | ids | filled & non-recap | date range | role |
|---|---|---|---|---|
| train | 1,198 | 1,173 | 2025-04-09 → 2026-05-05 | **development** |
| val | 388 | 383 | 2026-05-07 → 2026-08-21 | **selection** |
| test | 404 | 387 | 2026-08-25 → 2026-11-02 | **holdout — one final evaluation** |

The 39h purge (24h fill window + 15h hold) is already baked in. Gaps between
splits: 2 days train→val, 4 days val→test — both exceed the purge.

**The selection rule, stated precisely** (this is the correction to the
original draft, which proposed sweeping on train):

- **Train is for development only.** Build, debug, sanity-check, and fix
  mechanical bugs on train. Nothing is *chosen* on train, because choosing on
  train is selecting in-sample.
- **Validation is where selection happens.** The 1R baseline is the
  pre-registered primary. If the 0.5R / 1R / 1.5R comparison is run, the
  winner is selected on **validation**. Arm C (TP2 → TP1) is likewise adopted
  only if it beats both A and B on **validation**.
- **The holdout is reserved for one final evaluation** of whatever was frozen
  after validation. It is touched once, after the policy is frozen, and
  nothing is tuned afterwards.

### 4.3 Frozen common assumptions

| Item | Value | Note |
|---|---|---|
| Bar resolution | **1 minute** primary; 30m as reported sensitivity | §3 |
| Entry | limit touch at the stated price | unchanged from S0 |
| Fill window | 24h from `posted_at` | unchanged from S0 |
| TP ladder | normalised: drop wrong-side, sort nearest-first | **new** — Gap 1 |
| Sizing | 0.5% of **constant** $100,000 reference equity | **new** — Gap 2 |
| Spread | $0.25 charged on entry | observed |
| Entry cost in R | charged **once per setup**, and **included** in setup net P&L and R | **new** — see §4.3.1 |
| Slippage | $0.25 on market exits (SL, timeout, forced) | observed base case |
| Limit exits (TP fills) | free | existing `CostModel` |
| Commission | none | absent from the whole repo |
| Ambiguity (stop and target in the same bar) | **stop wins — marked, never favourable** | repo convention, at every resolution |
| Stop after TP1 | → cost-adjusted breakeven (`entry ± 0.50`) | identical in both arms |
| Stop after TP2 | no change | identical in both arms |
| Trail rule | 1R behind favourable peak, ratcheted, never backward | §2.2 |
| Runner size at FTL | half the remaining tranche trails; half closes | §2.1 |
| Max hold, pre-ladder | 24h hard cap | binds 24 setups on 30m; count reported per AC-8 |
| Max hold, runner | 48h hard cap | binds 0 setups on 30m; count reported per AC-8 |
| Seed | 42, deterministic | |

### 4.3.1 The entry-cost defect in the current engine — must be fixed

The existing engine has a real defect that inflates reported expectancy.

In `src/rade/backtest/engine.py` the entry cost is applied **once, to the
account equity only**:

```python
# line ~147, at fill
equity -= self.cost_model.entry_cost(size)
```

It is **not** applied to any leg's P&L. Every leg then computes:

```python
pnl = close_size * (tp_hit - entry_price)          # or the SL variant
pnl -= self.cost_model.market_exit_cost(close_size)  # 0 for limit exits
R   = pnl / pos.risk_dollars
```

So the **entry spread never reaches R**, at leg level or at setup level
(`walkforward.aggregate_by_setup` sums those same leg P&Ls). The equity curve
pays it; the R metric does not.

**Magnitude.** Entry cost as a fraction of one R is `spread / risk_price`.
With the cohort's median risk distance of $9.00 and a $0.25 spread that is
**0.028 R per setup** — charged once, against a mean per-setup R in the
low tens of hundredths. It is not a rounding error; it can flip the sign of
the headline expectancy.

**Resolution.** The replay charges the entry cost **once per setup** and
includes it in setup-level net P&L and setup-level R:

```
setup_net_pnl = Σ leg_pnl  −  entry_cost − Σ market_exit_costs
setup_R       = setup_net_pnl / risk_dollars
```

Per-leg R is reported too, with the entry cost **allocated** across legs
pro-rata to size purely for presentation; the setup total is the
authoritative number and is never the sum of rounded leg figures.

**Regression test.** A setup that fills, immediately hits its stop, and has no
other events must report exactly **−1.00 R minus the entry cost** — i.e.
strictly less than −1.00 R. A replay that returns exactly −1.00 R on that
case has reintroduced the defect.

### 4.4 Metrics — per setup, not per leg

The current `metrics.py` reports **per-leg** figures, and the S0 verification
note already flagged that "56 trades" was really ~14 setups × ~4 legs. All
paired reporting is **per setup**:

- net R per setup (total P&L ÷ initial risk, one R per setup, not per leg)
- expectancy in R, profit factor, hit rate
- **sampling uncertainty of each arm's mean** — SD, SE and a 95% interval.
  This is required: a negative mean with an interval spanning zero is not
  evidence of negative expectancy, and must not be reported as if it were.
- max drawdown on the per-setup equity curve — **an ordering artefact, not a
  portfolio drawdown**. It sorts completed setups by fill time and accumulates
  their R; it does not use exit-time cash flows or mark open positions to
  market. It compares arms fairly because every arm trades the same setups. It
  must never be used to size or approve an account.
- average/median hold time, per arm
- exit-reason mix (`tp1`, `tp2`, `ftl`, `trail_stop`, `sl`, `time_cap`, `window_end`)
- ambiguous-bar counts, and the optimistic-bound delta
- **paired difference** (A vs B) on the runner-eligible subset, with CI

**Four populations are reported, always together.** The full cohort, the
final-target-touched subset, the runner-eligible subset and the differing
subset answer different questions and none substitutes for another.

| Population | n (train/val/test, 1m) | Answers |
|---|---|---|
| **Full cohort** | 1,172 / 380 / 382 | overall strategy performance — expectancy, PF, drawdown |
| **Final target touched** (final ladder level traded within 48h of fill) | 692 / 295 / 299 | did price ever reach the final target? **Upper bound only** — the position may already be closed |
| **Runner-eligible** (a final-target leg filled while the position was live) | **208 / 55 / 101** | did a runner actually exist? |

*(Runner-eligible counts are for Policy A. C and CB give 197 / 54 / 86: their
TP2→TP1 stop fires before the final target, so it removes some setups that A
and B carry to the end. That is one reason C is not a runner variant.)*
| **Differing subset** (arms differ) | 208 / 55 / 101 | does the runner change the outcome? |

**A structural identity worth knowing.** For A vs B the differing subset is
*exactly* the runner-eligible subset, per split — 208 / 55 / 101 at 1m. That
is by construction: A and B are identical until the final target, so they can
only diverge where a final target was reached with the position live. It makes
A vs B the one comparison that genuinely tests the runner thesis, and it is a
useful assertion to check on any future run.

**Correction to an earlier claim in this plan.** A previous draft said the
holdout had "308 paired setups — that's real power". That was wrong on two
counts:

1. **308 was the count of setups that *reach* the FTL *price*, not the count
   where the arms actually differ.** Reaching the final target level is not
   the same as the ladder completing — a trade may already have been stopped
   out before the final TP is reached. The arms actually differ on ~1/3 of
   them.
2. **"Real power" was asserted without a variance basis.**

**The paired-variance basis.** A planning estimate was run on the archived 30m
cohort to size the test — *not* the replay, and **no selection was performed
on it**. Results, per setup, including the entry cost:

| Split | n | arms differ | paired diff (B−A) | paired SD | SE | MDE @80% power |
|---|---|---|---|---|---|---|
| train | 1,173 | 210 | **+0.0228** [+0.0111, +0.0345] | 0.2036 | 0.0059 | 0.0166 R |
| val | 383 | 78 | **+0.0006** [−0.0101, +0.0112] | 0.1058 | 0.0054 | 0.0152 R |
| test | 387 | 114 | **+0.0001** [−0.0149, +0.0151] | 0.1508 | 0.0077 | 0.0215 R |

Read honestly: **the train effect does not persist.** +0.0228 R on train
collapses to +0.0006 R on validation. That is the same train→validation decay
the 2D direction model showed (AUC 0.70 → 0.64 → 0.60) and the same pattern
the entry-quality study showed (every significant train pattern flipped on
validation). The prior should be that the runner's edge is near zero.

> **Correction (2026-11-11).** This planning estimate was computed with a
> reader that treated the cohort's naive-UTC timestamps as local time, and it
> predates the ladder normalisation. The replay's own numbers supersede it:
> the A→B difference is **+0.0252 / +0.0183 / +0.0211** on train / val / test,
> all with CIs excluding zero, and it holds under both the pessimistic base
> case and the optimistic bound. The estimate is kept here for the record and
> is superseded by `docs/15-replay-results.md` §3.

**Stated effect size.** The design detects:

- **≈0.022 R** over the full split (73 differing setups in the holdout)
- **≈0.075 R** restricted to the differing subset alone

So a runner edge smaller than roughly **0.05 R per setup** is not detectable
by this cohort at the holdout's size. If the true effect is of that order,
the correct conclusion is "not measurable here", not "small but positive".

The 30m numbers above are a **planning estimate from a simplified simulator**.
They are not the replay, they are not the 1m result, and they are not used to
select anything. The authoritative paired SD comes from the 1m **validation**
run before the holdout is touched.

### 4.5 Decision rule (pre-registered)

1. **Train**: build and debug. No selection.
2. **Validation**: confirm the pre-registered 1R baseline. If the variant
   comparison is run, select on validation. Adopt arm C only if it beats both
   A and B on validation. If the baseline does not hold on validation, report
   the negative and stop.
3. **Freeze** the policy.
4. **Holdout**: touched **once** to report the final paired numbers. No tuning
   after.
5. If B wins on the retrospective cohort, that cohort is now **selection
   data** — it must be confirmed by a prospective demo period before any live
   mirroring. This mirrors the constraint already recorded in ADR-0003.
6. Any result is publishable, including "no difference" and "B is worse". A
   negative result stops the project before money is spent, which is the
   stated purpose of Phase 2.

### 4.6 Replay acceptance criteria

The replay is accepted only when **every** row below passes. Each is
independently checkable; none is satisfied by assertion.

| # | Criterion | How it is verified |
|---|---|---|
| AC-1 | Cohort identity | Row count and id set match `cohort/v1` exactly; the 29 unassigned setups are excluded and named; no silent additions or drops |
| AC-2 | Split integrity | Train/val/test id sets are byte-identical to the archived `split_*.json`; the 39h purge is present at both boundaries; **the holdout is opened once** and the opening is logged with a timestamp |
| AC-3 | **Entry cost is inside R** | Regression test: a setup that fills and immediately stops reports **strictly less than −1.00 R**. A result of exactly −1.00 R fails the build (§4.3.1) |
| AC-4 | Sizing is frozen | Both arms hold byte-identical sizes on identical entries; size derives from a constant $100,000 reference equity, never from the running equity curve (§2.4 Gap 2) |
| AC-5 | TP ladder normalised | Every setup's ladder is nearest-first with wrong-side targets dropped; the count altered is reported (25 raw-order / 15 wrong-side on the archived cohort) |
| AC-6 | Intra-bar rules implemented | Fill rule, stop-before-target, one-target-per-bar, and open-gap fill all implemented per §3.2; each is unit-tested against a hand-built bar |
| AC-7 | Ambiguity reported, not tuned away | Published: bars where stop and target were both touched; fill bars that also touched the stop; open-gap fills; and the total R impact of the optimistic bound |
| AC-8 | **Caps are reported as binding counts** | Number of setups where the 24h pre-ladder cap and the 48h runner cap actually bind, per arm. "None" is a result to verify, not an assumption (§2.4 Gap 3) |
| AC-9 | Four populations reported | Full cohort, final-target-touched, runner-eligible and differing subsets, side by side, for every metric — never one without the others (§4.4). "Final target touched" must never be described as "the position was still open" |
| AC-9b | Sampling uncertainty reported | Every arm's mean is published with SD, SE and a 95% interval, and a negative point estimate with an interval spanning zero is reported as **not resolved**, never as a measured loss |
| AC-9c | Drawdown labelled honestly | The drawdown figure is named as a closed-trade ordering artefact and explicitly not a portfolio drawdown (§4.4) |
| AC-10 | Effect size stated | The paired SD and MDE are recomputed from the 1m validation run and published **before** the holdout is opened; the conclusion is stated relative to that MDE ("below the detectable threshold", not "small but positive") |
| AC-11 | Selection discipline | The 1R baseline and arm C selection are decided on **validation only**; no parameter is chosen on train; no parameter is changed after the holdout is opened |
| AC-12 | Determinism | Same seed + same data → byte-identical metrics across two runs; asserted in a test |
| AC-13 | No lookahead | A test asserts no bar with `ts` greater than the evaluation timestamp is read; plus the "shift the label start by one bar, results must change" test from `docs/12-phase2-design.md` §2C |
| AC-14 | 30m sensitivity reported | The same replay on 30m is reported next to the 1m result, with the difference quantified against the archived S0 numbers |
| AC-15 | Provenance | Every input file referenced by commit + SHA256; the run is reproducible from the frozen tag alone |
| AC-16 | **No broker orders** | The replay is a pure offline computation. It makes no network call to the broker at run time. (The 1m fetch is a separate, read-only, pre-authorised step.) |
| AC-17 | **Reconciled against the archived baseline** | The replay reproduces `data/s0_report.csv` wherever the two are meant to agree, and every disagreement is attributed to a named cause. Asserted in `tests/test_replay_reconciliation.py` (1,961 / 1,969 setups identical to 1e-9; 8 named stop-vs-cap ties). See `docs/evidence/legacy-reconciliation.md` |
| AC-18 | **Timestamps are UTC, explicitly** | The cohort export stores naive-UTC timestamps; every reader must treat naive as UTC and never as local time. Asserted against the database and by a unit test on the parser |
| AC-19 | **Caps bind at the first bar at or after the boundary** | A wall-clock cap must not be truncated by a data gap. `window_end` means the end of available data and is closed at the final bar's close. Asserted by tests that place a weekend gap across a cap boundary |
| AC-20 | **Unreachable ladders are invalid, not invented** | A setup whose entire ladder sits on the wrong side of the entry is marked `invalid` and counted. It is never given an invented exit. (3 setups on the archived cohort) |

---

## 5. The observer/recovery foundation (new repo)

Tasks 1–3 of the handoff, scoped. **Full deployment on the free tier** — the
complete stack and runbook, with the reliability caveats stated honestly
rather than engineered around.

### 5.1 Task 1 — feasibility probe (read-only) **plus one separately-authorised demo order**

| Probe | Method | Pass criterion |
|---|---|---|
| Telegram fast reception | Telethon listener, measure `message.date` → local receipt | p95 latency < 2s |
| Telegram reconnect catch-up | kill the listener mid-stream, restart, count missed messages | zero missed, no duplicates |
| Telegram edit detection | re-fetch last 50 ids each run, diff | edits detected, `matured`/`review_status` preserved |
| TradeLocker quotes | read-only quote fetch | 200, rate limit documented |
| TradeLocker positions | read-only position list | 200, matches broker UI |
| TradeLocker fills / realised P&L | read-only fills endpoint | reconciles to account history CSV |
| **TradeLocker stop modification** | **one demo order**, size 0.01, modify SL, verify, close | SL visible in the position fetch, change confirmed by the broker |
| **1m history at scale** | chunked fetch, 14-day windows | no gaps > 1m in the merged series |
| Render → Supabase | write + read from the hosted service | round trip, latency measured |

**Status of the Telegram rows (2026-11-12).** Catch-up, duplicate suppression
and edit detection pass on real message ids; fast reception is blocked by a
host clock 5.4h off true UTC, and would in any case need a four-hour soak in the
10:00–14:00 UTC window rather than a four-minute probe. See
`docs/17-observer-step1.md`.

**Status of the TradeLocker rows (2026-11-12).** Quotes, positions, orders,
executions, account state and the 18 documented rate limits all return on demo,
with every write method disarmed and proven to refuse. Latency is **not
attributable on this host**: a bare TLS connection costs 0.5s at best and timed
out entirely at worst, so broker timing must be measured on the deployment
host. See `docs/18-observer-tradelocker.md`.

`scripts/preflight.py` now gates the host before anything is measured — clock,
network, credentials, database, and optionally the Telegram session and the
broker login — because both defects were found after the numbers were taken.

**Boundary, and an explicit authorisation gate.** Every probe above is
read-only **except one**: the TradeLocker stop-modification check, which
cannot be verified without placing an order.

That order is therefore **not** covered by this handoff and **must not be
placed** until the lead authorises it specifically. The authorisation covers
exactly one thing:

| | |
|---|---|
| Account | TradeLocker **demo** only (`TRADELOCKER_ENV=demo`) |
| Instrument | XAUUSD, size 0.01 lot (minimum) |
| Purpose | prove a stop can be modified and the modification read back |
| Lifecycle | opened, stop modified, position read back, immediately closed |
| Logging | every request/response persisted to the evidence record |
| Live accounts | **none. No order may be placed against a live account under any circumstance in this task.** |

Until that authorisation is given, task 1 runs as a **read-only probe**: the
stop-modification row is marked `BLOCKED — awaiting authorisation` and is
carried as an open item on the demo gate (task 5). Nothing else in the plan
depends on it.

Note the local prototype already exists: `scripts/shadow_collect.py` and
`scripts/shadow_process.py` implement most of the Telegram half, and
`docs/06-shadow-study-spec.md` is a **frozen research spec** for exactly this
observation. The hosted service is that spec, made durable. The spec's rules
carry over unchanged — including the rule that edits to messages more than 50
positions back will not be detected, which the runbook must state.

### 5.2 Task 2 — Supabase schema + RLS

Tables (all `timestamptz`, UTC):

| Table | Purpose | Key columns |
|---|---|---|
| `channel_messages` | raw events, written **before** parsing | `channel`, `message_id`, `revision`, `posted_at`, `received_at`, `edited_at`, `text`, `raw_json` |
| `signals` | parsed setups | `message_id`, `instrument`, `direction`, `entry_type`, `entry_price`, `stop_loss`, `take_profits`, `parse_version`, `review_status` |
| `order_intents` | every intent before it is sent | `idempotency_key`, `symbol`, `side`, `type`, `price`, `size`, `status`, `broker_ref` |
| `fills` | broker fills | `order_intent_id`, `broker_fill_id`, `price`, `size`, `filled_at` |
| `positions` | live positions | `symbol`, `side`, `size`, `entry_price`, `stop_loss`, `opened_at`, `closed_at`, `close_reason`, `realised_pnl` |
| `stop_history` | every stop change, with the change reason | `position_id`, `old_stop`, `new_stop`, `changed_at`, `reason`, `broker_ack` |
| `risk_state` | daily/weekly counters, kill switch | `as_of`, `daily_pnl`, `weekly_pnl`, `peak_equity`, `halted` |
| `notification_outbox` | queued alerts | `channel`, `template`, `payload`, `status`, `sent_at` |
| `service_health` | listener/broker/db liveness | `checked_at`, `listener_ok`, `broker_ok`, `db_ok`, `last_message_id` |

**RLS plan.**

- The service role (Render) is the only writer. It holds the service key; the
  key never leaves Render's environment.
- Vercel holds **no** Supabase secret in v1. If the dashboard needs data, it
  reads through a Render-side read-only endpoint, or Supabase is given a
  separate read-only role scoped to specific views.
- `anon` gets no access to any table.
- Telegram and broker credentials exist **only** on Render.
- The outbox is the only path to Telegram notifications, so a failed send is
  retried rather than dropped, and the bot never posts into a group without an
  explicit human action.

### 5.3 Task 3 — observer-only implementation

- Write the raw event to Supabase **first**, parse **second**. If parsing
  crashes, the message is still in the table and can be re-parsed later.
- Dedupe on `(channel, message_id, revision)`. Re-fetch the last 50 ids each
  run to detect edits; on edit, update only `edited_at`/`text`/`revision` and
  **never** overwrite computed measurements or `review_status`.
- Catch-up on reconnect from `last_message_id`, then backfill by date in
  chunks, so a long outage is recovered rather than skipped.
- Measure `posted_at → received_at` and store it on the message row. Latency
  is a first-class metric, not a log line.
- **Health endpoint** (`/health`) checks three things and returns 503 if any
  fails: listener freshness (`last_message_id` advancing, or a recent poll
  heartbeat), broker reachability, and a real DB round trip. A bare HTTP 200
  is not a health check.
- BotFather notifications for: listener staleness, broker errors, DB errors,
  and a daily summary. Separate bot from the source-channel listener.

### 5.4 Reliability honesty — free tier, stated plainly

Full deployment on free tiers. The runbook must say exactly what is and is
not guaranteed:

- Render free services idle after 15 minutes of inactivity. UptimeRobot
  pinging `/health` every 5 minutes prevents idling but **not** restarts.
- Render free instances also **spin down daily** and restart with a cold
  filesystem — no durable local state, so **all state must live in Supabase**.
- Supabase Free pauses after a week of low activity and has **no** automated
  backups.
- The runbook must therefore include daily export, and an explicit statement
  of what is best-effort.

A paid Render worker plus a non-pausing Supabase plan removes these caveats,
but that is a budget decision to revisit after the demo trial, not a
prerequisite for it.

---

## 6. Sequencing

```
 1. APPROVE THIS PLAN
 2. Create branch feature/paired-exit-replay off rade-phase2-frozen
 3. Export cohort/v1 (labelled setups + splits + MANIFEST with sha256)
 4. Implement the paired replay on that branch; satisfy AC-1..AC-20
 5. Reconcile the replay against the archived S0 baseline (AC-17)
 6. Fetch 1m bars for the cohort period (14-day chunks, gap-verified)
 7. Replay reports train (development) -> val (selection) -> freeze -> test (once)
 8. In parallel: create rade-observer, run task 1 (read-only probes;
    the stop-modification order stays BLOCKED until separately authorised)
 9. Task 2 (schema + RLS) -> task 3 (observer implementation)
10. Demo gate (task 5) only after the stop-modification probe is authorised
    AND passed, and the policy is approved
11. Prospective demo period confirms the chosen policy
```

The paired replay (handoff task 4) is independent of the observer and does
not need it. The demo gate (task 5) depends on both.

### 6.1 Where this stands (2026-11-11)

Steps 1–7 are **done**. The replay has run on 1m and the pre-registered
decision rule has been applied.

| Step | State |
|---|---|
| 2 — branch | done: `feature/paired-exit-replay`, branched from `rade-phase2-frozen` |
| 3 — cohort export | done: `cohort/v1/`, 1,972 rows, SHA-256 pinned in the manifest |
| 4 — replay | done: `src/rade/replay/`, 69 tests, AC-1..AC-20 satisfied |
| 5 — reconciliation | done: `docs/evidence/legacy-reconciliation.md`; three defects found and fixed |
| 6 — 1m fetch | **done**: 564,213 bars, 42/42 chunks, 11 holes under 55 min in 19 months |
| 7 — 1m replay | **done**: train → val → holdout, all four arms |
| 8 — observer | **in progress**: read-only halves verified — Telegram catch-up/dedupe/edits on real ids, broker read surface with writes disarmed. **Mirror pipeline built and tested locally** (parser, fail-closed sizing, idempotent store, listener, notifications, read-only API, dashboard, deploy config); 198 tests pass, 15/15 simulation scenarios pass. Not deployed; latency rows still BLOCKED by the host. `docs/17-observer-step1.md`, `docs/18-observer-tradelocker.md`, `docs/19-mirror-pipeline.md` |
| 10 — demo gate | **BLOCKED — and now not recommended to open at all** (§6.2) |

### 6.2 Outcome of the 1m replay — study CLOSED (2026-11-12)

`docs/16-replay-results-1m.md` is the authoritative and **final** result for
this retrospective study. Four findings:

1. **The 30m conclusion is retracted.** The runner's +0.024 R edge at 30m
   becomes −0.003 R at 1m, with CIs including zero on every split. It was an
   artefact of bar width: 31% of setups were stopped out on their fill bar at
   30m, versus 4.3% at 1m.
2. **No exit policy was selected, and the runner's effect is unresolved.** The
   four arms' full-cohort means span 0.0085 R per setup — smaller than the
   entry spread (0.0306 R) and smaller than the standard error on any one arm
   (≈0.024 R). That bounds the exit choice's effect on the *average* setup; it
   does not bound the runner's effect where it acts. On the 55 runner-eligible
   validation setups the A→B interval is [−0.0465, +0.0713], wide enough to
   contain an edge worth having. **"The exit policy does not matter" is not an
   established finding.**
3. **Every arm's validation mean is negative** (A −0.0739, B −0.0721, C
   −0.0624, CB −0.0598) — **but every 95% interval includes zero**, so this is
   a failure to demonstrate positive expectancy, not a demonstration of loss.
   The intervals are in `docs/16` §1 and are required reporting (AC-9b).
   Policy B does not beat A; arm C beats A but not B, so the pre-registered
   rule adopts neither.

Per §4.5 step 2 — *"If the baseline does not hold on validation, report the
negative and stop"* — **the decision is to report the negative and stop.**

**No policy is approved, so the demo gate (task 5) does not open.** The demo
stop-modification order remains BLOCKED, and there is now no approved policy to
place it for.

**The retrospective study is closed.** It has answered the question it was
built for — no exit policy is selected — and the frozen cohort has now been
examined during development, so it cannot serve as a clean holdout again.

Selection is still the open question, but **it must be tested on prospective
data, not on this cohort**. Re-searching the same sample for a rule that works
is overfitting wearing a lab coat. That is why the next phase is the read-only
observer (§5), which generates a genuinely fresh period of signals.

---

## 7. Remaining open items

1. **Demo stop-modification order — needs explicit authorisation.** The one
   order in task 1 is BLOCKED until the lead authorises it (§5.1). Until then
   the demo gate carries it as an open item. It cannot be resolved by the
   plan; it needs a yes.
2. **Cohort version** — should `rade-observer` pin `cohort/v1` as-is, or
   wait for a `v2` built from the 1m re-derivation? *Recommendation: pin
   `v1` now.* Cohort *membership* does not change with resolution, so `v1` is
   correct for the observer; the 1m work changes outcomes within the cohort,
   not its membership. A `v2` is only warranted if membership itself turns
   out to change.
3. **1m data is stale relative to the cohort.** The archived bars end
   2026-11-03 and the shadow collector stopped on 2026-11-05. If the replay
   is meant to cover the most recent signals, a top-up fetch is needed before
   the cohort is frozen.
4. **The holdout was observed during replay development (2026-11-11).** The
   replay was debugged against the archived baseline with train/val/test all
   visible, and the holdout was re-run once the timezone and gap defects were
   fixed. No parameter was chosen on it and the policy was frozen before the
   replay existed, but it is not a pristine single observation. The
   mitigation is that the A→B conclusion is identical on train, validation and
   the holdout, and under both ambiguity bounds
   (`docs/15-replay-results.md` §6).
5. **What the data now establishes.** With 1m bars the ambiguity band is 0.05 R
   (down from 0.43 R at 30m), so the *path* measurement is resolvable. It
   establishes that **copying the published setups mechanically does not show
   a positive expectancy** on the validation period under any of four exit
   policies, with every arm's interval spanning zero. It does **not** establish
   that the strategy loses, that the trader is unprofitable, or that the exit
   policy is irrelevant — the runner's own interval on the 55 eligible setups
   is [−0.0465, +0.0713]. The open question is **selection**: whether any
   pre-specified rule identifies a subset of these setups with positive
   expectancy. That is a different study on a **different, prospective** sample.
6. **The demo gate should not open.** With no approved policy there is nothing
   to demo. The stop-modification probe remains blocked for that reason, not
   only for want of authorisation.
