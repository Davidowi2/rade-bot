# Paired exit-policy replay — 1-minute results (authoritative)

**Status:** complete. This supersedes the 30-minute result.
**Date:** 2026-11-11
**Spec:** `docs/14-redirect-plan.md` §3–§4
**30m sensitivity:** `docs/15-replay-results.md`
**Reconciliation:** `docs/evidence/legacy-reconciliation.md`
**Data:** `docs/evidence/1m-fetch.json`, `docs/evidence/1m-gaps.json`

---

## 1. The headline

**The 30-minute conclusion is retracted.** At 1-minute resolution the runner's
apparent edge disappears, and no exit policy produces a positive expectancy on
the validation period.

| Policy A mean net R | train | val | test | all |
|---|---|---|---|---|
| **1m (authoritative)** | **+0.0281** | **−0.0739** | **+0.0235** | **+0.0128** |
| 30m (superseded) | −0.0689 | −0.0642 | +0.0048 | −0.0507 |

| Policy B − Policy A | train | val | test | all |
|---|---|---|---|---|
| **1m** | −0.0032 [−0.0097, +0.0033] | +0.0018 [−0.0067, +0.0103] | −0.0050 [−0.0152, +0.0053] | −0.0032 [−0.0079, +0.0016] |
| 30m (superseded) | +0.0252 [+0.0138, +0.0366] | +0.0183 [+0.0043, +0.0324] | +0.0211 [+0.0058, +0.0365] | +0.0239 [+0.0159, +0.0318] |

At 30m, B beat A on all three splits with CIs excluding zero. At 1m, **the CI
includes zero on all three splits**, and the point estimate is negative on two
of them. The 30m result was an artefact of bar width, not a property of the
trader's behaviour.

**Per the pre-registered decision rule** (§4.5 step 2: *"If the baseline does
not hold on validation, report the negative and stop"*), the runner baseline
does not hold. The honest conclusion is the negative one.

---

## 2. Why the 30m result was wrong

The mechanism is fully measurable, and it is the one §1 of the 30m report
predicted.

| Flag (all splits, Policy A) | 30m | 1m | change |
|---|---|---|---|
| **`entry_bar_stop`** — stopped out on the bar the limit filled on | **607** (30.8%) | **84** (4.3%) | **−86%** |
| **`ambiguous_bar`** — stop and target touched in the same bar | **456** | **33** | **−93%** |
| `open_gap` — fill bar opened beyond the limit | 454 | 290 | −36% |
| `ftl_reached` | 1,340 | 1,313 | −2% |

A 30-minute bar's range spans both the entry and the stop often enough that
31% of setups died on their fill bar. A 1-minute bar rarely does. That single
mechanism drove almost the whole difference.

### The ambiguity band collapsed

| | pessimistic | optimistic | **band** |
|---|---|---|---|
| 30m — train | −0.0689 | +0.4085 | 0.477 R |
| 30m — val | −0.0642 | +0.3395 | 0.404 R |
| 30m — test | +0.0048 | +0.3209 | 0.316 R |
| **1m — train** | **+0.0281** | **+0.0792** | **0.051 R** |
| **1m — val** | **−0.0739** | **−0.0218** | **0.052 R** |
| **1m — test** | **+0.0235** | **+0.0681** | **0.045 R** |

**The unresolvable band shrinks ~8.5×, from 0.43 R to 0.05 R.** That is the
value of the fetch: the 1m numbers are *resolvable*, so the negative below is a
real finding rather than a measurement failure. At 30m no conclusion about
expectancy was possible in either direction.

---

## 3. All four arms, 1-minute, full cohort

| Arm | mean net R | total R | PF | win | max DD | mean hold |
|---|---|---|---|---|---|---|
| A — close all at FTL | +0.0128 | +25.21 | 1.029 | 0.589 | 86.9 R | 1.69h |
| B — half trails | +0.0097 | +19.01 | 1.022 | 0.589 | 84.2 R | 1.81h |
| C — A + TP2→TP1 stop | **+0.0182** | +35.67 | 1.041 | 0.589 | 85.0 R | 1.61h |
| CB — B + TP2→TP1 stop | +0.0155 | +30.40 | 1.035 | 0.589 | 82.3 R | 1.73h |

The **entire spread across four exit policies is 0.0085 R per setup**, against
a per-setup R standard deviation of about 1.0 and a paired MDE of 0.007–0.010 R.
The exit policy moves the result by less than the noise in a single trade.

Per split, Policy A: train +0.0281, val −0.0739, test +0.0235. **The
split-to-split variation (±0.05 R) is six times larger than the largest
difference between any two arms.** That is the whole statistical story: the
choice of exit is not what determines this strategy's outcome.

---

## 4. The selection decision, applied exactly

Pre-registered rule (§4.5): *"Adopt arm C only if it beats **both** A and B on
validation."*

1-minute validation, arm means:

| Arm | mean net R | PF | win |
|---|---|---|---|
| A | −0.0739 | 0.824 | 0.608 |
| B | −0.0721 | 0.829 | 0.608 |
| C | −0.0624 | 0.852 | 0.608 |
| CB | −0.0598 | 0.858 | 0.608 |

Paired comparisons on validation:

| Comparison | full cohort | FTL-reaching | verdict |
|---|---|---|---|
| A vs B | +0.0018 [−0.0067, +0.0103] | +0.0023 [−0.0086, +0.0132] | **CI includes 0** |
| A vs C | +0.0115 [+0.0048, +0.0181] | +0.0133 [+0.0050, +0.0216] | CI excludes 0 |
| **B vs C** | **+0.0097 [−0.0009, +0.0202]** | +0.0110 [−0.0025, +0.0245] | **CI includes 0** |
| A vs CB | +0.0141 [+0.0035, +0.0247] | +0.0167 [+0.0032, +0.0302] | CI excludes 0 |

- **Policy B does not beat A** → the frozen runner policy is not supported.
- **Arm C beats A with confidence, but not B** (the B→C CI includes zero) → under
  the rule as written, **arm C is not adopted either.** The rule is doing its
  job: it refuses to promote a variant on a difference that cannot be
  separated from the arm it is meant to beat.

**No policy is selected. Every arm is negative on validation.** That is the
result the rule was written to produce if the premise failed.

---

## 5. Data quality — the 1m series is good enough to conclude

| Check | Result |
|---|---|
| Bars fetched | 564,255 in 42/42 chunks (14-day windows), 0 failures |
| Bars stored | **564,213** |
| Span | 2025-04-01 → 2026-11-03 |
| Median bar step | **1.0 min** |
| Holes under 55 min (candidate missing data) | **11**, largest **26 min** |
| Daily maintenance breaks (55 min – 2h) | 320 |
| Weekend/holiday closures (>2h) | 93, max 73.0h |
| Coverage | 67.4% of wall-clock minutes — the remainder is weekends and the daily break |

**Eleven small holes across nineteen months.** There is effectively no
unresolved gap, so no setup needs excluding on data-quality grounds.

A useful cross-check on the fill logic: the 1m replay fills **1,963** of the
1,972 cohort setups, against 1,969 at 30m. The two fill sets agree on 99.7% of
the cohort, which is what it should be — resolution changes the *path* inside a
trade, not whether a resting limit was reached.

---

## 6. What this establishes, and what it does not

**Established:**

1. Copying this trader's published setups, with any of the four exit policies
   tested, does **not** produce a positive expectancy on the validation period
   (−0.060 to −0.074 R per setup). The band is now 0.05 R, so this is a real
   measurement, not an ambiguity artefact.
2. **The exit policy is not the binding constraint.** Four policies, spanning
   "close everything at the final target" to "trail half the final tranche",
   differ by 0.0085 R — less than the period-to-period noise and less than the
   cost of the entry spread (0.0306 R).
3. **The 30m result must not be quoted.** Its runner edge (+0.024 R, CIs
   excluding zero on all splits) reverses sign at 1m. Any earlier statement
   derived from 30m — including the plan's planning estimate — is void.

**Not established:**

1. That the trader is unprofitable. The replay measures *published setups taken
   mechanically at the stated entry*. It cannot see the trader's discretionary
   filtering, his actual fills, or the setups he never posted.
2. That no edge exists in the data. Only that it is not in the **exit**. The
   open question is whether it is in **selection** — which of these setups to
   take — and that is a different study, on the same frozen cohort, with the
   same discipline.
3. That 1m is the final word on path. It reduces the ambiguity band 8.5× but
   does not eliminate it; 0.05 R remains. That is small enough not to change any
   conclusion here, but it is not zero.

---

## 7. Recommendation

**Do not open the demo gate.** The plan's step 10 requires policy approval, and
there is no policy to approve: no arm is positive on validation, and the
pre-registered rule selects none.

Three honest options, in the order I would rank them:

1. **Test selection, not exit.** Keep the frozen cohort and the 1m data, and ask
   whether any pre-specified rule identifies a subset of setups with positive
   expectancy. This is the only place an edge can still be. The repo already has
   the feature pipeline and the train→val→test discipline for it.
2. **Measure the trader's actual behaviour** rather than his published levels —
   which setups he skipped, how he managed them. That needs the observer
   (handoff tasks 1–3), and it is read-only.
3. **Stop.** If the goal was to mirror the channel, the answer is now measured:
   the channel's mechanical setups do not pay for their costs.

The observer work remains worth doing on its own merits — it is the only way to
answer option 2 — and its task 1 probes are read-only and unblocked. The
**demo stop-modification order stays BLOCKED**: there is no approved policy to
place it for.

---

## Appendix — reproducing

```bash
# 1. fetch the 1m series (~7 min, 42 requests, read-only)
python scripts/fetch_1m_bars.py --start 2025-04-01 --end 2026-11-03

# 2. verify it
python scripts/verify_1m_gaps.py

# 3. replay
python scripts/run_paired_replay.py --resolution 1m --split train --out data/replay_1m_train.json
python scripts/run_paired_replay.py --resolution 1m --split val   --out data/replay_1m_val.json
python scripts/run_paired_replay.py --resolution 1m --split test  --out data/replay_1m_test.json
python scripts/run_paired_replay.py --resolution 1m --split all   --out data/replay_1m_all.json

# 4. the optimistic bound
python scripts/run_paired_replay.py --resolution 1m --split val --policies A --optimistic
```

Deterministic. No randomness, seed 42, same inputs give identical outputs.
