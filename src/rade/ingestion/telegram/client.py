"""Telegram client helpers."""
from __future__ import annotations

import os

from dotenv import load_dotenv
from telethon import TelegramClient

load_dotenv(".env")


def make_client(session_path: str = "rade_session") -> TelegramClient:
    api_id = int(os.getenv("TELEGRAM_API_ID", "0"))
    api_hash = os.getenv("TELEGRAM_API_HASH", "")
    if not api_id or not api_hash:
        raise RuntimeError("TELEGRAM_API_ID / TELEGRAM_API_HASH not set in .env")

    # A hosted deployment has an ephemeral filesystem: the .session file is
    # gone on every redeploy and the process would need a fresh phone login.
    # TELEGRAM_SESSION holds a StringSession instead, so the login survives.
    session_string = os.getenv("TELEGRAM_SESSION", "").strip()
    if session_string:
        from telethon.sessions import StringSession

        return TelegramClient(StringSession(session_string), api_id, api_hash)

    return TelegramClient(session_path, api_id, api_hash)
