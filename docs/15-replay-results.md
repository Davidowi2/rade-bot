# Paired exit-policy replay — 30-minute results

> ## ⚠️ SUPERSEDED BY THE 1-MINUTE RESULT
>
> **Read [`docs/16-replay-results-1m.md`](16-replay-results-1m.md) instead.**
>
> This document is kept as the **sensitivity analysis** the plan requires
> (AC-14), not as the result. The 1m replay, run on 564,213 fetched bars,
> **reverses its central conclusion**: the A→B advantage reported here
> (+0.0239 R, CIs excluding zero on all three splits) becomes −0.0032 R with
> CIs including zero at 1m.
>
> The reason is stated correctly in §1 below and is worth keeping: at 30m the
> pessimistic and optimistic bounds are 0.43 R apart, so 30m cannot resolve
> this strategy's expectancy at all. At 1m that band shrinks to 0.05 R.
>
> **Do not quote any number from this document as a result.** Quote it only as
> evidence for why 30-minute bars are the wrong instrument.

**Status:** complete for 30m — superseded, retained as sensitivity.
**Date:** 2026-11-11
**Spec:** `docs/14-redirect-plan.md` §3–§4
**Reconciliation:** `docs/evidence/legacy-reconciliation.md`
**Code:** `src/rade/replay/`, `scripts/run_paired_replay.py`,
`scripts/diagnose_legacy_divergence.py`
**Tests:** 69 passing (`pytest tests/`)

---

## 1. Read this first

Two numbers decide everything below.

| Policy A, all 1,969 filled setups, same data, same cohort | mean net R |
|---|---|
| **Pessimistic base case** (stop wins a same-bar tie) | **−0.0507** |
| **Optimistic bound** (target wins a same-bar tie) | **+0.3822** |

**The gap between the two bounds is 0.433 R per setup.** On the *same* data,
the *same* cohort, the *same* costs, the strategy either loses money or makes
a lot of it, and the only thing that decides which is the order of events
*inside* a 30-minute bar.

That is the finding. **The 30-minute series cannot resolve this strategy's
expectancy.** It is not evidence of an edge, and it is not evidence against
one. Any number quoted from 30m alone — including the archived S0 baseline's
−0.0879 R and this replay's −0.0507 R — is an artefact of an unresolvable
ordering question.

The mechanism is measurable:

| | Pessimistic | Optimistic |
|---|---|---|
| Setups stopped out **on the bar their limit filled on** | **607 (30.8%)** | **275 (14.0%)** |
| Total stop exits | 834 | 426 |
| Mean winner | +0.6873 (n=1,142) | +0.7781 (n=1,547) |
| Mean loser | −1.0698 (n=827) | −1.0694 (n=422) |

A 30-minute bar's range is wide enough to span both the entry and the stop
often enough that **73% of all stop exits (607 of 834) happen on the fill
bar**. The optimistically-bound cases differ by exactly those 332 setups.

This is what the 1-minute fetch is for. Everything else in this document is
secondary.

---

## 2. Cohort and accounting

| | count |
|---|---|
| Eligible setups (non-recap, has a stop) | 2,019 |
| Filled & non-recap (the archived cohort) | 1,972 |
| — of which **invalid** in the replay (whole ladder on the wrong side) | **3** (`pk=9730/9731/9732`) |
| Filled in the replay | **1,969** |
| Unfilled (limit never touched inside 24h) | 45 |
| Invalid (no usable ladder) | 5 |

Split intersection (from `cohort/v1/MANIFEST.json`):

| Split | ids | in cohort | replay filled | role |
|---|---|---|---|---|
| train | 1,198 | 1,173 | 1,173 | development only |
| val | 388 | 383 | 383 | **selection** |
| test | 404 | 387 | 387 | holdout |

29 cohort rows carry no split assignment (purged at the boundaries); 47 split
ids are not in the cohort (never filled).

---

## 3. Results

### 3.1 Full cohort, per split — pessimistic base case

Policy A closes everything at the final target; Policy B closes half the final
tranche and trails the rest under the 1R rule.

| Split | A mean net R | A win | A PF | B mean net R | B win | B PF | B − A (full cohort) |
|---|---|---|---|---|---|---|---|
| train | −0.0689 | 0.537 | 0.861 | −0.0437 | 0.537 | 0.912 | **+0.0252** [+0.0138, +0.0366] |
| val | −0.0642 | 0.626 | 0.840 | −0.0459 | 0.626 | 0.886 | **+0.0183** [+0.0043, +0.0324] |
| test | +0.0048 | 0.667 | 1.014 | +0.0260 | 0.667 | 1.073 | **+0.0211** [+0.0058, +0.0365] |
| all | −0.0507 | 0.580 | 0.887 | −0.0269 | 0.580 | 0.940 | **+0.0239** [+0.0159, +0.0318] |

### 3.2 Optimistic bound, per split

| Split | A mean net R | B mean net R |
|---|---|---|
| train | +0.4085 | +0.4726 |
| val | +0.3395 | +0.3660 |
| test | +0.3209 | +0.3523 |

The *ordering* of the splits is the same under both bounds, and the A→B
improvement survives both. That is a genuinely robust signal: **the runner
helps, by roughly +0.02 R per setup, regardless of how the bar interiors are
resolved.** It does not rescue the strategy, because the strategy's problem is
not the exit.

### 3.3 The three populations (reviewer's correction #5)

Three populations are always reported together, because each answers a
different question and none substitutes for another.

> **Corrected 2026-11-12.** This table originally described the 1,340-setup
> group as "FTL-reaching — did the runner have anything to run on?". That was
> wrong: 1,340 setups had the final target's *price* touched at some point,
> but only **282** had a live position when it was reached. The other 1,058
> had already stopped out or hit the pre-ladder cap. The group is now called
> **final target touched** and is presented as an upper bound; the 282-setup
> group is the **runner-eligible** population, and at 30m it happens to equal
> the differing subset for A vs B (as it must — see `docs/14-redirect-plan.md`
> §4.4).

| Population | n (all) | What it answers |
|---|---|---|
| **Full cohort** | 1,969 | overall strategy performance |
| **Final target touched** (the final ladder level was traded inside 48h of fill) | 1,340 | did price reach the final target? **Upper bound only** |
| **Runner-eligible** (a final-target leg filled while the position was live) | **282** | did a runner actually exist? |
| **Differing** (the arms actually produce different outcomes) | 282 | what is the runner worth? |

Policy A → B, all splits:

| Population | n | mean diff (B−A) | CI95 | SD | MDE @80% |
|---|---|---|---|---|---|
| Full cohort | 1,969 | **+0.0239** | [+0.0159, +0.0318] | 0.1803 | 0.0114 R |
| Final target touched | 1,340 | **+0.0351** | [+0.0234, +0.0467] | — | 0.0167 R |
| Runner-eligible | 282 | **+0.1666** | [+0.1139, +0.2193] | — | 0.0753 R |
| Differing | 282 | **+0.1666** | [+0.1139, +0.2193] | — | 0.0753 R |

> All of these figures are 30m, and all of them are **retracted** by the 1m
> run. At 1m the same comparison is −0.0032 R on the full cohort with a CI
> spanning zero.

Per split, full cohort: train +0.0252, val +0.0183, test +0.0211 — all with
CIs excluding zero, and all of the same order.

**The stated effect size.** This design detects ≈ **0.022 R** over the full
holdout split and ≈ **0.075 R** restricted to the differing subset. A runner
edge smaller than roughly **0.05 R per setup** is not measurable by this
cohort at this size. The measured edge is +0.021 R — **below that floor on
the full-cohort measure, and above it on the differing subset.** The honest
statement is: *the runner's effect on the whole cohort is around +0.02 R and
this cohort can just barely resolve it; the effect on the setups where it
actually acts is +0.17 R and is clearly resolved.*

> **Note on the FTL count.** The plan (§4.1) quotes 1,235 FTL-reaching
> setups (631 / 296 / 308). That figure came from an ad-hoc calculation that
> was never committed as code and cannot be reproduced by any of the four
> definitions tested (normalised ladder from fill: 701/297/317 = 1,315; from
> `posted_at`: 692/296/317; raw ladder: 705/297/317; the whole cohort: 1,340).
> The replay now computes it explicitly and publishes the definition:
> **the final normalised ladder level is touched at any point inside 48h of
> fill, independent of the exit policy.** The replay's figure supersedes the
> plan's.

### 3.4 Arm C (TP2 → TP1) — rejected

Pre-registered rule: *"Adopt arm C only if it beats both A and B on
validation."*

| Split | A → C, full cohort | A → C, final target touched |
|---|---|---|
| train | +0.0080 [+0.0001, +0.0160] | −0.0018 [−0.0134, +0.0098] |
| **val** | **+0.0053 [−0.0031, +0.0136]** | **+0.0037 [−0.0064, +0.0138]** |
| test | +0.0008 [−0.0096, +0.0112] | −0.0009 [−0.0118, +0.0101] |

On validation the CI includes zero on both populations. **Arm C is rejected.**
It was never adopted.

### 3.5 Selection outcome

> **RETRACTED by the 1m run.** The paragraph below was correct for 30m and is
> wrong for 1m, where B does **not** beat A (CI includes zero). No policy is
> selected. Kept only to document what the 30m data appeared to show.

~~**Policy B is selected**, on validation only: B beats A there with a CI that
excludes zero (+0.0183 [+0.0043, +0.0324] full cohort; +0.0235 [+0.0056,
+0.0414] final-target-touched), and the pre-registered 1R trail / half-tranche
runner rule is unchanged.~~

**But selection is not endorsement.** B is the better exit policy on this
data; neither arm has positive expectancy at 30m. The exit policy is not the
binding constraint.

---

## 4. Sensitivity and decomposition

### 4.1 Cap sensitivity — the cap is not the problem

Policy A, all splits, breakeven on:

| pre-ladder cap | mean net R | total R | `time_cap` exits |
|---|---|---|---|
| 6h | −0.0508 | −100.09 | 74 |
| 12h | −0.0503 | −99.08 | 36 |
| 15h | −0.0488 | −96.11 | 26 |
| **24h (frozen)** | **−0.0507** | −99.86 | **24** |
| 48h | −0.0546 | −107.46 | 10 |
| 72h | −0.0574 | −112.93 | 1 |

The cap binds on **24 setups** at the frozen 24h setting and the result moves
by less than 0.009 R across a 12× range of cap lengths. **The cap length is
not what makes this strategy look bad.** (The plan's earlier "provably
non-binding" claim is corrected: it binds 24 times, and the binding count is
now reported as a headline output per AC-8.)

### 4.2 Breakeven decomposition — it helps, and it is not enough

Policy A / Policy B, all splits:

| | A mean net R | B mean net R | A median |
|---|---|---|---|
| breakeven after TP1 **off** | −0.0855 | −0.0572 | −0.4750 |
| breakeven after TP1 **on** | **−0.0507** | **−0.0269** | **+0.1917** |

Moving the stop to cost-adjusted breakeven after the first target adds
**+0.0348 R** and flips the median from −0.475 to +0.192. It is a real
improvement and it is applied identically in both arms — but it does not
change the sign.

### 4.3 Entry cost

The legacy engine charged the entry spread to equity only, so it never
reached any leg's R. Charging it once per setup costs **−0.0306 R per setup**
(mean legacy R − mean net R, all splits). Every headline number above is net
of it.

---

## 5. Reconciliation to the archived baseline

The replay was verified against `data/s0_report.csv`. Full detail in
`docs/evidence/legacy-reconciliation.md`; the summary:

| | archived baseline | replay, legacy-matched config |
|---|---|---|
| setups | 1,972 | 1,969 filled + 3 invalid |
| mean R | −0.0879 | −0.0491 |
| win rate | 0.340 | 0.342 |

The residual difference is the **ladder-normalisation defect in the legacy
engine** (+0.0378 R, 97% of it carried by 15 setups with a target on the wrong
side of the entry). The replay is the corrected engine; the legacy engine is
frozen Phase 2 code and was deliberately not rewritten.

**A correction to earlier work in this repo.** An earlier version of the
replay reported positive expectancy (train +0.0647, val +0.0888, test
+0.2175). Those numbers were wrong: the cohort reader treated the export's
naive-UTC timestamps as local time and shifted every fill window by one hour
on this UTC+1 machine. The corrected figures are the ones above. The
archived S0 baseline was right all along.

---

## 6. Holdout disclosure

The holdout was **observed during engine development**, before this run. The
sequence was:

1. Replay written, run on train/val/test with the timezone defect → wrong numbers.
2. Reconciliation against the archived baseline → three defects found and fixed.
3. Replay re-run on train/val/test → these results.

The policy was frozen *before* the replay existed (it comes from the original
handoff: 1R ratcheted trail, half the final tranche trails, no TP2 stop move),
and the only selection made — rejecting arm C — was made on **validation**.
Nothing was tuned on the holdout.

Even so, the holdout is no longer a pristine single observation, and this
document should not be read as one. The mitigation that matters is that the
**A→B conclusion is identical on train, validation and the holdout, and under
both the pessimistic base case and the optimistic bound** — so it does not
rest on the holdout being untouched.

---

## 7. What happens next

1. **Fetch the 1-minute bars** (2025-04-01 → 2026-11-03, 14-day chunks,
   ≈42 requests). The probe already proved this is possible; the 30k-row cap
   is the only constraint. Gap-verify the merged series before use.
2. **Re-run the replay on 1m**, train → validation → freeze → holdout.
3. **Report both bounds again.** The 1m pessimistic/optimistic gap is the
   number that matters now: it is the residual ambiguity that even 1m cannot
   remove, and it replaces the 0.433 R spread seen at 30m.
4. **Expect the entry-bar-stop count to fall sharply** (30.8% → ?). If it
   does not, that is itself a finding about the trader's fills.
5. Only then decide whether the project is a *mirroring* bot (copy the
   trader's entries and exits) or a *selection* bot (take the trader's setups
   only when a model says they will work). The 30m data says the entries
   alone are not the edge — but it cannot say that yet, because it cannot
   resolve the bars.

### The observer work is not blocked

Task 1 (read-only feasibility probes: fast Telegram event reception and
reconnect catch-up; TradeLocker quote, entry, stop modification, position
reconciliation, fills/realised-P&L, rate limits; Render→Supabase connectivity)
can proceed in parallel. The **demo stop-modification order remains BLOCKED**
pending explicit authorisation, and no live orders are implied by anything in
this document.

---

## Appendix — reproducing these numbers

```bash
# the reconciliation against the archived baseline
python scripts/diagnose_legacy_divergence.py

# the replay, per split
python scripts/run_paired_replay.py --resolution 30m --split train --out data/replay_30m_train.json
python scripts/run_paired_replay.py --resolution 30m --split val   --out data/replay_30m_val.json
python scripts/run_paired_replay.py --resolution 30m --split test  --out data/replay_30m_test.json

# the optimistic bound
python scripts/run_paired_replay.py --resolution 30m --split all --optimistic

# the legacy-matched configuration
python scripts/run_paired_replay.py --resolution 30m --split all --policies A --no-breakeven --pre-ladder-cap 15

# the suite
python -m pytest tests/ -q
```

Deterministic: no randomness, seed 42 recorded, same inputs give identical
outputs.
