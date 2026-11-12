"""BotFather notification adapter.

The bot token and the destination chat id come from the backend environment.
They are never returned to a caller, never logged, and never placed in a
response body. Errors are scrubbed of the token before they are surfaced,
because the Telegram endpoint embeds the token in its URL and library
exceptions routinely echo the URL.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Callable, Protocol

import requests

from rade.observer.mirror_pipeline import (
    STATUS_BLOCKED,
    STATUS_CONFLICT,
    STATUS_MANUAL_REVIEW,
    STATUS_PAPER_INTENT,
    STATUS_SKIPPED_MIN_LOT,
    PipelineResult,
)

API_ROOT = "https://api.telegram.org"

#: Which dispositions are worth a phone notification. Ignored and duplicate
#: outcomes are deliberately silent: at 37 messages/day most traffic is noise.
NOTIFY_STATUSES = {
    STATUS_PAPER_INTENT: "accepted",
    STATUS_MANUAL_REVIEW: "review",
    STATUS_CONFLICT: "review",
    STATUS_BLOCKED: "blocked",
    STATUS_SKIPPED_MIN_LOT: "blocked",
}


class Notifier(Protocol):
    def send(self, text: str) -> tuple[bool, str]:
        ...


def _scrub(text: str, secret: str) -> str:
    if secret:
        text = text.replace(secret, "***")
    return text


@dataclass
class BotFatherNotifier:
    """Sends one message through the Bot API. Backend only."""

    token: str
    chat_id: str
    timeout: float = 10.0
    post: Callable[..., object] | None = None

    def __post_init__(self) -> None:
        self._post = self.post or requests.post

    def __repr__(self) -> str:
        return f"BotFatherNotifier(chat_id={self.chat_id!r})"

    def send(self, text: str) -> tuple[bool, str]:
        if not self.token or not self.chat_id:
            return False, "bot token or chat id is not configured"
        try:
            resp = self._post(
                f"{API_ROOT}/bot{self.token}/sendMessage",
                json={"chat_id": self.chat_id, "text": text, "disable_web_page_preview": True},
                timeout=self.timeout,
            )
        except Exception as exc:  # network error; the URL carries the token
            return False, _scrub(f"{type(exc).__name__}: {exc}", self.token)

        ok = getattr(resp, "status_code", 0) == 200
        if ok:
            return True, "sent"
        detail = ""
        try:
            detail = str(resp.text)[:200]
        except Exception:  # pragma: no cover - defensive
            pass
        return False, _scrub(f"HTTP {getattr(resp, 'status_code', '?')}: {detail}", self.token)


class RecordingNotifier:
    """In-memory notifier for tests and dry runs."""

    def __init__(self, ok: bool = True):
        self.ok = ok
        self.sent: list[str] = []

    def send(self, text: str) -> tuple[bool, str]:
        self.sent.append(text)
        return self.ok, "recorded"


def build_notification(result: PipelineResult) -> str | None:
    """Render one message for a disposition, or ``None`` when it is silent."""
    kind = NOTIFY_STATUSES.get(result.status)
    if kind is None:
        return None

    signal = result.signal or {}
    intent = result.order_intent or {}
    header = {
        "accepted": "PAPER INTENT",
        "review": "MANUAL REVIEW",
        "blocked": "BLOCKED",
    }[kind]

    lines = [f"[{header}] {result.status}", result.reason]
    if signal:
        side = str(signal.get("side", "")).upper()
        lines.append(
            f"{side} {signal.get('instrument')} {signal.get('entry_type')} "
            f"entry {signal.get('published_entry')} SL {signal.get('stop_loss')} "
            f"TPs {', '.join(str(x) for x in signal.get('take_profits') or [])}"
        )
    if intent:
        lines.append(
            f"{intent.get('size_lots')} lot, state {intent.get('order_state')}, "
            f"risk budget ${intent.get('risk_budget')}, filled={intent.get('filled')}"
        )
    lines.append("paper only; no broker order was sent")
    return "\n".join(lines)


def notify_result(result: PipelineResult, notifier: Notifier, *,
                  ledger=None, source: str = "") -> dict:
    """Send at most one notification per event, if the disposition warrants it.

    When a ledger is supplied the attempt is recorded under
    ``(event_key, kind)``, whose unique constraint makes a restart idempotent.
    """
    kind = NOTIFY_STATUSES.get(result.status)
    text = build_notification(result)
    if kind is None or text is None:
        return {"notified": False, "kind": None, "reason": "disposition is silent"}

    if source:
        text = f"{text}\nsource: {source}"

    if ledger is not None and hasattr(ledger, "record_notification"):
        # Reserve the slot first: if it was already used, do not send again.
        try:
            reserved = ledger.record_notification(result.event_key, kind, True)
        except Exception as exc:  # store down: better to notify twice than never
            reserved = True
            _ = exc
        if not reserved:
            return {"notified": False, "kind": kind, "reason": "already notified"}

    ok, detail = notifier.send(text)
    return {"notified": ok, "kind": kind, "detail": detail}


def notifier_from_env() -> BotFatherNotifier | None:
    """Build a notifier when both secrets exist; otherwise report the gap."""
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_NOTIFY_CHAT_ID", "").strip()
    if not token or not chat_id:
        return None
    return BotFatherNotifier(token=token, chat_id=chat_id)
