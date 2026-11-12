# Paired exit-policy replay — 1-minute results (authoritative)

**Status:** complete. This supersedes the 30-minute result.
**Date:** 2026-11-11
**Revised:** 2026-11-12 — statistical wording, the runner population, the
drawdown label and the reconciliation arithmetic corrected after review. See
§8 for exactly what changed and why. The numbers did not change; the claims
about them did.
**Spec:** `docs/14-redirect-plan.md` §3–§4
**30m sensitivity:** `docs/15-replay-results.md`
**Reconciliation:** `docs/evidence/legacy-reconciliation.md`
**Data + input hashes:** `docs/evidence/1m-fetch.json`,
`docs/evidence/1m-gaps.json`, `docs/evidence/replay-inputs.json`

---

## 1. The headline

**The 30-minute conclusion is retracted.** At 1-minute resolution the runner's
apparent edge disappears, and **no exit policy is demonstrated to be
profitable** on the validation period.

Two different statements hide inside that sentence, and they must not be
conflated:

* the **observed mean** is negative for all four arms on validation; and
* the **population mean** is not established to be negative, because every
  arm's 95% interval includes zero.

The first is a fact about this sample. The second is the limit of what 380
setups can prove. Reporting only the first would be an overclaim, so both are
reported together below.

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

### The sampling interval on each arm's mean

These are *not* the same intervals as the pessimistic/optimistic band in §2.
The band is uncertainty about the price path **inside a bar**. This is
uncertainty about the population mean given the **sample size**. Both are real;
neither absorbs the other.

| 1m validation | mean net R | SD | SE | 95% interval | verdict |
|---|---|---|---|---|---|
| A | −0.0739 | 0.9115 | 0.0468 | **[−0.1655, +0.0177]** | includes 0 |
| B | −0.0721 | 0.9207 | 0.0472 | **[−0.1647, +0.0205]** | includes 0 |
| C | −0.0624 | 0.9235 | 0.0474 | **[−0.1553, +0.0304]** | includes 0 |
| CB | −0.0598 | 0.9335 | 0.0479 | **[−0.1537, +0.0341]** | includes 0 |

**Read this honestly.** On 380 validation setups, a mean of −0.074 R with a
standard error of 0.047 R is entirely compatible with a true mean of zero, and
also with a true mean of −0.16 R. The data does not distinguish "this strategy
loses a little" from "this strategy breaks even". What it *does* establish is
that no arm shows a positive expectancy large enough to see at this sample
size — the upper bounds are all below +0.035 R, which is small next to the
0.92 R standard deviation of a single trade.

The same caution applies with the opposite sign on train (+0.0281, CI
[−0.0391, +0.0954]) and test (+0.0235, CI [−0.0701, +0.1171]). **No split, in
either direction, is statistically resolved.**

**Per the pre-registered decision rule** (§4.5 step 2: *"If the baseline does
not hold on validation, report the negative and stop"*), the runner baseline
does not hold. The honest conclusion is the negative one — stated as a failure
to demonstrate, not as a demonstrated loss.

---

## 2. Why the 30m result was wrong

The mechanism is fully measurable, and it is the one §1 of the 30m report
predicted.

| Flag (all splits, Policy A) | 30m | 1m | change |
|---|---|---|---|
| **`entry_bar_stop`** — stopped out on the bar the limit filled on | **607** (30.8%) | **84** (4.3%) | **−86%** |
| **`ambiguous_bar`** — stop and target touched in the same bar | **456** | **33** | **−93%** |
| `open_gap` — fill bar opened beyond the limit | 454 | 290 | −36% |
| `ftl_reached` — final target *price* touched within 48h | 1,340 | 1,313 | −2% |
| — of which the position was **still open** at that moment | 282 | 373 | +32% |

A 30-minute bar's range spans both the entry and the stop often enough that
31% of setups died on their fill bar. A 1-minute bar rarely does. That single
mechanism drove almost the whole difference.

### `ftl_reached` is a price event, not a trade

The flag says only that the market traded at or beyond the final target inside
the horizon. It does **not** say the position was still open at that moment —
and usually it was not. Of the 1,313 setups flagged at 1m, only **373 (28%)**
had a live position when the final target was reached. The other 940 had
already stopped out, hit the pre-ladder cap, or both.

So `ftl_reached` is an **upper bound** on where a runner could have mattered,
not a count of trades that had a runner. The population that actually had one
is `n_ftl_open` — a final-target leg genuinely filled while the position was
live. At 1m that is **373 of 1,963** (19%), and on validation **55 of 380**
(14%). At 30m it is smaller still (282), because coarse bars stop trades out
before the target is reached more often.

This distinction is why the "FTL-reaching" subgroup must never be described as
"trades that reached the final target with a runner". The all-cohort A/B
comparison remains the useful policy comparison; the subgroup is context.

*(The count rises from 30m to 1m because finer bars let more trades survive to
the final target — the same mechanism as the entry-bar-stop collapse.)*

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

| Arm | mean net R | 95% interval | total R | PF | win | DD* | mean hold |
|---|---|---|---|---|---|---|---|
| A — close all at FTL | +0.0128 | [−0.0350, +0.0607] | +25.21 | 1.029 | 0.589 | 86.9 R | 1.69h |
| B — half trails | +0.0097 | [−0.0382, +0.0576] | +19.01 | 1.022 | 0.589 | 84.2 R | 1.81h |
| C — A + TP2→TP1 stop | **+0.0182** | [−0.0297, +0.0661] | +35.67 | 1.041 | 0.589 | 85.0 R | 1.61h |
| CB — B + TP2→TP1 stop | +0.0155 | [−0.0324, +0.0634] | +30.40 | 1.035 | 0.589 | 82.3 R | 1.73h |

\* **DD is not a portfolio drawdown.** It is the largest peak-to-trough decline
of a curve built by sorting *completed* setups by fill time and accumulating
their net R. It ignores exit-time cash flows, concurrent exposure and
mark-to-market on open positions. It is useful for comparing arms on identical
trades and for nothing else — **do not use it to size or approve an account.**
A real portfolio drawdown would need position-level equity marked to market at
each bar, which this replay does not compute.

The **entire spread across four exit policies is 0.0085 R per setup**, against
a per-setup R standard deviation of about 1.08 and a paired MDE of 0.007–0.010 R.
The exit policy moves the result by less than the noise in a single trade.

Per split, Policy A: train +0.0281, val −0.0739, test +0.0235. **The
split-to-split variation (±0.05 R) is six times larger than the largest
difference between any two arms.** That is the whole statistical story: the
choice of exit is not what determines this strategy's outcome.

Note also that every interval in the table spans zero by a wide margin. The
four arms are not four different strategies with four different expectancies;
they are one strategy measured four ways, and the measurement cannot separate
any of them from zero.

---

## 4. The selection decision, applied exactly

Pre-registered rule (§4.5): *"Adopt arm C only if it beats **both** A and B on
validation."*

1-minute validation, arm means:

| Arm | mean net R | 95% interval | PF | win |
|---|---|---|---|---|
| A | −0.0739 | [−0.1655, +0.0177] | 0.824 | 0.608 |
| B | −0.0721 | [−0.1647, +0.0205] | 0.829 | 0.608 |
| C | −0.0624 | [−0.1553, +0.0304] | 0.852 | 0.608 |
| CB | −0.0598 | [−0.1537, +0.0341] | 0.858 | 0.608 |

Paired comparisons on validation, by population (the fourth, the differing
subset, is in the saved JSON and summarised below the table):

| Comparison | full cohort | final tgt touched | runner-eligible | verdict |
|---|---|---|---|---|
| A vs B | +0.0018 [−0.0067, +0.0103] | +0.0023 [−0.0086, +0.0132] | +0.0124 [−0.0465, +0.0713] | **CI includes 0** |
| A vs C | +0.0115 [+0.0048, +0.0181] | +0.0133 [+0.0050, +0.0216] | −0.0103 [−0.0304, +0.0099] | CI excludes 0 (full) |
| **B vs C** | **+0.0097 [−0.0009, +0.0202]** | +0.0110 [−0.0025, +0.0245] | −0.0226 [−0.0831, +0.0378] | **CI includes 0** |
| B vs CB | +0.0123 [+0.0060, +0.0186] | +0.0144 [+0.0065, +0.0223] | −0.0044 [−0.0200, +0.0111] | CI excludes 0 (full) |
| A vs CB | +0.0141 [+0.0035, +0.0247] | +0.0167 [+0.0032, +0.0302] | +0.0079 [−0.0535, +0.0694] | CI excludes 0 (full) |
| C vs CB | +0.0026 [−0.0057, +0.0110] | +0.0034 [−0.0073, +0.0141] | +0.0185 [−0.0404, +0.0775] | CI includes 0 |

- **Policy B does not beat A** → the frozen runner policy is not supported.
- **Arm C beats A with confidence, but not B** (the B→C CI includes zero) → under
  the rule as written, **arm C is not adopted either.** The rule is doing its
  job: it refuses to promote a variant on a difference that cannot be
  separated from the arm it is meant to beat.

**No policy is selected. Every arm's mean is negative on validation, and every
arm's interval includes zero** — so the correct statement is that no policy is
*demonstrated* to work, not that every policy is *proven* to lose. That is the
result the rule was written to produce if the premise failed.

### The runner-eligible column is the one that tests the runner thesis

A sanity check that also sharpens the interpretation:

* **A vs B**: the differing set is **exactly** the 55 runner-eligible setups
  (n_differing = n_runner = 55). That is by construction — A and B are
  identical until the final target, so they can only differ where a final
  target was reached with the position live. This is the only comparison in
  which the runner thesis is actually being tested, and its effect is
  **+0.0124 R on 55 setups, CI [−0.0465, +0.0713]**.
* **A vs C**: the differing set is 20 setups, but only **one** of them is
  runner-eligible. Arm C's stop move to TP1 happens *before* the final target,
  so it changes trades that never become runners. On the runner-eligible
  population C is **−0.0103 R**. **Arm C is not a runner variant at all** — it
  is an earlier-stop variant, and the gain it shows on the full cohort
  (+0.0115) comes from trades that exit sooner, not from managing a runner.

That distinction matters for the decision: the one comparison that tests the
runner question (A vs B) returns an interval spanning zero, and the variant
that looks best on the full cohort (C) does not act on runners.

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

1. **No arm shows a positive expectancy that is visible at this sample size.**
   The observed validation means are all negative (−0.060 to −0.074 R per
   setup) and all four 95% intervals include zero, with upper bounds below
   +0.035 R. That is a **failure to demonstrate**, not a demonstration of loss:
   the data is consistent with a true mean anywhere from roughly −0.17 R to
   +0.03 R. Anyone quoting "−0.074 R per setup" as the strategy's expectancy is
   overreading a 380-setup sample.
2. **The exit policy is not the binding constraint.** Four policies, spanning
   "close everything at the final target" to "trail half the final tranche",
   differ by 0.0085 R — less than the period-to-period noise and less than the
   cost of the entry spread (0.0306 R). The bar-path ambiguity band is now
   0.05 R, so this comparison is resolvable even though the absolute level is
   not.
3. **The 30m result must not be quoted.** Its runner edge (+0.024 R, CIs
   excluding zero on all splits) reverses sign at 1m. Any earlier statement
   derived from 30m — including the plan's planning estimate — is void.
4. **The inputs are pinned.** The bar database, cohort files and every replay
   output are SHA-256 hashed in `docs/evidence/replay-inputs.json`, with the
   commit that produced them, so the run can be verified rather than trusted.

**Not established:**

1. **That the strategy loses money.** The sample mean is negative; the
   population mean is not resolved. This is the single most important caveat in
   the document.
2. That the trader is unprofitable. The replay measures *published setups taken
   mechanically at the stated entry*. It cannot see the trader's discretionary
   filtering, his actual fills, or the setups he never posted.
3. That no edge exists in the data. Only that it is not in the **exit**. The
   open question is whether it is in **selection** — which of these setups to
   take — and that is a different study, on the same frozen cohort, with the
   same discipline.
4. That 1m is the final word on path. It reduces the ambiguity band 8.5× but
   does not eliminate it; 0.05 R remains. That is small enough not to change any
   conclusion here, but it is not zero — and it is a *separate* uncertainty
   from the sampling interval in §1, which is an order of magnitude larger.

---

## 7. Recommendation

**Do not open the demo gate.** The plan's step 10 requires policy approval, and
there is no policy to approve: no arm's validation mean is positive, and the
pre-registered rule selects none. (Nor is any arm *proven* negative — but a
policy cannot be approved on an interval that includes zero either.)

Three honest options, in the order I would rank them:

1. **Test selection, not exit.** Keep the frozen cohort and the 1m data, and ask
   whether any pre-specified rule identifies a subset of setups with positive
   expectancy. This is the only place an edge can still be. The repo already has
   the feature pipeline and the train→val→test discipline for it.
2. **Measure the trader's actual behaviour** rather than his published levels —
   which setups he skipped, how he managed them. That needs the observer
   (handoff tasks 1–3), and it is read-only.
3. **Stop.** If the goal was to mirror the channel, the answer is now measured:
   the channel's mechanical setups do not pay for their costs *on this sample*,
   and no exit policy rescues them.

The observer work remains worth doing on its own merits — it is the only way to
answer option 2 — and its task 1 probes are read-only and unblocked. The
**demo stop-modification order stays BLOCKED**: there is no approved policy to
place it for.

---

## 8. Revision note — what changed on 2026-11-12

A review of commit `7c39995` found five problems with the *write-up*. No trade
outcome, mean or paired difference changed — but one **metric** did: the
runner-eligible counter reported 0 for Policy B, because B's final-target leg
is `ftl_half` rather than `ftl`. That is fixed, and the two FTL populations are
now genuinely separate. The corrections are pinned by
`tests/test_replay_statistics.py` (14 tests).

| # | Finding | Correction |
|---|---|---|
| 1 | Negative sample means were reported as negative expectancy | §1 now reports each arm's 95% interval on the mean. All four include zero. Wording throughout changed from "is negative" to "observed negative, not resolved" |
| 2 | `ftl_reach()` counts a price touch, not a live position | The two counts are now separate everywhere: `n_ftl_touched` (price-only, 1,313) and `n_ftl_open` (runner-eligible, 373). The subgroup is renamed "final target touched" and the runner-eligible column is added (§2, §4) |
| 3 | `max_drawdown_R` is not a portfolio drawdown | Renamed `max_drawdown_R_closed_by_fill_time`, documented as an ordering artefact, and flagged in §3 as unusable for sizing |
| 4 | The reconciliation table's causes did not sum to the gap | Replaced with a measured waterfall that closes exactly (`scripts/decompose_gap.py`). The old `+0.184 R` timezone figure and the `+0.1486 R` figure were both real but measured in **different configurations** — see `docs/evidence/legacy-reconciliation.md` §6 |
| 5 | Reproduction depended on gitignored local files | `docs/evidence/replay-inputs.json` now hashes the bar database, the cohort files and all nine replay outputs, with the producing commit |

**What did not change:** every mean, every paired difference, every flag count,
the policy decision, and the demo gate staying closed.

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

# 5. verify the inputs and outputs against the committed hashes
python scripts/hash_replay_inputs.py
```

Deterministic. No randomness, seed 42, same inputs give identical outputs.
`docs/evidence/replay-inputs.json` records the SHA-256 of the bar database, the
cohort files and every replay output, plus the commit that produced them — so
a reviewer can confirm they ran against the same inputs rather than take it on
trust.
