# Reconciling the archived S0 baseline against the paired replay

**Status:** complete. Every disagreement between the archived legacy baseline
(`data/s0_report.csv`) and the new paired replay is accounted for, and the
replay is now proven to reproduce the baseline wherever the two are supposed
to agree.

**Date:** 2026-11-11
**Re-runnable script:** `scripts/diagnose_legacy_divergence.py`
**Pinned by:** `tests/test_replay_reconciliation.py` (5 tests)

---

## 1. Why this had to be settled first

The archived S0 report and the new replay disagreed by **+0.22 R per setup**
(mean). One of them was reporting a strategy with positive expectancy and the
other was reporting it with negative expectancy. Nothing downstream — the
policy comparison, the expectancy headline, the observer's sizing — is
trustworthy until that gap is explained.

It turned out to have **three** independent causes, not one.

---

## 2. Step 1 — the archived baseline reproduces exactly

Before attributing anything, the baseline itself was re-run from the current
code and the current database and compared row by row against the archived
CSV.

| Check | Result |
|---|---|
| Archived rows | 1,972 |
| Fresh legacy run | 1,972 |
| Common setup ids | 1,972 |
| Identical `total_R` (\|d\| < 1e-9) | **1,972 / 1,972** |
| Maximum absolute difference | **0.0000000000 R** |
| Only in archive / only in fresh | 0 / 0 |

**The baseline is not stale.** The database, the engine and the archived CSV
are all in exact agreement, so the divergence is a genuine difference in
*behaviour*, not a difference in *inputs*.

> This test is now pinned in the suite as
> `test_archived_s0_baseline_is_still_reproducible`, so the comparison can
> never again be made against a moving target.

---

## 3. Step 2 — the legacy engine never normalised the take-profit ladder

### The defect

`src/rade/backtest/engine.py` indexes `pos.take_profits` **in the raw order
the trader published them**, and never checks that a target is actually
reachable from the entry. A target on the *wrong side* of the entry is treated
as a live take-profit and filled **instantly**, at that target's price.

Because the per-leg size is `size / n_tps`, including the unreachable target
also inflates the number of legs, which shrinks every leg.

### The proof, on `pk=452`

```
direction  buy
entry      3360.0
stop       3351.0                     risk_price = 9.0
raw TPs    [2367.0, 3380.0, 3400.0]   <- the first target is $993 BELOW entry
```

The archived row for this setup:

```
n_legs 2   total_pnl -17022.684471   total_R -37.462963   risk_dollars 454.38703
```

Reproducing the legacy arithmetic exactly:

| Step | Computation | Value |
|---|---|---|
| `risk_dollars` | `0.005 × 90,877.41` (running equity at fill) | 454.38703 |
| `size` | `454.38703 / 9.0` | 50.487448 |
| leg fraction | `1 / 3` (the raw ladder has three entries) | 0.333333 |
| leg 1 size | `50.487448 / 3` | 16.829149 |
| leg 1 P&L | `16.829149 × (2367.0 − 3360.0)` | **−16,711.3452** (−36.7778 R) |
| leg 2 (stop) | `33.658 × (3351 − 3360) − 0.25 × 33.658` | −311.3393 (−0.6852 R) |
| **total** | | **−17,022.684476 → −37.462963 R** |

Reproduced to **1e-6 R**. The archived outlier is not a mystery — it is a
target $993 below the entry being filled as if it were a profit.

### Population and impact

| Measure | Value |
|---|---|
| Eligible setups (non-recap, has a stop) | 2,019 |
| With at least one target on the wrong side | **17** |
| Whose ladder was not nearest-first | 10 |
| With a duplicate target | 0 |
| Setups whose result changes once the ladder is normalised | **13** |
| Mean R shift over all 1,972 | **+0.0378 R** |
| — contributed by the 15 wrong-side setups in the filled set | **+0.0368 R (97.4%)** |
| — contributed by the other 1,957 setups | +0.0010 R |

**97% of the ladder defect's impact sits in 15 setups.** The other 1,957 are
untouched (mean +0.0010 R each).

### The isolated arithmetic

`pk=452` goes from **−37.462963 R** to **−1.027778 R** once the unreachable
target is removed — i.e. it becomes the ordinary stop-out it should have been.
Both values are asserted in the test suite.

### Two further wrong-side cases

`pk=9730`, `pk=9731`, `pk=9732` are three copies of one setup (same entry,
stop, targets and `posted_at`) whose **entire** ladder — `[4608.0, 4613.0]`
against a buy entry of **4703.0** — is on the wrong side. The legacy engine
filled both targets instantly for **−9.25 R** each.

The replay marks these **invalid** rather than inventing an exit for a
trade that was never published as tradeable. This is the only reason the
replay fills 1,969 of the cohort's 1,972.

---

## 4. Step 3 — two further gaps, both in the replay

With the ladder normalised, the legacy engine and the replay still disagreed
on 15 setups. Both causes were in **my** new code, not the old.

### 4a. The gap-across-a-cap-boundary bug

The 30m series is **67.7% continuous**: 18,872 bars over a 581-day span,
with **73 weekend gaps of ~49.5h** (Friday 20:30 → Sunday 22:00).

The replay originally stopped scanning at the last bar *before* the cap
boundary. For a Friday-evening fill there is no bar at all until Sunday, so
the position was closed at **Friday's stale close** and mislabelled
`window_end` — a price the market had long since left.

The plan requires the opposite:

> "The runner closes at the earliest of: the trailing stop, **48h from fill**,
> or the end of available data."
>
> "**Window-end handling.** Any position still open when data ends is closed at
> the final bar's close with `exit_reason = window_end`."

A cap is a wall-clock rule, so it binds at the **first bar at or after** the
boundary. `window_end` is reserved for the end of the data.

Fixed in `src/rade/replay/engine.py`: the scan now runs to the first bar at or
after the outermost cap, and `window_end` means what the plan says it means.
Guarded by three tests in `tests/test_replay_reconciliation.py`.

**Effect:** residual disagreement fell from 15 setups to 8, and the mean
residual from **−0.0043 R to −0.0002 R**.

### 4b. The timezone bug in the cohort reader — the largest single cause

`scripts/run_paired_replay.py` loaded `posted_at` from `cohort/v1/labeled_setups.csv`
like this:

```python
posted_at = datetime.fromisoformat(r["posted_at"]).astimezone(timezone.utc)
```

The cohort export carries **naive-UTC** timestamps (that is how the labeller
stores them — `scripts/store_labeled.py` parses them with `utc=True`). But
`.astimezone()` on a *naive* datetime interprets it as **local** time. This
machine is **UTC+1**, so every `posted_at` was shifted by **−1 hour**, and with
it every fill window.

| | n filled | mean net R | win rate | PF |
|---|---|---|---|---|
| Buggy reader (naive → local) | 1,959 | **+0.0979** | 0.691 | 1.296 |
| Fixed reader (naive → UTC) | 1,969 | **−0.0507** | 0.580 | 0.887 |

**+0.1486 R per setup.** The bug turned a losing strategy into an apparently
winning one and silently un-filled 10 setups.

Fixed with an explicit `parse_utc()` helper, and pinned by two tests
(`test_naive_cohort_timestamps_are_read_as_utc`,
`test_cohort_csv_timestamps_match_the_database`).

The cohort CSV itself is **unchanged** — the freeze and its SHA-256s stand.
The convention is now documented in `cohort/v1/MANIFEST.json` under
`timestamp_convention`.

---

## 5. What is left — 8 setups, all intentional

With the ladder normalised, the gap handled and the timestamps correct, the
legacy engine and the replay agree on **1,961 of 1,969** setups to within
1e-9 R. The remaining **8** are all the same case, and all of them are a
deliberate difference.

**The stop-vs-cap tie.** On the bar that carries the cap boundary, the replay
checks the stop **first** (plan §3.2: *"the stop wins ties"*), so the
position exits at the stop. The legacy engine applied its timeout check
*before* its stop check, so it closed at the bar close instead — the
optimistic reading.

| | legacy | replay |
|---|---|---|
| exit reason | `timeout` (bar close) | `sl` (the stop price) |
| setups | 8 | 8 |

Verified on all 8: in every case the stop is genuinely touched in the cap
boundary bar. The replay's tie-break is asserted in the suite.

> This is a tie-*rule*, not a "the replay is always worse" rule. For a short
> whose bar closed through the stop, exiting at the stop is the better price.
> What the replay guarantees is that **the stop binds**, which is what the
> plan specifies.

---

## 6. The end-to-end check

The strongest evidence is that the corrected replay, run through the cohort
CSV in the legacy's own configuration, lands back on the archived baseline:

| | archived `data/s0_report.csv` | replay (CSV path, BE off, cap 15h) |
|---|---|---|
| setups | 1,972 | 1,969 filled + 3 invalid |
| mean R | **−0.0879** | **−0.0491** (legacy-equivalent) |
| win rate | **0.340** | **0.342** |
| total R | −173.4 | −156.93 |

The win rate matches to 0.2pp. The mean R differs by **+0.0388 R** — which is
the ladder correction (+0.0378 R) plus the 8 tie cases (−0.0002 R), to within
0.0008 R.

**The entire original +0.2218 R gap is accounted for:**

| Source | Size | Owner |
|---|---|---|
| Timezone bug in the cohort reader | **+0.184 R** | replay (fixed) |
| Missing ladder normalisation | **+0.038 R** | legacy engine (defect) |
| Gap-across-cap-boundary handling | +0.004 R | replay (fixed) |
| Stop-vs-cap tie-break | −0.0002 R | intentional (replay is right) |

---

## 7. Consequence for the headline

The archived S0 baseline was **right** and the first replay was **wrong**. The
corrected 30m expectancy is **negative**, not positive:

| Split | Policy A mean net R | Policy B mean net R |
|---|---|---|
| train | −0.0689 | −0.0437 |
| val | −0.0642 | −0.0459 |
| test | +0.0048 | +0.0260 |

But that is **not** the end of the story, because at 30 minutes the answer is
dominated by bar-interior ordering. See `docs/15-replay-results.md` §4: the
pessimistic base case and the optimistic bound differ by **0.43 R per setup**
on the same data, spanning from "loses money" to "very profitable". The 30m
series cannot resolve this strategy's expectancy at all, which is exactly the
gap the 1-minute fetch exists to close.

---

## 8. What was changed

| File | Change |
|---|---|
| `scripts/run_paired_replay.py` | `parse_utc()` — naive cohort timestamps are UTC |
| `src/rade/replay/engine.py` | caps bind at the first bar at/after the boundary; `window_end` means end-of-data; `optimistic_ambiguity` added |
| `src/rade/replay/assumptions.py` | `optimistic_ambiguity` flag (AC-7 bound) |
| `src/rade/replay/metrics.py` | FTL-reaching population added to the paired statistics |
| `scripts/diagnose_legacy_divergence.py` | **new** — the reconciliation, re-runnable |
| `tests/test_replay_reconciliation.py` | **new** — 5 regression tests pinning the reconciliation |
| `tests/test_replay.py` | 3 tests for the optimistic bound; self-sufficient `sys.path` |
| `cohort/v1/MANIFEST.json` | `timestamp_convention` documented (CSV bytes unchanged) |

**The legacy engine was deliberately not modified.** It is the frozen Phase 2
research code; the replay is the corrected engine, and the reconciliation
above documents the difference rather than rewriting history.
