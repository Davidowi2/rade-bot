"""BotFather notification adapter.

The bot token and the destination chat id come from the backend environment.
They are never returned to a caller, never logged, and never placed in a
response body. Errors are scrubbed of the token before they are surfaced,
because the Telegram endpoint embeds the token in its URL and library
exceptions routinely echo the URL.
"""
from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass
from typing import Any, Callable, Protocol

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

#: Delay before the second attempt; each further attempt doubles it.
RETRY_BASE_DELAY_S = 0.5

#: How many times a single notification is attempted before it is recorded as
#: failed. A failure is never silently swallowed.
DEFAULT_ATTEMPTS = 3

_HTTP_STATUS_RE = re.compile(r"^HTTP (\d{3})\b")

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


def _retryable(detail: str) -> bool:
    """Whether another attempt could plausibly succeed.

    A 4xx from the Bot API means the request itself is wrong - a malformed
    chat id, a revoked token - and repeating it only wastes time. 429 and 5xx
    are transient, and a transport error is simply unknown, so both are
    retried.
    """
    match = _HTTP_STATUS_RE.match(detail or "")
    if not match:
        return True
    code = int(match.group(1))
    return code == 429 or code >= 500


def send_with_retry(notifier: Notifier, text: str, *,
                    attempts: int = DEFAULT_ATTEMPTS,
                    backoff_s: float = RETRY_BASE_DELAY_S,
                    sleep: Callable[[float], None] = time.sleep) -> tuple[bool, str, int]:
    """Send once, then retry only what a retry could fix.

    Returns ``(ok, detail, tries)``. ``detail`` is the last failure reason, so
    the caller can store why the notification did not arrive.
    """
    tries_allowed = max(1, int(attempts))
    detail = "not attempted"
    for attempt in range(1, tries_allowed + 1):
        ok, detail = notifier.send(text)
        if ok:
            return True, detail, attempt
        if attempt >= tries_allowed or not _retryable(detail):
            break
        sleep(backoff_s * (2 ** (attempt - 1)))
    return False, detail, min(tries_allowed, attempt)


def notify_result(result: PipelineResult, notifier: Notifier, *,
                  ledger=None, source: str = "",
                  attempts: int = DEFAULT_ATTEMPTS,
                  backoff_s: float = RETRY_BASE_DELAY_S,
                  sleep: Callable[[float], None] = time.sleep) -> dict:
    """Send at most one notification per event, if the disposition warrants it.

    The slot is reserved as *pending* before the send and completed with the
    real outcome afterwards. A failed send is therefore recorded as failed and
    stays eligible for a retry; it is never recorded as a success.
    """
    kind = NOTIFY_STATUSES.get(result.status)
    text = build_notification(result)
    if kind is None or text is None:
        return {"notified": False, "kind": None, "reason": "disposition is silent"}

    if source:
        text = f"{text}\nsource: {source}"

    store_error: str | None = None

    if ledger is not None and hasattr(ledger, "notification_status"):
        try:
            status = ledger.notification_status(result.event_key, kind)
        except Exception as exc:  # store down: send anyway, record later
            status = None
            store_error = type(exc).__name__
        if status == "sent":
            return {"notified": False, "kind": kind, "reason": "already notified",
                    "status": "sent", "attempts": 0}

    attempt_no = 0
    if ledger is not None and hasattr(ledger, "begin_notification"):
        try:
            attempt_no = int(ledger.begin_notification(result.event_key, kind))
        except Exception as exc:
            attempt_no = 0
            store_error = store_error or type(exc).__name__

    ok, detail, tries = send_with_retry(notifier, text, attempts=attempts,
                                        backoff_s=backoff_s, sleep=sleep)

    if ledger is not None and hasattr(ledger, "complete_notification"):
        try:
            ledger.complete_notification(result.event_key, kind, ok, detail)
        except Exception as exc:
            # The slot stays 'pending', which a later pass may resend. Losing
            # the notification outright is the worse failure.
            store_error = store_error or type(exc).__name__

    out: dict = {
        "notified": ok,
        "kind": kind,
        "detail": detail,
        "status": "sent" if ok else "failed",
        "attempts": attempt_no or tries,
        "tries": tries,
    }
    if store_error:
        out["store_error"] = store_error
    return out


def _as_json(value: Any) -> Any:
    """PostgREST returns jsonb already decoded; SQLite returns a string."""
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return None
    return value


def result_from_row(row: dict) -> PipelineResult | None:
    """Rebuild the pipeline result from a stored event row.

    A retry does not need the original Telegram message: the stored signal and
    intent are enough to render the same notification.
    """
    status = row.get("event_status")
    if not status:
        # A notification row without its event is an orphan; its own 'status'
        # is the notification state, not a disposition.
        candidate = row.get("status")
        status = candidate if candidate in NOTIFY_STATUSES else None
    if not status:
        return None
    return PipelineResult(
        event_key=row.get("event_key", ""),
        status=status,
        reason=row.get("event_reason") or row.get("reason") or "",
        signal=_as_json(row.get("signal_json")),
        order_intent=_as_json(row.get("intent_json")),
        notification_preview="",
    )


def retry_failed_notifications(ledger: Any, notifier: Notifier, *,
                               limit: int = 20,
                               attempts: int = 2,
                               backoff_s: float = RETRY_BASE_DELAY_S,
                               sleep: Callable[[float], None] = time.sleep) -> dict:
    """Re-send notifications whose last attempt failed.

    Only rows the store still marks as failed are considered, and each one is
    re-recorded with its real outcome, so a retry cannot loop forever.
    """
    summary = {"considered": 0, "sent": 0, "failed": 0, "skipped": 0}
    if ledger is None or not hasattr(ledger, "failed_notifications"):
        return summary
    rows = ledger.failed_notifications(limit=limit) or []
    summary["considered"] = len(rows)
    for row in rows:
        detail = str(row.get("detail") or "")
        if detail and not _retryable(detail):
            # A permanent Bot API error (a 4xx other than 429) will fail again
            # for the same reason. Repeating it every cycle would bury the
            # failures that a retry could actually fix.
            summary["skipped"] += 1
            continue
        result = result_from_row(row)
        text = build_notification(result) if result is not None else None
        if text is None:
            summary["skipped"] += 1
            continue
        ok, detail, _tries = send_with_retry(notifier, text, attempts=attempts,
                                              backoff_s=backoff_s, sleep=sleep)
        try:
            ledger.complete_notification(row["event_key"], row["kind"], ok, detail)
        except Exception:
            pass
        summary["sent" if ok else "failed"] += 1
    return summary


def notifier_from_env() -> BotFatherNotifier | None:
    """Build a notifier when both secrets exist; otherwise report the gap."""
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_NOTIFY_CHAT_ID", "").strip()
    if not token or not chat_id:
        return None
    return BotFatherNotifier(token=token, chat_id=chat_id)
