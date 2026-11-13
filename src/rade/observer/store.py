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
from datetime import datetime, timezone
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
CURSOR_TABLE = "mirror_cursor"


class StorageError(RuntimeError):
    """A store call failed. The message never contains credentials."""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


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

    # -- notifications ----------------------------------------------------
    #
    # The slot is reserved as 'pending' before the send and completed with the
    # real outcome afterwards. A boolean written before the send cannot tell a
    # delivery from a failure, and a failed send used to be recorded as a
    # success with nothing left to retry.

    def notification_status(self, event_key: str, kind: str) -> str | None:
        """'sent', 'failed', 'pending', or None when the slot is unused."""
        _, body = self.transport.request(
            "GET", NOTIFICATION_TABLE,
            params={
                "event_key": f"eq.{event_key}",
                "kind": f"eq.{kind}",
                "select": "status",
                "limit": "1",
            },
        )
        if isinstance(body, list) and body:
            return str(body[0].get("status") or "pending")
        return None

    def begin_notification(self, event_key: str, kind: str) -> int:
        """Claim the slot for an attempt and count it."""
        _, row = self.transport.request(
            "GET", NOTIFICATION_TABLE,
            params={
                "event_key": f"eq.{event_key}",
                "kind": f"eq.{kind}",
                "select": "attempts",
                "limit": "1",
            },
        )
        attempts = 0
        if isinstance(row, list) and row:
            attempts = int(row[0].get("attempts") or 0)
        attempts += 1
        self.transport.request(
            "POST", NOTIFICATION_TABLE,
            params={"on_conflict": "event_key,kind"},
            json_body={
                "event_key": event_key,
                "kind": kind,
                "status": "pending",
                "ok": False,
                "attempts": attempts,
                "detail": None,
                "sent_at": _now_iso(),
            },
            prefer="resolution=merge-duplicates,return=representation",
        )
        return attempts

    def complete_notification(self, event_key: str, kind: str, ok: bool,
                              detail: str | None = None) -> bool:
        """Record the real outcome of the attempt."""
        _, body = self.transport.request(
            "PATCH", NOTIFICATION_TABLE,
            params={"event_key": f"eq.{event_key}", "kind": f"eq.{kind}"},
            json_body={
                "status": "sent" if ok else "failed",
                "ok": bool(ok),
                "detail": detail,
                "sent_at": _now_iso(),
            },
            prefer="return=representation",
        )
        return bool(isinstance(body, list) and body)

    def record_notification(self, event_key: str, kind: str, ok: bool,
                            detail: str | None = None) -> bool:
        """Claim the slot and record the outcome in one step.

        False means the slot was already delivered, so nothing was sent.
        """
        if self.notification_status(event_key, kind) == "sent":
            return False
        self.begin_notification(event_key, kind)
        self.complete_notification(event_key, kind, ok, detail)
        return True

    def failed_notifications(self, *, limit: int = 20) -> list[dict]:
        """Notifications whose last attempt failed, oldest first."""
        _, body = self.transport.request(
            "GET", NOTIFICATION_TABLE,
            params={
                "status": "eq.failed",
                "select": "event_key,kind,detail,attempts",
                "order": "sent_at.asc",
                "limit": str(int(limit)),
            },
        )
        rows = body or []
        if not rows:
            return []
        keys = [row["event_key"] for row in rows if row.get("event_key")]
        events: dict[str, dict] = {}
        if keys:
            _, found = self.transport.request(
                "GET", TABLE,
                params={
                    "event_key": f"in.({','.join(keys)})",
                    "select": "event_key,status,reason,signal_json,intent_json,"
                              "channel,message_id",
                },
            )
            for row in found or []:
                # Renamed so the event's status cannot overwrite the
                # notification's own status ('failed').
                events[row.get("event_key")] = {
                    "event_status": row.get("status"),
                    "event_reason": row.get("reason"),
                    "signal_json": row.get("signal_json"),
                    "intent_json": row.get("intent_json"),
                    "channel": row.get("channel"),
                    "message_id": row.get("message_id"),
                }
        merged = []
        for row in rows:
            item = dict(row)
            item.update(events.get(row.get("event_key"), {}))
            merged.append(item)
        return merged

    # -- cursor -----------------------------------------------------------

    def load_cursor(self, channel: str) -> int:
        """The persisted 'start from here' floor for this channel."""
        _, body = self.transport.request(
            "GET", CURSOR_TABLE,
            params={"channel": f"eq.{channel}", "select": "last_message_id", "limit": "1"},
        )
        if isinstance(body, list) and body:
            return int(body[0].get("last_message_id") or 0)
        return 0

    def save_cursor(self, channel: str, message_id: int, *, source: str = "") -> bool:
        """Persist the floor, so an ephemeral disk does not lose it."""
        self.transport.request(
            "POST", CURSOR_TABLE,
            params={"on_conflict": "channel"},
            json_body={
                "channel": channel,
                "last_message_id": int(message_id),
                "start_source": source or "",
                "updated_at": _now_iso(),
            },
            prefer="resolution=merge-duplicates,return=representation",
        )
        return True


def hosted_mode() -> bool:
    """True when the process runs where the local filesystem is disposable.

    Render's free tier wipes the disk on redeploy, restart and spin-down, so a
    SQLite file there is a store that forgets. The deployment host sets
    ``RENDER``; the explicit switch exists for other hosts and for tests.
    """
    if os.getenv("MIRROR_REQUIRE_DURABLE_STORE", "").strip().lower() in {"1", "true", "yes"}:
        return True
    return os.getenv("RENDER", "").strip().lower() in {"1", "true", "yes"}


def make_ledger(*, hosted: bool | None = None) -> Ledger:
    """Pick the store from the environment.

    Supabase when it is configured. On a host with an ephemeral disk the local
    fallback is refused rather than accepted: a store that forgets every
    restart is worse than no store, because it looks like it is working.
    """
    url = os.getenv("SUPABASE_URL", "").strip()
    key = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "").strip()
    if url and key:
        return SupabaseLedger(RequestsTransport(url, key))
    if hosted is None:
        hosted = hosted_mode()
    if hosted:
        raise StorageError(
            "hosted mode requires SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY; "
            "refusing to fall back to the local SQLite file, which this host "
            "wipes on every restart"
        )
    path = os.getenv("MIRROR_SQLITE_PATH", "data/observer_intents.sqlite")
    return SQLiteIntentLedger(path)
