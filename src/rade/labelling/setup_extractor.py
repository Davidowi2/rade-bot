"""Strict setup extractor for the trader's channel messages.

Grammar (one setup per message, first entry line wins):

    XAUUSD (BUY|SELL) (NOW|LIMIT) <price>
    Sl : <price>
    Tp : <price>          (repeated; "Tp : open" is allowed and ignored)
    ...

Anything that does not match the entry line is rejected.
"""
from __future__ import annotations

import re
from typing import Optional

from .models import Setup

ENTRY_RE = re.compile(
    r"XAUUSD\s+(BUY|SELL)\s+(NOW|LIMIT)\s+(\d{3,5}(?:\.\d+)?)",
    re.IGNORECASE,
)
SL_RE = re.compile(r"Sl\s*:\s*(\d{3,5}(?:\.\d+)?)", re.IGNORECASE)
TP_RE = re.compile(r"Tp\s*:\s*(\d{3,5}(?:\.\d+)?)", re.IGNORECASE)


def extract_setups_from_text(
    text: str | None,
    message_pk: int,
    posted_at,
    instrument: str = "XAUUSD",
) -> list[Setup]:
    """Extract setups from a single message.

    Returns a list with at most one Setup (the first entry line). Multi-entry
    messages are intentionally collapsed to the first setup to match the
    reconnaissance dataset (2,382 setups). Multi-setup extraction is deferred
    to a later phase.
    """
    if not text:
        return []
    m = ENTRY_RE.search(text)
    if not m:
        return []

    direction = m.group(1).lower()
    entry_type = "market" if m.group(2).upper() == "NOW" else "limit"
    entry_price = float(m.group(3))

    sl_m = SL_RE.search(text)
    stop_loss = float(sl_m.group(1)) if sl_m else None

    take_profits: list[float] = []
    seen: set[float] = set()
    for v in TP_RE.findall(text):
        f = float(v)
        if f not in seen:
            seen.add(f)
            take_profits.append(f)

    setup = Setup(
        message_pk=message_pk,
        posted_at=posted_at,
        instrument=instrument,
        direction=direction,
        entry_type=entry_type,
        entry_price=entry_price,
        stop_loss=stop_loss,
        take_profits=take_profits,
        raw_text=text[:500],
    )
    return [setup]


def extract_all(raw_rows) -> list[Setup]:
    """Extract setups from an iterable of (message_pk, posted_at, text) rows."""
    out: list[Setup] = []
    for message_pk, posted_at, text in raw_rows:
        out.extend(extract_setups_from_text(text, message_pk, posted_at))
    return out
