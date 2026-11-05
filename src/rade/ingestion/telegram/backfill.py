"""Backfill Telegram channel history into the database."""
from __future__ import annotations

import asyncio
import json
from datetime import timezone

from sqlalchemy.orm import Session

from rade.ingestion.telegram.client import make_client
from rade.ingestion.telegram.parser import parse_signal
from rade.storage.db import make_engine, make_session_factory
from rade.storage.models import Base, RawTelegramMessage, Signal

CHANNEL = None  # resolved from settings at runtime


def _msg_to_raw(m, channel: str) -> dict:
    try:
        raw = m.to_dict()
    except Exception:
        raw = {"id": m.id, "date": str(m.date), "message": m.message}
    return {
        "channel": channel,
        "message_id": m.id,
        "posted_at": m.date.astimezone(timezone.utc) if m.date else None,
        "edited_at": (m.edit_date.astimezone(timezone.utc) if m.edit_date else None),
        "deleted": 0,
        "text": m.message,
        "raw_json": json.dumps(raw, default=str),
    }


async def backfill(channel: str, limit: int | None = None, since_id: int = 0) -> dict:
    from rade.config.settings import get_settings

    s = get_settings()
    ch = channel or s.telegram_channel
    client = make_client()
    await client.start()
    entity = await client.get_entity(ch)

    engine = make_engine()
    Base.metadata.create_all(engine)
    SessionFactory = make_session_factory()

    total = 0
    signals = 0
    async for m in client.iter_messages(entity, limit=limit, min_id=since_id, reverse=True):
        with SessionFactory() as session:
            # upsert raw
            existing = (
                session.query(RawTelegramMessage)
                .filter_by(channel=ch, message_id=m.id)
                .first()
            )
            data = _msg_to_raw(m, ch)
            if existing is None:
                row = RawTelegramMessage(**data)
                session.add(row)
                session.flush()
                raw_pk = row.id
            else:
                for k, v in data.items():
                    setattr(existing, k, v)
                session.flush()
                raw_pk = existing.id
            session.commit()

            # parse signal (only if text and not already parsed for this message)
            parsed = parse_signal(m.message)
            if parsed is not None and parsed.get("entry_price") is not None:
                # avoid duplicate signals for same message
                dup = session.query(Signal).filter_by(message_id=raw_pk).first()
                if dup is None:
                    sig = Signal(message_id=raw_pk, parser_version="v1-regex", **parsed)
                    session.add(sig)
                    session.commit()
                    signals += 1
        total += 1

    await client.disconnect()
    return {"messages": total, "signals": signals}


def run(channel: str | None = None, limit: int | None = None, since_id: int = 0) -> dict:
    return asyncio.run(backfill(channel or "", limit=limit, since_id=since_id))
