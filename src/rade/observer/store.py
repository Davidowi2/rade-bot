"""Storage adapters for the mirror.

Two implementations behind one interface:

* :class:`SQLiteIntentLedger` (in ``mirror_pipeline``) for local development
* :class:`SupabaseLedger` for deployment, talking to PostgREST over HTTPS

Both enforce idempotency in the store rather than in the process, so a restart,
a redelivery or two overlapping listeners cannot produce a second intent.

Credentials: the Supabase service-role key is read from the backend environment
only. It is never written to a response body, never logged, and never sent to a
browser. The transport's ``repr`` deliberately omits headers so that a stray
``print(client)`` or a traceback frame cannot leak it.
"""
from __future__ import annotations

import os
from typing import Any, Protocol

import requests

from rade.observer.mirror_pipeline import (
    CLAIM_CONFLICT,
    CLAIM_DUPLICATE,
    CLAIM_INSERTED,
    Ledger,
    MessageEnvelope,
    PipelineResult,
    SQLiteIntentLedger,
    as_utc,
    text_hash,
)

TABLE = "mirror_events"
NOTIFICATION_TABLE = "notification_log"


class StorageError(RuntimeError):
    """A store call failed. The message never contains credentials."""


class Transport(Protocol):
    def request(self, method: str, path: str, *, params: dict | None = None,
                json_body: Any = None, prefer: str | None = None) -> tuple[int, Any]:
        ...


class RequestsTransport:
    """Minimal PostgREST client. Backend only."""

    def __init__(self, base_url: str, api_key: str, *, timeout: float = 15.0):
        if not base_url or not api_key:
            raise StorageError("Supabase URL and service-role key are required")
        self.base_url = base_url.rstrip("/")
        self._api_key = api_key
        self.timeout = timeout
        self._session = requests.Session()

    def __repr__(self) -> str:  # never expose the key
        return f"RequestsTransport(base_url={self.base_url!r})"

    def request(self, method: str, path: str, *, params: dict | None = None,
                json_body: Any = None, prefer: str | None = None) -> tuple[int, Any]:
        url = f"{self.base_url}/rest/v1/{path.lstrip('/')}"
        headers = {
            "apikey": self._api_key,
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        if prefer:
            headers["Prefer"] = prefer
        try:
            resp = self._session.request(
                method, url, params=params, json=json_body,
                headers=headers, timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise StorageError(f"{method} {path} failed: {type(exc).__name__}") from exc
        if resp.status_code >= 400:
            # PostgREST puts the reason in the body; it never echoes the key.
            raise StorageError(f"{method} {path} -> {resp.status_code}: {resp.text[:300]}")
        if not resp.content:
            return resp.status_code, None
        try:
            return resp.status_code, resp.json()
        except ValueError:
            return resp.status_code, resp.text


class SupabaseLedger:
    """PostgREST-backed ledger. Satisfies the same ``Ledger`` contract."""

    def __init__(self, transport: Transport):
        self.transport = transport

    def claim(self, message: MessageEnvelope, result: PipelineResult) -> str:
        row = {
            "event_key": result.event_key,
            "channel": message.channel,
            "message_id": message.message_id,
            "revision": message.revision,
            "posted_at": as_utc(message.posted_at).isoformat(),
            "received_at": as_utc(message.received_at).isoformat(),
            "text_sha256": text_hash(message.text),
            "raw_text": message.text,
            "status": result.status,
            "reason": result.reason,
            "signal_json": result.signal,
            "intent_json": result.order_intent,
            "source": message.source,
        }
        _, body = self.transport.request(
            "POST", TABLE,
            params={"on_conflict": "event_key", "select": "event_key"},
            json_body=row,
            prefer="resolution=ignore-duplicates,return=representation",
        )
        if isinstance(body, list) and body:
            return CLAIM_INSERTED

        # Nothing inserted: the row already exists. Decide duplicate vs conflict
        # from the stored body hash, never from memory.
        _, existing = self.transport.request(
            "GET", TABLE,
            params={"event_key": f"eq.{result.event_key}", "select": "text_sha256"},
        )
        stored = (existing or [{}])[0].get("text_sha256") if isinstance(existing, list) and existing else None
        if stored and stored != row["text_sha256"]:
            return CLAIM_CONFLICT
        return CLAIM_DUPLICATE

    def revision_for(self, channel: str, message_id: int) -> int:
        _, body = self.transport.request(
            "GET", TABLE,
            params={
                "channel": f"eq.{channel}",
                "message_id": f"eq.{message_id}",
                "select": "revision",
                "order": "revision.desc",
                "limit": "1",
            },
        )
        if isinstance(body, list) and body:
            return int(body[0].get("revision") or 0)
        return 0

    def last_message_id(self) -> int:
        """Highest stored message id, so a restart with an empty disk still resumes."""
        _, body = self.transport.request(
            "GET", TABLE,
            params={"select": "message_id", "order": "message_id.desc", "limit": "1"},
        )
        if isinstance(body, list) and body:
            return int(body[0].get("message_id") or 0)
        return 0

    def counts(self) -> dict[str, int]:
        _, body = self.transport.request(
            "GET", TABLE, params={"select": "status", "limit": "5000"},
        )
        counts: dict[str, int] = {}
        for row in body or []:
            status = row.get("status", "unknown")
            counts[status] = counts.get(status, 0) + 1
        return counts

    def recent(self, *, limit: int = 50, statuses: list[str] | None = None) -> list[dict]:
        params: dict[str, Any] = {
            "select": "event_key,channel,message_id,revision,posted_at,received_at,"
                      "status,reason,signal_json,intent_json,source",
            "order": "received_at.desc",
            "limit": str(limit),
        }
        if statuses:
            params["status"] = f"in.({','.join(statuses)})"
        _, body = self.transport.request("GET", TABLE, params=params)
        return body or []

    def record_notification(self, event_key: str, kind: str, ok: bool,
                            detail: str | None = None) -> bool:
        """Log a notification attempt once. Returns False if already logged."""
        _, body = self.transport.request(
            "POST", NOTIFICATION_TABLE,
            params={"on_conflict": "event_key,kind", "select": "id"},
            json_body={"event_key": event_key, "kind": kind, "ok": ok, "detail": detail},
            prefer="resolution=ignore-duplicates,return=representation",
        )
        return bool(isinstance(body, list) and body)


def make_ledger() -> Ledger:
    """Pick the store from the environment: Supabase if configured, else SQLite."""
    url = os.getenv("SUPABASE_URL", "").strip()
    key = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "").strip()
    if url and key:
        return SupabaseLedger(RequestsTransport(url, key))
    path = os.getenv("MIRROR_SQLITE_PATH", "data/observer_intents.sqlite")
    return SQLiteIntentLedger(path)
