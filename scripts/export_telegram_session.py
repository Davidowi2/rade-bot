"""Print a Telethon StringSession so a hosted deployment can stay logged in.

A Render service has an ephemeral disk: the ``.session`` file is wiped on every
redeploy, which would force a new phone login each time. ``TELEGRAM_SESSION``
holds the same credentials in a string instead.

Run this locally, once, and paste the output into the Render environment
variable ``TELEGRAM_SESSION``. It is a credential: treat it like a password and
do not paste it into a chat window.

    python scripts/export_telegram_session.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def main() -> int:
    from dotenv import load_dotenv
    from telethon import TelegramClient
    from telethon.sessions import StringSession

    load_dotenv(ROOT / ".env")
    api_id = int(os.getenv("TELEGRAM_API_ID", "0"))
    api_hash = os.getenv("TELEGRAM_API_HASH", "")
    if not api_id or not api_hash:
        print("TELEGRAM_API_ID / TELEGRAM_API_HASH are not set in .env", file=sys.stderr)
        return 2

    print("A phone login is required. Telegram will send a code to the account.")
    with TelegramClient(StringSession(), api_id, api_hash) as client:
        value = client.session.save()
    print()
    print("TELEGRAM_SESSION (paste into the Render environment; keep it secret):")
    print(value)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
