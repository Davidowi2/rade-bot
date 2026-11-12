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
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from rade.observer.mirror_pipeline import STATUS_CONFLICT, STATUS_MANUAL_REVIEW, STATUS_PAPER_INTENT
from rade.observer.store import make_ledger

REVIEW_STATUSES = [STATUS_MANUAL_REVIEW, STATUS_CONFLICT]
INTENT_STATUSES = [STATUS_PAPER_INTENT]
MAX_LIMIT = 200


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class MirrorService:
    """Request routing and data assembly, independent of the HTTP plumbing."""

    def __init__(self, ledger: Any, *, token: str, heartbeat_path: str | Path | None = None):
        self.ledger = ledger
        self.token = token
        self.heartbeat_path = Path(heartbeat_path) if heartbeat_path else None
        self.started_at = time.time()

    # -- helpers ----------------------------------------------------------
    def listener_state(self) -> dict:
        if not self.heartbeat_path or not self.heartbeat_path.exists():
            return {"known": False, "reason": "no listener heartbeat file"}
        try:
            data = json.loads(self.heartbeat_path.read_text(encoding="utf-8"))
        except (ValueError, OSError) as exc:
            return {"known": False, "reason": f"unreadable heartbeat: {type(exc).__name__}"}
        age = time.time() - self.heartbeat_path.stat().st_mtime
        data["known"] = True
        data["heartbeat_age_s"] = round(age, 1)
        # A heartbeat older than 90s means the listener is not ticking, even if
        # the process is still up. Say so rather than reporting "listening".
        data["stale"] = age > 90
        return data

    def config_summary(self) -> dict:
        """Presence only. Never a value, never a prefix of a value."""
        return {
            "supabase": bool(os.getenv("SUPABASE_URL") and os.getenv("SUPABASE_SERVICE_ROLE_KEY")),
            "notifier": bool(os.getenv("TELEGRAM_BOT_TOKEN") and os.getenv("TELEGRAM_NOTIFY_CHAT_ID")),
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
            return 200, {
                "status": "ok",
                "uptime_s": round(time.time() - self.started_at, 1),
                "time": _now_iso(),
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


def start_listener_if_configured(ledger: Any, log: Any = print) -> Any:
    """Start the Telegram listener beside the API when MIRROR_RUN_LISTENER=1.

    Returns ``None`` when it is not configured, and never raises: the read-only
    API must come up even if the Telegram half cannot.
    """
    if os.getenv("MIRROR_RUN_LISTENER", "").strip() not in {"1", "true", "yes"}:
        return None

    equity = os.getenv("MIRROR_EQUITY", "").strip()
    units = os.getenv("MIRROR_CONTRACT_UNITS_PER_LOT", "").strip()
    if not equity or not units:
        log("listener requested but MIRROR_EQUITY / MIRROR_CONTRACT_UNITS_PER_LOT is missing")
        return None

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

        listener = MirrorListener(
            channel=channel,
            ledger=ledger,
            instrument=InstrumentSpec("XAUUSD", float(units), 0.01, 0.01, 50.0),
            risk=RiskConfig(float(equity), 0.005,
                            float(os.getenv("MIRROR_QUOTE_MAX_AGE_S", "2"))),
            notifier=notifier_from_env(),
            quote_provider=quote_provider,
            state=MirrorState(os.getenv("MIRROR_STATE", "data/mirror_state.json")),
            log=log,
        )
        return start_listener_thread(
            client_factory=make_client,
            listener=listener,
            channel=channel,
            reconnect_every=float(os.getenv("MIRROR_RECONNECT_S", "900")),
            heartbeat_path=Path(os.getenv("MIRROR_HEARTBEAT",
                                          "data/mirror_listener.heartbeat.json")),
            log=log,
        )
    except Exception as exc:
        log(f"listener not started: {type(exc).__name__}: {exc}")
        return None


def main(argv: list[str] | None = None) -> int:
    port = int(os.getenv("PORT", "8000"))
    token = os.getenv("MIRROR_API_TOKEN", "").strip()
    heartbeat = os.getenv("MIRROR_HEARTBEAT", "data/mirror_listener.heartbeat.json")

    if not token:
        print("MIRROR_API_TOKEN is not set; refusing to start a data endpoint without auth",
              file=sys.stderr)
        return 2

    ledger = make_ledger()
    service = MirrorService(ledger, token=token, heartbeat_path=heartbeat)
    handler = make_handler(service, log=lambda line: print(line, flush=True))
    httpd = ThreadingHTTPServer(("0.0.0.0", port), handler)
    print(f"listening on :{port}; read-only; store={type(service.ledger).__name__}", flush=True)

    start_listener_if_configured(ledger)

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
