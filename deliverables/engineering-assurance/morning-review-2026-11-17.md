# Morning review — what changed overnight

**Date:** 2026-11-17 (overnight)
**Branch pushed to:** `origin/main`
**Test status:** 578 passed, 6 skipped, 0 failed

Read the first three sections. The rest is detail you can skip unless something
looks wrong.

---

## 1. The short version

Last night you asked for two things: stop blocking good signals, and make the
alerts and logs tell the truth. Both are done and pushed.

Overnight I finished the pieces that were still half-done, and I fixed one thing
that scared me more than the original bug: **a configuration script in this repo
could have switched real trading on just by being run.** That is fixed and there
is now a test that stops it coming back.

Nothing I changed loosens a risk limit. Nothing I changed turns trading on or
off. If you want trading on, you still do it yourself in the Render dashboard.

---

## 2. What is different, in plain English

| # | Change | What it means for you |
|---|---|---|
| 1 | **A slow price read is no longer called a "clock error"** *(done earlier tonight)* | This was the actual reason signals were blocked. The bot blamed a clock problem that never existed. Now a slow read says so, and a genuinely old price still gets refused. |
| 2 | **A failed save no longer freezes the queue** *(done earlier)* | Stale signals were being rejected by the database and retried forever, which is why the dashboard showed nothing. Fixed, plus a safety net so it cannot wedge again. |
| 3 | **The Telegram alert now says what really happened** *(done earlier)* | It used to append "paper only; no broker order was sent" to every message, even on a live account. It now reports the order that was actually placed, or the real reason none was. |
| 4 | **Bid and ask are read as one price, not two** *(new)* | The broker gives the two sides as two separate requests. On a fast market the gap between them could produce a "bid above ask" that never existed, and the bot refused the signal as bad data. It now re-reads once when it sees that. |
| 5 | **The config script can no longer turn trading on** *(new)* | It used to write `MIRROR_EXECUTE=1`. Running a "fix my settings" script could have started placing real orders. It now only reports the value and leaves it alone. |
| 6 | **The blueprint can no longer turn trading off** *(new)* | Same problem from the other side: a deploy would have reset that setting. It is now left to you, in both directions. |
| 7 | **The two config files agree again** *(new)* | They had drifted: the deploy blueprint said a $30 per-trade ceiling, the script said $25, the example file said $25. Also `MIRROR_EQUITY` was declared twice, so the value depended on which line was read last. |
| 8 | **The startup message stops inventing an account number** *(new)* | It printed a bare number even when the broker had not resolved one yet, so an unknown destination looked like a confirmed one. It now says "number not resolved" and shows the account id you pinned. |
| 9 | **A test that compares the two config files** *(new)* | Nothing was checking that they agreed. That is how #7 happened. Now something does. |

---

## 3. What I deliberately did NOT change

These are real issues. I left them because fixing them blind, at night, with you
asleep, was the wrong trade. Each one needs you to decide.

**a) One slow broker call can still stall the listener.**
The listener processes messages one at a time and waits for the broker during
each one. There is no network timeout on those broker calls. If the broker stops
answering, the listener waits. The heartbeat then goes stale and you get an
alert — so it is loud, not silent — but the bot is not trading during that time.
Fixing it properly means adding timeouts and changing how the listener runs
concurrent work. That is a careful, tested change, not an overnight one.

**b) I did not cap how many old messages a reconnect replays.**
I looked at adding a limit, and it would have been dangerous. The fetch order is
newest-first, so a cap would have moved the "last seen" marker to the newest
message and silently skipped everything older. That is worse than the problem.
This needs a tested change to the fetch order first.

**c) Trades marked "in review" may be using up your position allowance.**
You have 5 in review. If those count toward "max open positions", they are
blocking new signals. Worth deciding whether a review item should hold a slot.

**d) The dashboard count can under-report.**
One count reads at most 5000 rows. Not urgent at your volume, but it will lie
once you pass that.

---

## 4. About your dashboard screenshot

You saw: `MODE=live`, 5 in review, 100 ignored, 5 blocked. Nothing there was a
paper-versus-live conflict. `MODE=live` and "paper only" were two different
fields being written by two different pieces of code, and one of them was a
hardcoded string. That is item 3 above — the alert no longer claims paper mode
while placing orders.

The 5 blocked are the signals from the incident. They are recorded with their
reasons; they were not silently dropped.

The 100 ignored are mostly non-signal chatter in the channel (closed-trade notes,
comments). That is normal.

---

## 5. How to check it yourself

Every claim above has a test behind it:

```bash
python -m pytest tests/ -q
```

The ones that matter for tonight:

```bash
python -m pytest tests/test_quote_freshness.py -v   # the blocking bug
python -m pytest tests/test_notification_truth.py -v  # the alert wording
python -m pytest tests/test_book_read.py -v         # bid/ask race
python -m pytest tests/test_config_agreement.py -v  # the config drift
```

I also checked that the config tests actually fail when the bug is put back.
A test that cannot fail is decoration, so I reintroduced each bug, watched the
test go red, and removed it again.

---

## 6. Decisions waiting for you

1. **Do you want the broker timeout work (3a)?** This is the one I would do next.
   It is the difference between "the bot alerts you when the broker hangs" and
   "the bot keeps working".
2. **Should a trade in review hold a position slot (3c)?**
3. **`MIRROR_QUOTE_MAX_AGE_S` is 2 seconds.** After the fix this means "the price
   must not already be 2 seconds old when we read it", which is not the same
   thing it used to mean. 2 is fine for a live feed. If you ever switch to a
   cached price source, this number needs to go up.
4. **Equity in the config is a fixed `4862.73`.** With the executor running, the
   broker's own figure is used, so this does not affect risk. It is only there
   so a fresh deploy cannot fail its startup check.

---

## 7. One thing to watch

`tests/test_features.py::test_future_leak_feature_does_not_use_post_day_bar`
failed once during a full run and then passed on re-run, in isolation, and at
the commit from before tonight's work. It looks time-dependent. It is in the
research code, not the live path, so it cannot block a trade — but a test that
fails once in a while will hide a real failure one day. Worth a look when you
have time.
