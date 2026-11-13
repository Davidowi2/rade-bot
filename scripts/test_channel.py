"""Post, edit and read messages in the private test channel.

The pre-deployment milestone needs fake signals, and fake signals must never be
posted in the trader's channel: a test message there is indistinguishable from a
real call to anything reading it. So this tool refuses to touch the channel in
``TELEGRAM_CHANNEL``, and refuses any chat that is not the configured test
channel.

It uses the same user session as the listener, so what it posts is exactly what
the listener will read - a bot would be a different path and would not prove
anything about the real one.

    python scripts/test_channel.py --list 5
    python scripts/test_channel.py --post "XAUUSD SELL NOW 3915..."
    python scripts/test_channel.py --edit 12 "XAUUSD SELL NOW 3916..."
    python scripts/test_channel.py --delete 12

Nothing here talks to a broker.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import datetime, timezone

from dotenv import load_dotenv

load_dotenv(".env")

from rade.ingestion.telegram.client import make_client  # noqa: E402

#: The channel created for this milestone. It is referenced by username rather
#: than by id: a channel can only be resolved by bare id when the session has it
#: cached, and a cold session - a fresh deploy - does not.
DEFAULT_TEST_CHANNEL = "@rade_mirror_test_20261113"


def _channel() -> str:
    return os.getenv("MIRROR_TEST_CHANNEL", DEFAULT_TEST_CHANNEL).strip()


def _guard(channel: str) -> None:
    """Refuse to write anywhere a real signal could be mistaken for a test."""
    live = (os.getenv("TELEGRAM_CHANNEL") or "").strip().lower()
    if not channel:
        raise SystemExit("no test channel configured (MIRROR_TEST_CHANNEL)")
    if live and channel.lower() == live:
        raise SystemExit(
            f"refusing to write to {channel}: that is TELEGRAM_CHANNEL, the "
            "trader's channel. Test signals go in the test channel only.")
    if channel.lower() == live.lstrip("@"):
        raise SystemExit(
            f"refusing to write to {channel}: that is the trader's channel")


async def run(args) -> int:
    channel = _channel()
    _guard(channel)
    client = make_client()
    await client.connect()
    if not await client.is_user_authorized():
        print("the session is not authorized")
        return 2
    try:
        entity = await client.get_entity(
            int(channel) if channel.lstrip("-").isdigit() else channel)

        if args.list:
            print(f"{entity.title} ({entity.id})")
            async for message in client.iter_messages(entity, limit=args.list):
                stamp = message.date.astimezone(timezone.utc).isoformat()
                body = (message.message or "").replace("\n", " | ")
                edited = " (edited)" if message.edit_date else ""
                print(f"  {message.id} {stamp}{edited} {body[:90]}")
            return 0

        if args.post is not None:
            text = args.post
            if not text.strip():
                print("refusing to post an empty message")
                return 2
            sent = await client.send_message(entity, text)
            print(f"posted id={sent.id} at "
                  f"{sent.date.astimezone(timezone.utc).isoformat()}")
            return 0

        if args.post_file:
            text = open(args.post_file, encoding="utf-8").read()
            sent = await client.send_message(entity, text)
            print(f"posted id={sent.id} at "
                  f"{sent.date.astimezone(timezone.utc).isoformat()}")
            return 0

        if args.edit:
            message_id, _, text = args.edit.partition(":")
            await client.edit_message(entity, int(message_id), text)
            print(f"edited id={message_id}")
            return 0

        if args.delete:
            await client.delete_messages(entity, [int(args.delete)])
            print(f"deleted id={args.delete}")
            return 0

        print("nothing to do; see --help")
        return 2
    finally:
        await client.disconnect()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--list", type=int, default=0, metavar="N")
    parser.add_argument("--post", default=None, metavar="TEXT")
    parser.add_argument("--post-file", default=None, metavar="PATH")
    parser.add_argument("--edit", default=None, metavar="ID:TEXT")
    parser.add_argument("--delete", default=None, metavar="ID")
    args = parser.parse_args()
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())
