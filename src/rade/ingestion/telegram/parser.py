"""Regex-based parser for the channel's signal messages."""
from __future__ import annotations

import json
import re
from typing import Any

INSTRUMENT_RE = re.compile(r"(XAUUSD|GOLD|XAU/USD)", re.I)
DIR_RE = re.compile(r"\b(BUY|SELL)\b", re.I)
# entry price: number following NOW or directly after direction token
ENTRY_RE = re.compile(r"(?:BUY|SELL)\s*(?:LIMIT|MARKET|NOW|ENTRY|AT)?\s*[:=]?\s*(\d{3,5}(?:\.\d+)?)", re.I)
SL_RE = re.compile(r"(?:SL|STOP(?:\s*LOSS)?)\s*[:=]?\s*(\d{3,5}(?:\.\d+)?)", re.I)
TP_RE = re.compile(r"(?:TP|TARGET(?:\s*\d*)?|TAKE\s*PROFIT)\s*[:=]?\s*(\d{3,5}(?:\.\d+)?)", re.I)


def parse_signal(text: str | None) -> dict[str, Any] | None:
    if not text:
        return None
    t = text.strip()
    dir_m = DIR_RE.search(t)
    if not dir_m:
        return None
    direction = dir_m.group(1).lower()
    # avoid result/TP-hit messages: if message is just "+70 pip" style, skip
    if re.search(r"\+\d+\s*pip", t, re.I) and "NOW" not in t.upper() and "SL" not in t.upper():
        return None

    instrument = "XAUUSD"
    m = INSTRUMENT_RE.search(t)
    if m and m.group(1).upper() in ("GOLD", "XAU/USD"):
        instrument = "XAUUSD"

    entry_m = ENTRY_RE.search(t)
    entry_price = float(entry_m.group(1)) if entry_m else None

    sl_m = SL_RE.search(t)
    stop_loss = float(sl_m.group(1)) if sl_m else None

    tps = [float(x) for x in TP_RE.findall(t)]
    # dedupe while preserving order
    seen = set()
    tp_list = []
    for v in tps:
        if v not in seen:
            seen.add(v)
            tp_list.append(v)

    # confidence heuristic
    score = 0.2
    if entry_price is not None:
        score += 0.4
    if stop_loss is not None:
        score += 0.2
    if tp_list:
        score += 0.2

    return {
        "instrument": instrument,
        "direction": direction,
        "entry_type": "market" if "NOW" in t.upper() else "limit",
        "entry_price": entry_price,
        "stop_loss": stop_loss,
        "take_profit": json.dumps(tp_list) if tp_list else None,
        "rationale": t[:500],
        "confidence": round(min(score, 1.0), 2),
    }
