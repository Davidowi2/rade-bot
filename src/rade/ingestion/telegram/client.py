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
    return TelegramClient(session_path, api_id, api_hash)
