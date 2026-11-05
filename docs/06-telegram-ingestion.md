# 06 — Telegram ingestion

## Source
A public Telegram channel: **"Free trading signal"** (`@XAUUSD_FOREX4`). Read-only. No posting.
Primary learning input is the channel's **historical** messages/trades (backfill),
not only live tailing. Ingestion must backfill from the oldest available message
upward, then incrementally tail new posts.

## Access methods
- Option A: `t.me/s/<channel>` HTML scraping — **NOT viable for this channel**
  (web preview shows only channel header, no messages). Skip.
- Option B: Telethon (MTProto) with a user account session — **required**.
  Supports history backfill, edits/deletes, rate-limited. One-time interactive
  auth (phone + code) by the lead; session file stored locally (gitignored).

## Parsing pipeline
1. Backfill historical messages from the channel (oldest → newest), then tail
   incrementally. Idempotent by (channel, message_id).
2. Store raw (immutable).
3. Parse: extract instrument, direction, entry, SL, TP, rationale, confidence.
   - Start with regex + keyword rules (deterministic, debuggable).
   - Add LLM-assisted parsing only as a fallback/validator, never as sole source.
4. Validate against schema; quarantine unparseable messages for manual review.
5. Align signal to market bars (nearest bar after post time). Note: since we learn
   from history, alignment uses the bar at/after the message timestamp.

## Notes on scope
- v1 follows the trader's instrument (XAUUSD). If the channel posts other
  instruments, those messages are quarantined (not traded) until v2 extends
  support. Do not silently substitute a different symbol.

## Failure modes
- Channel posts images/charts without text → OCR later (out of scope v1).
- Late edits change meaning → keep history, re-derive signals on edit.
- Ambiguous posts → quarantine, don't trade.

## Open
- Language of posts (English?) — to confirm on first backfill.
- Does the trader post entry before or after execution? (affects alignment) —
  user clarified we learn from history, not live-mirror; alignment uses bar at/after
  message timestamp regardless.
