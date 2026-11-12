# 17 — Observer step 1: Telegram reception, catch-up, dedupe, edits

**Status: partially complete.** Three of the five reception checks pass on real
data. The fourth — end-to-end latency against the 2s budget — **cannot be
measured on this host**, and the fifth needs a window four hours long. Both
limits are measured below, not assumed.

Read-only throughout: no message sent, no broker call, no order. The one row
that needs an order (TradeLocker stop modification) is untouched and remains
`BLOCKED — awaiting authorisation` per `docs/14-redirect-plan.md` §5.1.

Code: `src/rade/observer/reception.py` (pure logic),
`scripts/verify_telegram_reception.py` (live probe),
`scripts/channel_activity.py` (window sizing), `tests/test_reception.py`
(29 tests).

---

## 1. Results

| Check | Result | Evidence |
|---|---|---|
| Catch-up completeness | **PASS** — 85 of 85 messages fetched after a forced 9.5s disconnect; `missed=0`, `extra=0` | `telegram-reception-2026-11-12T13-22-55Z.json` |
| Duplicate suppression across a restart | **PASS** — the same 85 messages replayed with restored state produced 85 duplicates and 0 new records | `telegram-reception-2026-11-12T13-25-07Z.json` |
| Edit detection | **PASS** — refetch of the last 50 ids found no body that differed from what was recorded live | same two files, `missed_edits_on_refetch: []` |
| **Live push delivery** | **UNPROVEN** — no message has ever arrived through the event handler; all 85 records came from the fetch path | §7 |
| End-to-end latency (p95 < 2s) | **BLOCKED** — host clock is 5.4h off true UTC; the measurement is refused rather than reported | §2 |
| Window power for that latency | **UNDERPOWERED at 4 minutes** — the channel posts 1.5 msg/hour overall; a 240s probe expects 0.1 messages at this hour, 25 at 12:00 UTC | §3 |
| Our own dispatch cost | **PASS** — p95 0.024 ms per message, measured on one clock | §4 |

Two things this pass **did not** verify, and does not claim: TradeLocker quotes,
positions and fills; and Render → Supabase round trips. Both are outside the
Telegram half of step 1.

### What "85 of 85" actually proves

The probe was run with `--baseline-id 18700`, which forces the catch-up floor
below the newest message so the fetch path returns a real window instead of an
empty one. The live stream saw nothing (the channel was between posts), the
fetch returned 85 ids, and the diff against what the recorder already held was
empty. That is the same code path a reconnect uses — `_connect()` calls
`catch_up()`, then the audit re-fetches from `last_message_id` — exercised
against real message ids rather than fixtures.

The reconnect itself is in the record: `reconnects[0].down_s = 9.5`,
`recovered: []` (nothing arrived during the 9.5s the link was down, which is
consistent with 1.5 messages/hour).

### The dedupe test is the one that matters

Restarting a listener is the normal case, not the exception: a free-tier host
restarts daily (§5.4 of the plan). If de-duplication state lives only in
memory, every restart re-records the recent window as new — inflating the
record and, in a live observer, re-firing alerts.

So the second run deliberately reloaded the first run's `.jsonl` with
`--state-from` and re-fetched the same window. Result: `new=0`, `duplicates=85`,
`events_recorded=0`. `Recorder.restore()` carries the text hash **and** the
revision number forward, so an edit that arrives after a restart still
increments to the next revision instead of restarting at 0
(`test_restore_keeps_the_revision_number_so_a_later_edit_still_increments`).

A truncated final line — what a crash mid-write leaves behind — is skipped
rather than raised, so a soak can resume from a damaged file
(`test_load_events_skips_a_truncated_final_line`).

---

## 2. The clock blocker

**The host clock is 19,430 s (5 h 24 m) behind true UTC.** Confirmed by three
independent sources:

| Source | Reading |
|---|---|
| `https://www.cloudflare.com` HTTP `Date` header | `Thu, 12 Nov 2026 18:22:30 GMT` while host UTC read `12:58` |
| Telegram's own `message.date` | message `18785` is stamped `2026-11-12T17:15:12Z` and was received by this host at `13:24:08Z` — an arrival **3.85h before it was posted** |
| Windows Time service | **Stopped**, and `Start-Service w32time` fails without elevation |

The Telegram row is the decisive one, because it needs no external clock: a
message cannot be received before it was sent. The gap is 3.85h rather than the
full 5.4h only because the message was already 1.55h old when the fetch
returned it; `3.85 + 1.55 = 5.40` reconciles the two sources.

Root cause: `w32time` is not running, so nothing has disciplined the clock. The
machine is 5.4h slow and drifting.

**Why this is not papered over with an offset.** The probe measures
`true − host` from the `Date` header and corrects for it, but that estimate is
only good to about ±1s: the header is truncated to the whole second and the
round trip on this link was 1.6s. The budget being tested is 2s. A correction
whose error is half the quantity being measured cannot certify a pass, so
`latency_summary()` returns `INVALID` and the verdict says `BLOCKED` instead of
printing a percentile.

This is the same rule the replay report applied to a zero measured on no
sample: an unmeasurable quantity is reported as unmeasurable, not as a number.

**Remedy, in order of preference:**

1. Enable the Windows Time service on this host (needs an elevated shell:
   `Set-Service w32time -StartupType Automatic; Start-Service w32time;
   w32tm /resync`). Then re-run the probe — the measurement becomes valid
   immediately, and the drift term in the evidence file proves it.
2. Run the probe where NTP does run — the Render worker in §5.3 of the plan is
   the host that matters anyway. Latency measured on a developer desktop was
   never the number the plan needs.
3. Do **not** subtract the offset by hand and report a p95. The correction is
   ±1s on a 2s budget.

Until one of the first two happens, the `p95 < 2s` row stays open. It is the
only row in step 1 that is blocked by the host rather than by the channel.

---

## 3. Why the first three probes came back empty

Three 90-second runs at 10:15–10:26Z returned zero messages. That was read as
"quiet channel", which was a guess. (Those three runs were discarded rather
than kept: they were produced before the empty-window verdict was corrected, so
each recorded `catchup: PASS` on a 0-vs-0 sample — a false pass, and exactly the
kind of claim this pass exists to stop.) `scripts/channel_activity.py` replaces
the guess with the measured arrival rate from the local database:

```
channel @XAUUSD_FOREX4: 1824 message(s) 2026-09-13 -> 2026-11-02
  span 49.1d  37.13/day  1.547/hour
  gap median 0.2min  mean 25.4min
  messages per hour, by UTC hour:
    10   4.906  ################################
    11   3.420  ######################
    12   6.209  ########################################   <- peak
    13   4.560  #############################
    15   0.020
    18   0.020
```

The channel is not uniformly quiet: it is **bursty and strongly diurnal**. It
posts 6.2 messages/hour at 12:00 UTC and 0.02/hour at 15:00 and 18:00 UTC. The
earlier probes ran at ~15:45–16:00 UTC, in the dead zone between the London and
New York bursts. They were not unlucky; they were mis-scheduled.

Sizing the window, at 12:00 UTC:

| Window | Expected messages | Use |
|---|---|---|
| 4 minutes | 0.4 | nothing |
| 30 minutes | 3.1 | proves the pipe works, no percentile |
| 4 hours | 24.8 | **adequate** for a p95 over 20 samples |

So the reception row needs a **four-hour soak in the 10:00–14:00 UTC window**,
not another four-minute probe. On this host, with a clock 5.4h slow, "12:00 UTC"
is **06:36 host time** — scheduling by host time would land in the dead zone
again. `channel_activity.py --at-hour 12` prints the expected count for any
proposed window, so this can be checked before a run instead of after.

---

## 4. Our share of the budget, measured without a clock

The end-to-end number needs two clocks. The part we control does not. 20,000
observations pushed through `Recorder.record()` on this host:

| | per message |
|---|---|
| p50 | 0.011 ms |
| p95 | 0.024 ms |
| max | 1.14 ms |

Reproduce with the one-liner in the commit message; the point is the order of
magnitude. **Our handling is ~0.02 ms against a 2000 ms budget** — five orders
of magnitude of headroom. Whatever the end-to-end latency turns out to be, it
is Telegram's delivery plus network transit, not our code. That is worth
knowing before the soak, because it means a failed p95 would implicate the
transport, and no amount of local optimisation would fix it.

The probe now reports this per run as `dispatch` (measured on `received_at` →
write-complete, one clock, `skew_ms=0`), separately from `latency`.

---

## 5. Gaps this pass found in the existing Telegram half

From the static audit (`telegram-audit-2026-11-12T10-15-49Z.json`), unchanged by
this work because fixing them belongs to the hosted observer (§5.3), not to the
probe:

1. **There is no push listener.** `scripts/shadow_collect.py` polls
   `iter_messages(min_id=...)`. Latency has therefore never been measurable
   from stored data — only poll interval is.
2. **No `received_at` column.** `ingested_at` is set on first insert and is
   poll-time, not receipt-time. The hosted schema in §5.2 already fixes this;
   the local table does not.
3. **No `revision` column.** Backfill upserts in place, so an edit destroys the
   previous body. The probe records revisions; the database cannot store them.
4. **Edit detection is bounded to the last 50 ids**, as
   `docs/06-shadow-study-spec.md` states. Older edits are invisible. This pass
   confirms the bound is real: the audit refetches exactly `[-50:]`.

None of the four affects the three passing checks, which are computed in the
probe from the stream and the fetch, not from the table.

---

## 6. What closes step 1

| Row | Needs |
|---|---|
| p95 < 2s | a host whose clock is on UTC **and** a 4h soak at 10:00–14:00 UTC (§2, §3) |
| catch-up, dedupe, edits | **done**, on real message ids |
| TradeLocker quotes / positions / fills | credentials, separate pass, read-only |
| Render → Supabase round trip | the hosted service, §5.2–5.3 |
| stop modification | authorisation, still `BLOCKED` |

The demo gate stays closed. Nothing in this pass touches the broker, and the
exit-policy study remains closed per `docs/16-replay-results-1m.md`.

---

## 7. The live path is still unproven, and the soak that will prove it

Every one of the 85 records so far came from the **fetch** path. The
`NewMessage` handler — the thing that actually makes the observer live rather
than a poller — has never fired on a real message. That gap is more important
than the missing percentile: a percentile on a path that has never delivered
anything would be a number about the wrong thing.

It cannot be closed in a four-minute window, and §3 says why: at 0.02
messages/hour this window would need fifty hours. So the probe gained a **soak
mode** built for the conditions the plan imposes (a free-tier host that
restarts daily):

| Feature | Why |
|---|---|
| `--soak FILE` appends to one rolling `.jsonl` | a 4h listen spans restarts; the record must survive them |
| state rebuilt from that same file on start | a restart must not re-record the window it already holds |
| `*.heartbeat.json` every 30s | a soak that dies silently looks exactly like a quiet channel |
| `*.summary.json` rolling + `telegram-soak-run-<utc>.json` per run | the rolling file is the total; the per-run file is the record of a run that failed |
| `--summary-only` | report the accumulated sample without connecting |

Current state of the soak file: **85 events, 0 of them live**, spanning 7.75
hours of channel history, verdict `no push-delivered message yet - the live
path is still unproven`. That is the correct verdict for the data, and it stays
correct until a message arrives while the listener is up.

A soak was also run with a forced baseline and a reconnect inside it: the
reconnect took 3.9s and `catch_up()` recovered nothing (nothing was posted),
and the subsequent fetch returned the same 85 messages as duplicates —
`new=0, duplicates=85`. The rolling state works.

## 8. Three false passes found by a network outage

A run taken while the link was down produced a summary that was wrong in three
ways at once. Each is now a test.

| Claim | Why it was false | Fix |
|---|---|---|
| `clock: PASS - within tolerance` | `measure_clock_skew_ms()` returned `0.0` when the HTTP request failed, and 0.0 is inside the 2s tolerance | returns `None`; `latency_summary` reports `UNKNOWN`; the verdict says `UNMEASURED - the offset is unknown, not zero` |
| `catchup: FAIL - missed 0, extra 85` | the fetch broke after recording 85 messages, so it returned an empty id set and every recorded message looked like one the history lacked | `_fetch_since` returns `None` on failure; the verdict says `INCOMPLETE - the fetch broke before it finished` |
| `edits: PASS - no edit missed` | the refetch never ran, so "no edit missed" was a statement about nothing | `INCOMPLETE` whenever the fetch did not complete |

The pattern is the same one the replay report had to learn: **an unmeasured
quantity and a measured zero are different things**, and the default must be
"not measured" rather than the comfortable number. All three were introduced by
this work, not inherited, and all three are now pinned by tests
(`test_an_unmeasured_clock_offset_is_not_zero`,
`test_an_incomplete_fetch_is_not_a_set_mismatch`).
