"""Does a live Telegram update actually reach this process?

The milestone could not demonstrate live push: an edit sent while a listener was
connected produced no event, and the report had to say so. That is a timing
problem, not a cosmetic one - if push never arrives, every signal is delivered by
catch-up, and catch-up latency is the reconnect interval.

This probe answers the question directly and separately from the pipeline:

* it registers the same handlers the listener registers, plus a raw handler that
  sees *every* update, so "no event" can be told apart from "the filter did not
  match";
* it posts the message itself through a second session, so there is no race with
  a human typing;
* it records the wall clock at send, at receipt, and Telegram's own date on the
  message, so the latency is measured rather than assumed.

    python scripts/probe_live_push.py
    python scripts/probe_live_push.py --seconds 60 --post
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from rade.ingestion.telegram.client import make_client


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seconds", type=float, default=45.0)
    parser.add_argument("--channel", default=os.getenv("MIRROR_TEST_CHANNEL", ""))
    parser.add_argument("--session", default="rade_session")
    parser.add_argument("--post", action="store_true",
                        help="post a message from a second session while listening")
    parser.add_argument("--text", default="")
    args = parser.parse_args()

    channel = args.channel or os.getenv("TELEGRAM_CHANNEL", "")
    if not channel:
        print("no channel: set MIRROR_TEST_CHANNEL or pass --channel")
        return 2

    seen: list[dict] = []
    client = make_client(args.session)
    await client.connect()
    entity = await client.get_entity(channel)

    from telethon import events as tl_events

    async def on_new(event):
        seen.append({
            "kind": "NewMessage",
            "at": _now(),
            "message_id": event.message.id,
            "date": event.message.date.isoformat() if event.message.date else None,
            "text": (event.message.message or "")[:60],
        })
        print(f"[{_now()}] NewMessage id={event.message.id} "
              f"telegram_date={event.message.date}")

    async def on_edit(event):
        seen.append({"kind": "MessageEdited", "at": _now(),
                     "message_id": event.message.id})
        print(f"[{_now()}] MessageEdited id={event.message.id}")

    async def on_raw(update):
        kind = type(update).__name__
        # Raw updates are noisy on a real account; only report the ones that
        # could be a message in this channel, and count the rest.
        seen.append({"kind": f"raw:{kind}", "at": _now()})
        print(f"[{_now()}] raw update {kind}")

    client.add_event_handler(on_new, tl_events.NewMessage(chats=entity))
    client.add_event_handler(on_edit, tl_events.MessageEdited(chats=entity))
    client.add_event_handler(on_raw, tl_events.Raw())

    print(f"listening on {channel} for {args.seconds:.0f}s; session={args.session}")
    # A high-level request is required before Telegram pushes updates at all.
    me = await client.get_me()
    print(f"authenticated as {getattr(me, 'username', None) or me.id} "
          f"(bot={getattr(me, 'bot', None)})")

    posted: dict | None = None
    if args.post:
        text = args.text or (
            f"live-push probe {datetime.now(timezone.utc).strftime('%H:%M:%S')}")
        # The listener's session is single-writer, so posting uses a copy.
        poster = make_client("data/post_session")
        await poster.connect()
        await asyncio.sleep(3.0)  # let the listener settle after connecting
        sent_at = _now()
        msg = await poster.send_message(await poster.get_entity(channel), text)
        posted = {"sent_at": sent_at, "message_id": msg.id,
                  "date": msg.date.isoformat()}
        print(f"[{sent_at}] posted id={msg.id} from the second session")
        await poster.disconnect()

    deadline = asyncio.get_running_loop().time() + args.seconds
    while asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.5)

    await client.disconnect()

    pushes = [e for e in seen if e["kind"] in {"NewMessage", "MessageEdited"}]
    raws = [e for e in seen if e["kind"].startswith("raw:")]
    print(f"\nevents seen: {len(pushes)} message event(s), {len(raws)} raw update(s)")
    for event in pushes:
        print(f"  {event['kind']} id={event['message_id']} at {event['at']}")
    if not pushes:
        print("  no message event arrived: live push did not reach this process")

    out = Path("docs/evidence") / (
        f"live-push-{datetime.now(timezone.utc).strftime('%Y-%m-%dT%H-%M-%SZ')}.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({
        "at": _now(),
        "channel": channel,
        "session": args.session,
        "seconds": args.seconds,
        "posted": posted,
        "message_events": pushes,
        "raw_updates": len(raws),
        "live_push_observed": bool(pushes),
    }, indent=2), encoding="utf-8")
    print(f"evidence: {out}")
    return 0 if pushes else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
