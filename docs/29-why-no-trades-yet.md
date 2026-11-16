# Why no trade has been placed yet

**Status: 2026-11-16, ~04:00 UTC.** The service is live, executing, connected,
and error-free. It has placed **zero** orders. This is not a malfunction — it is
the safety rules doing exactly what they were written to do. This note records
the evidence so the next decision is made on facts.

## 1. What the live service has actually seen

75 channel messages ingested since the listener started. Every one is accounted
for:

| Outcome | Count | What it is |
|---|---|---|
| `ignored` — not a recognised signal | 60 | VIP advertising, empty messages, market-open hype |
| `ignored` — recap/result post | 10 | Trader result recaps, which are never trade calls |
| `manual_review` | 4 | **Real signals, edited before the bot first saw them** |
| `blocked` | 2 | Real signals refused by the contract-size gate (fixed 2026-11-16) |

Of the 60 "not a recognised signal" messages, **none** contained a signal word
(`XAU`, `GOLD`, `BUY`, `SELL`, `SL`, `TP`, `ENTRY`, `LIMIT`, `NOW`). The parser
is not missing calls. The channel genuinely spends most of its time on
marketing and recaps.

## 2. The real blocker: the channel edits every signal within seconds

All four real signals the bot has ever seen were **first seen in their edited
form**:

```
message 18836  posted 02:02:18  edited 02:02:20  -> manual_review
message 18837  posted 02:02:18  edited 02:02:20  -> manual_review
message 18807  posted 23:07:56  (edited before first sighting) -> manual_review
```

The listener polls every 15 seconds. The channel edits its calls 0–3 seconds
after posting. **The bot therefore never sees an original call** — only the
amended one.

This matches the shadow study exactly: 11 of 11 collected signals were edited
0–3 s after posting, all `original_version_unknown=True`.

`mirror_pipeline.py` refuses an edited call on principle, and that is correct:
the edit may be a stop move, a partial close, or a correction, and the exit
policy for an amendment is unresolved. Copying the amended body as if it were
the original call is the one thing the copier must never do.

**The consequence is that the bot is inert on this channel under the current
rule.** It is not a bug, and it will not fix itself.

### The decision this forces

Three options, and this is a policy decision, not an engineering one:

1. **Keep the rule.** The bot never trades an amended call. Nothing is risked;
   nothing is earned. The channel's behaviour makes the copier unusable.
2. **Trade the edited text as the call.** The trader's final published wording
   is the edited one, and the original was visible for ~2 s — too short for any
   human to have acted on it. This is the only option that makes the bot trade
   on this channel. It must be a deliberate, recorded decision, not a quiet
   relaxation.
3. **Trade it, but only when the edit is provably cosmetic** (formatting or
   emoji, with the tradeable fields unchanged). Not currently possible: the
   original text was never stored, so there is nothing to compare against.

Changing this rule was explicitly reserved for a decision with the lead. It has
not been changed.

## 3. "1 trade per day" — what that setting actually is

There is **no per-day trade count** anywhere in the bot. The "1" that appears on
the dashboard is `MIRROR_MAX_OPEN_POSITIONS` — the maximum number of positions
held **at the same time**.

It is not a throughput knob. `docs/25` §2 records its purpose:

> Total drawdown room is **$162.73** ... At $27 risk per trade, **six losing
> trades leave $0.73 and the seventh breaches the account**. The daily breaker
> (1% ≈ $49) and the max-1-position rule are the controls that stand between a
> bad run and a terminated account.

Raising it to 3 would allow three concurrent $20 positions — up to **$60** of
simultaneous risk, more than the $48.63 daily breaker it sits alongside. That
removes one of the two documented account-protection controls.

**Not changed.** It is a one-line change (`MIRROR_MAX_OPEN_POSITIONS=3` in
Render, and `render.yaml`) and it should be an explicit decision with the
consequence understood — not a side effect of a message sent while asleep.

## 4. Everything else is verified working

| Connection | State |
|---|---|
| Telegram intake | listening, heartbeat 15 s, 0 errors, cursor 18843 |
| Render service | `live_trading=true`, executor enabled, no error |
| TradeLocker destination | BLBRY / account 2346225, instrument 4231, `FULLY_OPEN`, spec verified |
| Sizing | 0.02 lots, $2.00/point, $25 ceiling → 12.5-point stops admitted |
| Supabase | 75 events, cursor row, 4 notifications, `mirror_control` unpaused |
| Telegram notifications | 4 sent, 0 failed |
| Vercel dashboard | 200, auth enforced, API proxy 401 unauthenticated |
| Kill switch | `/api/pause` and `/api/resume` both wired to `mirror_control` |

The bot is ready. It is waiting on a signal it is allowed to copy.
