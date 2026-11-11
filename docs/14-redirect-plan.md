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

`main` carries only the freeze metadata (README status + `.gitignore`). New
work happens on branches off the freeze tag, never on `main`.

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
  costs. The floor is what makes the runner incapable of turning a trade into
  a loss.
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

**Related landmine.** `setup_extractor.py` preserves raw message order for TPs,
and `backtest/engine.py` consumes `take_profits` **verbatim** — while
`labelling/labeler.py` normalises nearest-first. Measured on the cohort: **25
setups have TPs in non-nearest-first order, and 15 have a TP on the wrong side
of entry.** Today that is a 1.3% discrepancy. Under a TP-indexed policy it
becomes a correctness bug, because "TP3" would mean a different price in the
engine than in the label. **The replay must normalise the ladder itself**
(drop wrong-side TPs, sort nearest-first) and record how many were altered.

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
trailing stop, **48h from fill**, or the end of available data. 48h is
generous (max observed hold in this cohort is 9.5h; p99 is 4.5h) and it
truncates only **2 test-split setups** against the 2026-11-03 data end. The
pre-ladder phase gets a **24h hard cap** — provably non-binding (max observed
9.5h; only 6 setups reached even the old 15h cap). Unlike the old 15h cap,
the 24h cap is not presented as this trader's real hold limit; it is a
simulation guard chosen because it demonstrably does not bind.

**Window-end handling.** Any position still open when data ends is closed at
the final bar's close with `exit_reason = window_end`, and the count is
reported per arm. It is never silently dropped.

---

## 3. Data resolution — 1-minute primary, 30-minute as sensitivity

The replay must resolve stop/target ordering at the finest available
granularity, and ambiguity must be **marked**, never resolved favourably.
TradeLocker has been probed live today (demo account, read-only):

| Probe | Result |
|---|---|
| 1m resolution available | **Yes** — 1,379 rows for a 24h window (23h trading + 1h break) |
| 1m bar step | 1 minute, no gaps within the probed windows |
| Request speed | ~2.5–5s per request |
| Row cap | **Enforced at 30,000** — a 30-day 1m request raises `ValueError: No. of requested rows (44640) larger than max allowed (30000)` |
| 14-day chunk | Works — 13,772 rows, ~4.4s |
| Instrument | XAUUSD id `1808`, server PLEXY, demo `https://demo.tradelocker.com` |

**Fetch plan.** 2025-04-01 → 2026-11-03 in **14-day chunks** (≈42 requests,
≈4 minutes, max ~13.8k rows per request). Chunk windows do not align exactly
to the API's returned span, so chunks are **upserted on `ts`** and the merged
series is **verified for gaps > 1 minute** before use. Any setup whose
evaluation window contains an unresolved gap is excluded and counted.

**What changes on 1m, and what does not.**

- **Cohort membership does not change.** Inclusion (parsed, valid, non-recap,
  filled) is a property of the message and the market, not of bar resolution.
  The cohort stays the one frozen in `cohort/v1`.
- **Fills and exits are re-derived on 1m inside the replay.** The fill anchor
  becomes precise, and the 17.3% intra-bar ambiguity measured on 30m should
  fall sharply. The fall is itself a reported number — it quantifies how much
  of the archived S0 ambiguity was a 30m artefact.
- **30m stays as a reported sensitivity**, so the replay remains comparable
  with the archived S0 numbers in `data/s0_report.csv`. If 1m and 30m agree
  materially, the archived results stand; if they disagree, the 1m result is
  the one that counts and the discrepancy gets reported.

**Feature rows are not recomputed.** Features are D1/1H-context features at
post time; resolution does not change them, and the archived `features.csv`
stays valid for the cohort.

---

## 4. The frozen replay protocol

Written down **before** running, so the holdout can be touched once.

### 4.1 Cohort

```
2,382  labelled setups (all)
  − 410  recaps / unfilled / invalid
  ──────
1,972  filled & non-recap
  −  29  no split assignment (unknown period)
  ──────
1,943  in the purged train/val/test split
        of which 1,855 have ≥2 TPs  (policy-relevant)
        of which 1,235 reach their FTL within 48h of fill
```

**The paired statistic lives on the FTL-reaching subset** (train 631, val 296,
test 308) — those are the only setups where the two arms can differ.

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
| Slippage | $0.25 on market exits (SL, timeout, forced) | observed base case |
| Limit exits (TP fills) | free | existing `CostModel` |
| Commission | none | absent from the whole repo |
| Ambiguity (stop and target in the same bar) | **stop wins — marked, never favourable** | repo convention, at every resolution |
| Stop after TP1 | → cost-adjusted breakeven (`entry ± 0.50`) | identical in both arms |
| Stop after TP2 | no change | identical in both arms |
| Trail rule | 1R behind favourable peak, ratcheted, never backward | §2.2 |
| Runner size at FTL | half the remaining tranche trails; half closes | §2.1 |
| Max hold, pre-ladder | 24h hard cap | provably non-binding |
| Max hold, runner | 48h hard cap | only 2 test setups affected |
| Seed | 42, deterministic | |

### 4.4 Metrics — per setup, not per leg

The current `metrics.py` reports **per-leg** figures, and the S0 verification
note already flagged that "56 trades" was really ~14 setups × ~4 legs. All
paired reporting is **per setup**:

- net R per setup (total P&L ÷ initial risk, one R per setup, not per leg)
- expectancy in R, profit factor, hit rate
- max drawdown on the per-setup equity curve
- average/median hold time, per arm
- exit-reason mix (`tp1`, `tp2`, `ftl`, `trail_stop`, `sl`, `time_cap`, `window_end`)
- ambiguous-bar counts, and the optimistic-bound delta
- **paired difference** (A vs B) on the FTL-reaching subset, with CI

For context on the noise floor: per-setup R on the archived label set has
sd ≈ 0.85–1.00 and SE of the mean ≈ 0.03 (train) to 0.04 (val/test). A
*paired* design has far lower variance than that, because the arms are
identical on most setups.

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

---

## 5. The observer/recovery foundation (new repo)

Tasks 1–3 of the handoff, scoped. **Full deployment on the free tier** — the
complete stack and runbook, with the reliability caveats stated honestly
rather than engineered around.

### 5.1 Task 1 — feasibility probe (no live orders)

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

**Boundary.** The stop-modification probe is the only step that places an
order, and it is **demo-only, minimum size, immediately closed, fully logged**.
That is a capability probe, not a strategy trade. Nothing runs against a live
account in this task.

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
 2. Fetch 1m bars for the cohort period (14-day chunks, gap-verified)
 3. Implement the paired replay on branch feature/paired-exit-replay
    off rade-phase2-frozen
 4. In parallel: create rade-observer, run task 1 (feasibility probe)
 5. Task 2 (schema + RLS) -> task 3 (observer implementation)
 6. Replay reports train -> val (selection) -> freeze -> test (once)
 7. Demo gate (task 5) only after TradeLocker stop modification is proven
    and the policy is approved
 8. Prospective demo period confirms the chosen policy
```

The paired replay (handoff task 4) is independent of the observer and does
not need it. The demo gate (task 5) depends on both.

---

## 7. Remaining open item

- **Cohort version** — should `rade-observer` pin `cohort/v1` as-is, or
  should it wait for a `v2` built from the 1m re-derivation? *Recommendation:
  pin `v1` now.* The cohort *membership* does not change with resolution, so
  `v1` is still correct for the observer; the 1m work changes outcomes within
  the cohort, not its membership. A `v2` is only warranted if membership
  itself turns out to change.
