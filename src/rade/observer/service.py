"""Read-only HTTP service for the mirror.

Two endpoints are public: ``/healthz`` (for Render's health check) and
``/robots.txt``. Everything under ``/api`` requires a bearer token, because the
data includes the trader's calls and the account's paper sizing.

There is no write endpoint. The only state this process can change is nothing:
every handler is a read against the ledger.

Run locally:
    MIRROR_API_TOKEN=dev .venv/Scripts/python.exe -m rade.observer.service
"""
from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from rade.observer.mirror_pipeline import STATUS_CONFLICT, STATUS_MANUAL_REVIEW, STATUS_PAPER_INTENT
from rade.observer.store import StorageError, hosted_mode, make_ledger

REVIEW_STATUSES = [STATUS_MANUAL_REVIEW, STATUS_CONFLICT]
INTENT_STATUSES = [STATUS_PAPER_INTENT]
MAX_LIMIT = 200

#: A heartbeat older than this means the listener is not ticking, even if the
#: process is still up.
DEFAULT_HEALTH_MAX_AGE_S = 90.0

#: How long after boot a missing heartbeat is still "starting" rather than
#: "dead". A monitor that pings during this window gets an honest 200.
DEFAULT_STARTUP_GRACE_S = 90.0

TRUTHY = {"1", "true", "yes"}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in TRUTHY


@dataclass
class ListenerHandle:
    """What the Telegram half of the service is doing.

    A listener that was requested but did not start is an error. The handle
    carries it so the service can refuse to come up pretending to be healthy.
    """

    expected: bool = False
    thread: Any = None
    error: str | None = None
    heartbeat_path: Path | None = None


class MirrorService:
    """Request routing and data assembly, independent of the HTTP plumbing."""

    def __init__(self, ledger: Any, *, token: str,
                 heartbeat_path: str | Path | None = None,
                 listener_expected: bool = False,
                 listener_thread: Any = None,
                 listener_error: str | None = None):
        self.ledger = ledger
        self.token = token
        self.heartbeat_path = Path(heartbeat_path) if heartbeat_path else None
        self.started_at = time.time()
        self.listener_expected = bool(listener_expected)
        self.listener_thread = listener_thread
        self.listener_error = listener_error
        self.health_max_age_s = float(os.getenv("MIRROR_HEALTH_MAX_AGE_S",
                                                DEFAULT_HEALTH_MAX_AGE_S))
        self.startup_grace_s = float(os.getenv("MIRROR_STARTUP_GRACE_S",
                                                DEFAULT_STARTUP_GRACE_S))

    # -- helpers ----------------------------------------------------------
    def _read_heartbeat(self) -> dict | None:
        if not self.heartbeat_path or not self.heartbeat_path.exists():
            return None
        try:
            data = json.loads(self.heartbeat_path.read_text(encoding="utf-8"))
        except (ValueError, OSError) as exc:
            return {"phase": None, "unreadable": type(exc).__name__}
        return data if isinstance(data, dict) else None

    def listener_state(self) -> dict:
        """Everything a monitor needs to know about the Telegram intake.

        The heartbeat file alone is not enough: a listener thread that died
        leaves a fresh-looking file behind, so the thread's own liveness is
        checked too.
        """
        state: dict[str, Any] = {"expected": self.listener_expected}
        if self.listener_thread is not None:
            state["thread_alive"] = bool(self.listener_thread.is_alive())
        if self.listener_error:
            state["error"] = self.listener_error

        heartbeat = self._read_heartbeat()
        if heartbeat is None:
            state["known"] = False
            state["phase"] = None
            state["heartbeat_age_s"] = None
            state["stale"] = True
        else:
            age = round(time.time() - self.heartbeat_path.stat().st_mtime, 1)
            state.update(heartbeat)
            state["known"] = True
            state["heartbeat_age_s"] = age
            # A heartbeat older than the budget means the listener is not
            # ticking, even if the process is still up.
            state["stale"] = age > self.health_max_age_s

        healthy, reason = self._listener_health(state)
        state["healthy"] = healthy
        state["reason"] = reason
        return state

    def _listener_health(self, state: dict) -> tuple[bool, str]:
        if not self.listener_expected:
            return True, "listener is not enabled"
        if self.listener_error:
            return False, "listener failed to start"
        if state.get("thread_alive") is False:
            return False, "listener thread is not running"
        if not state.get("known"):
            if time.time() - self.started_at < self.startup_grace_s:
                return True, "listener is starting"
            return False, "no listener heartbeat"
        if state.get("stale"):
            return False, "listener heartbeat is stale"
        phase = state.get("phase")
        if phase == "stopped":
            return False, "listener stopped"
        if phase == "connecting":
            # Still working on it: not a failure, but not listening either.
            return True, "listener is connecting"
        return True, "listener is listening"

    def config_summary(self) -> dict:
        """Presence only. Never a value, never a prefix of a value."""
        supabase = bool(os.getenv("SUPABASE_URL") and os.getenv("SUPABASE_SERVICE_ROLE_KEY"))
        return {
            "supabase": supabase,
            "durable_store": supabase or not hosted_mode(),
            "notifier": bool(os.getenv("TELEGRAM_BOT_TOKEN") and os.getenv("TELEGRAM_NOTIFY_CHAT_ID")),
            "contract_units_confirmed": _truthy(os.getenv("MIRROR_CONTRACT_UNITS_CONFIRMED")),
            "broker_writes": False,
            "paper_only": True,
            "exit_policy": "unresolved",
        }

    def counts(self) -> dict:
        try:
            return self.ledger.counts()
        except Exception as exc:
            return {"error": type(exc).__name__}

    # -- routes -----------------------------------------------------------
    def route(self, path: str, query: dict[str, list[str]]) -> tuple[int, dict]:
        limit = min(int((query.get("limit") or ["50"])[0] or 50), MAX_LIMIT)
        statuses = query.get("status")

        if path == "/healthz":
            listener = self.listener_state()
            healthy = bool(listener.get("healthy"))
            return (200 if healthy else 503), {
                "status": "ok" if healthy else "degraded",
                "uptime_s": round(time.time() - self.started_at, 1),
                "time": _now_iso(),
                # Only the intake's state, never the channel, the account or
                # the store. A monitor needs to know whether Telegram intake
                # works; it does not need the trader's calls.
                "listener": {
                    "expected": bool(listener.get("expected")),
                    "healthy": healthy,
                    "phase": listener.get("phase"),
                    "heartbeat_age_s": listener.get("heartbeat_age_s"),
                    "reason": listener.get("reason"),
                },
            }

        if path == "/api/status":
            listener = self.listener_state()
            return 200, {
                "service": "rade-mirror",
                "paper_only": True,
                "broker_writes": False,
                "exit_policy": "unresolved",
                "uptime_s": round(time.time() - self.started_at, 1),
                "time": _now_iso(),
                "counts": self.counts(),
                "listener": listener,
                "config": self.config_summary(),
                "store": type(self.ledger).__name__,
            }

        if path == "/api/events":
            return 200, {"events": self._recent(limit, statuses)}

        if path == "/api/review":
            return 200, {"events": self._recent(limit, statuses or REVIEW_STATUSES)}

        if path == "/api/intents":
            return 200, {"events": self._recent(limit, statuses or INTENT_STATUSES)}

        return 404, {"error": "not found"}

    def _recent(self, limit: int, statuses: list[str] | None) -> list[dict]:
        if not hasattr(self.ledger, "recent"):
            return []
        try:
            return self.ledger.recent(limit=limit, statuses=statuses)
        except Exception as exc:
            return [{"error": type(exc).__name__, "detail": str(exc)[:200]}]


def make_handler(service: MirrorService, *, log: Any = None):
    class Handler(BaseHTTPRequestHandler):
        server_version = "rade-mirror"
        sys_version = ""

        def log_message(self, fmt: str, *args) -> None:  # noqa: A003
            # Default logging includes the request line; that is fine, but the
            # path can carry a query string. Strip it.
            if log is None:
                return
            path = (args[0] if args else "").split("?")[0] if args else ""
            log(f"{self.command} {path} {args[1] if len(args) > 1 else ''}".strip())

        def _send(self, code: int, payload: dict) -> None:
            body = json.dumps(payload, default=str).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def _authorised(self) -> bool:
            if not service.token:
                return False
            header = self.headers.get("Authorization", "")
            return header.startswith("Bearer ") and header[7:].strip() == service.token

        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            path = parsed.path.rstrip("/") or "/"
            query = parse_qs(parsed.query)

            if path == "/robots.txt":
                body = b"User-agent: *\nDisallow: /\n"
                self.send_response(200)
                self.send_header("Content-Type", "text/plain")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

            if path != "/healthz" and not self._authorised():
                self._send(401, {"error": "unauthorised"})
                return

            try:
                code, payload = service.route(path, query)
            except Exception as exc:  # never leak a traceback to a client
                code, payload = 500, {"error": "internal error",
                                      "detail": type(exc).__name__}
            self._send(code, payload)

        def _reject_write(self) -> None:
            self._send(405, {"error": "read-only service; no write endpoints"})

        do_POST = do_PUT = do_PATCH = do_DELETE = _reject_write  # noqa: N815

    return Handler


def start_listener_if_configured(ledger: Any, log: Any = print) -> ListenerHandle:
    """Start the Telegram listener beside the API when MIRROR_RUN_LISTENER=1.

    The handle says what happened. A listener that was requested but did not
    start is an error, not a quiet degradation: the caller refuses to serve a
    health check that claims the mirror is running when it is not.
    """
    if not _truthy(os.getenv("MIRROR_RUN_LISTENER")):
        return ListenerHandle(expected=False)

    heartbeat = Path(os.getenv("MIRROR_HEARTBEAT", "data/mirror_listener.heartbeat.json"))
    handle = ListenerHandle(expected=True, heartbeat_path=heartbeat)

    equity = os.getenv("MIRROR_EQUITY", "").strip()
    units = os.getenv("MIRROR_CONTRACT_UNITS_PER_LOT", "").strip()
    if not equity:
        handle.error = "MIRROR_EQUITY is not set; the paper size would be a guess"
    elif not units:
        handle.error = ("MIRROR_CONTRACT_UNITS_PER_LOT is not set; the contract "
                        "size would be a guess")
    if handle.error:
        log(f"listener not started: {handle.error}")
        return handle

    try:
        from rade.config.settings import get_settings
        from rade.ingestion.telegram.client import make_client
        from rade.observer.listener import MirrorListener, MirrorState, start_listener_thread
        from rade.observer.mirror_pipeline import InstrumentSpec, RiskConfig
        from rade.observer.notify import notifier_from_env
        from rade.observer.quotes import tradelocker_quote_provider

        settings = get_settings()
        channel = os.getenv("MIRROR_CHANNEL") or settings.telegram_channel
        quote_provider = None
        if os.getenv("MIRROR_QUOTE_PROVIDER", "none").strip() == "tradelocker":
            quote_provider = tradelocker_quote_provider()

        # The contract size is only used when a human has confirmed it. Until
        # then every signal is blocked instead of sized from a number nobody
        # has checked.
        confirmed = _truthy(os.getenv("MIRROR_CONTRACT_UNITS_CONFIRMED"))
        if not confirmed:
            log("MIRROR_CONTRACT_UNITS_CONFIRMED is not set: signals will be "
                "recorded as blocked, not sized")

        listener = MirrorListener(
            channel=channel,
            ledger=ledger,
            instrument=InstrumentSpec("XAUUSD", float(units), 0.01, 0.01, 50.0,
                                      units_confirmed=confirmed),
            risk=RiskConfig(float(equity), 0.005,
                            float(os.getenv("MIRROR_QUOTE_MAX_AGE_S", "2"))),
            notifier=notifier_from_env(),
            quote_provider=quote_provider,
            state=MirrorState(os.getenv("MIRROR_STATE", "data/mirror_state.json")),
            log=log,
        )
        handle.thread = start_listener_thread(
            client_factory=make_client,
            listener=listener,
            channel=channel,
            reconnect_every=float(os.getenv("MIRROR_RECONNECT_S", "900")),
            heartbeat_path=heartbeat,
            log=log,
            start_at=os.getenv("MIRROR_START_AT", "latest"),
            allow_history_import=_truthy(os.getenv("MIRROR_ALLOW_HISTORY_IMPORT")),
            notify_retry_every=float(os.getenv("MIRROR_NOTIFY_RETRY_S", "300")),
        )
    except Exception as exc:
        handle.error = f"{type(exc).__name__}: {exc}"
        log(f"listener not started: {handle.error}")
    return handle


def main(argv: list[str] | None = None) -> int:
    port = int(os.getenv("PORT", "8000"))
    token = os.getenv("MIRROR_API_TOKEN", "").strip()
    heartbeat = os.getenv("MIRROR_HEARTBEAT", "data/mirror_listener.heartbeat.json")

    if not token:
        print("MIRROR_API_TOKEN is not set; refusing to start a data endpoint without auth",
              file=sys.stderr)
        return 2

    try:
        ledger = make_ledger()
    except StorageError as exc:
        # A hosted deployment without a durable store would silently write to
        # a disk that is wiped on every restart. Refuse instead.
        print(str(exc), file=sys.stderr)
        return 3

    handle = start_listener_if_configured(ledger)
    if handle.expected and handle.error:
        print(f"the listener was requested but did not start: {handle.error}",
              file=sys.stderr)
        return 3

    service = MirrorService(ledger, token=token, heartbeat_path=heartbeat,
                             listener_expected=handle.expected,
                             listener_thread=handle.thread,
                             listener_error=handle.error)
    handler = make_handler(service, log=lambda line: print(line, flush=True))
    httpd = ThreadingHTTPServer(("0.0.0.0", port), handler)
    print(f"listening on :{port}; read-only; store={type(service.ledger).__name__}",
          flush=True)

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
