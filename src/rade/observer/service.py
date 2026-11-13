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
import threading
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


def _trade_view(row: dict, *, full: bool = False) -> dict:
    """Shape one trade row for the dashboard.

    The view carries the broker's own state - the order and position ids, the
    size, the entry, the stop the broker holds and the targets - rather than a
    reconstruction from the signal. A dashboard that showed what the signal
    asked for instead of what the broker has would be a dashboard that lies.
    """
    plan = row.get("plan") or {}
    if isinstance(plan, str):
        try:
            plan = json.loads(plan)
        except ValueError:
            plan = {}
    targets = row.get("targets") or []
    if isinstance(targets, str):
        try:
            targets = json.loads(targets)
        except ValueError:
            targets = []

    view = {
        "trade_id": row.get("trade_id"),
        "state": row.get("state"),
        "symbol": row.get("symbol"),
        "side": row.get("side"),
        "size_lots": row.get("size"),
        "published_entry": row.get("entry"),
        "fill_price": row.get("fill_price"),
        "initial_stop": row.get("stop"),
        "current_stop": row.get("current_stop"),
        "targets": targets,
        "final_target": plan.get("final_target"),
        "ignored_targets": plan.get("ignored_targets") or [],
        "policy": plan.get("policy"),
        "legs_done": row.get("legs_done"),
        "legs_total": len(plan.get("legs") or []),
        "broker_order_id": row.get("broker_order_id"),
        "broker_position_id": row.get("broker_position_id"),
        "management_stopped": bool(row.get("management_stopped")),
        "note": row.get("note") or "",
        "message_id": row.get("message_id"),
        "revision": row.get("revision"),
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
    }
    if full:
        view["plan"] = plan
        view["event_key"] = row.get("event_key")
        view["channel"] = row.get("channel")
    return view


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
                 listener_error: str | None = None,
                 executor: Any = None,
                 broker_spec: Any = None,
                 execution_error: str | None = None):
        self.ledger = ledger
        self.token = token
        self.heartbeat_path = Path(heartbeat_path) if heartbeat_path else None
        self.started_at = time.time()
        self.listener_expected = bool(listener_expected)
        self.listener_thread = listener_thread
        self.listener_error = listener_error
        #: The demo execution engine, when it is wired. ``None`` means this
        #: process is the paper mirror and nothing else.
        self.executor = executor
        self.broker_spec = broker_spec
        self.execution_error = execution_error
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
        if phase == "degraded":
            # The process is up but the Telegram connection is not: a reconnect
            # failed and has not succeeded again. Saying "listening" here would
            # keep the health check green through an outage.
            return False, "listener reconnect failed"
        if phase == "connecting":
            # Still working on it: not a failure, but not listening either.
            return True, "listener is connecting"
        return True, "listener is listening"

    def config_summary(self) -> dict:
        """Presence only. Never a value, never a prefix of a value."""
        supabase = bool(os.getenv("SUPABASE_URL") and os.getenv("SUPABASE_SERVICE_ROLE_KEY"))
        executing = self.executor is not None
        return {
            "supabase": supabase,
            "durable_store": supabase or not hosted_mode(),
            "notifier": bool(os.getenv("TELEGRAM_BOT_TOKEN") and os.getenv("TELEGRAM_NOTIFY_CHAT_ID")),
            "contract_units_confirmed": _truthy(os.getenv("MIRROR_CONTRACT_UNITS_CONFIRMED")),
            # The paper mirror and the demo executor are different states and
            # the status says which one this process is in. There is no live
            # account path either way.
            "broker_writes": executing,
            "paper_only": not executing,
            "account_scope": "demo" if executing else "none",
            "live_trading": False,
            "exit_policy": "tp_ladder" if executing else "unresolved",
        }

    def execution_state(self) -> dict:
        """What the demo execution path is doing, for the dashboard."""
        state: dict[str, Any] = {
            "enabled": self.executor is not None,
            "account_scope": "demo",
            "live_trading": False,
        }
        if self.execution_error:
            state["error"] = self.execution_error
        spec = self.broker_spec
        if spec is not None:
            state["broker"] = {
                "environment": spec.environment,
                "symbol": spec.symbol,
                "instrument_id": spec.instrument_id,
                "trade_route_id": spec.trade_route_id,
                "account_number": spec.account_number,
                "contract_units_per_lot": spec.contract_units_per_lot,
                "lot_step": spec.lot_step,
                "min_lot": spec.min_lot,
                "max_lot": spec.max_lot,
                "symbol_status": spec.symbol_status,
                "verified": spec.verified,
                "unconfirmed_reason": spec.unconfirmed_reason,
            }
        return state

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
                "paper_only": self.executor is None,
                "broker_writes": self.executor is not None,
                "account_scope": "demo" if self.executor is not None else "none",
                "live_trading": False,
                "exit_policy": "tp_ladder" if self.executor is not None else "unresolved",
                "uptime_s": round(time.time() - self.started_at, 1),
                "time": _now_iso(),
                "counts": self.counts(),
                "listener": listener,
                "execution": self.execution_state(),
                "config": self.config_summary(),
                "store": type(self.ledger).__name__,
            }

        if path == "/api/trades":
            return 200, {"trades": self._trades(limit)}

        if path.startswith("/api/trades/"):
            trade_id = path[len("/api/trades/"):].strip()
            if not trade_id:
                return 404, {"error": "not found"}
            return self._trade_detail(trade_id)

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

    # -- trades -----------------------------------------------------------
    #
    # These read the trade rows the execution engine writes. Nothing here can
    # change a trade: the service is still read-only.

    def _trades(self, limit: int) -> list[dict]:
        if not hasattr(self.ledger, "list_trades"):
            return []
        try:
            rows = self.ledger.list_trades()
        except Exception as exc:
            return [{"error": type(exc).__name__, "detail": str(exc)[:200]}]
        # Newest first, so the dashboard shows the live trade at the top.
        rows = sorted(rows, key=lambda row: str(row.get("created_at") or ""), reverse=True)
        return [_trade_view(row) for row in rows[:limit]]

    def _trade_detail(self, trade_id: str) -> tuple[int, dict]:
        if not hasattr(self.ledger, "get_trade"):
            return 404, {"error": "not found"}
        try:
            row = self.ledger.get_trade(trade_id)
        except Exception as exc:
            return 500, {"error": "internal error", "detail": type(exc).__name__}
        if row is None:
            return 404, {"error": "not found"}
        transitions = []
        if hasattr(self.ledger, "transitions"):
            try:
                transitions = [
                    {
                        "stage": item.get("stage"),
                        "status": item.get("status"),
                        "detail": item.get("detail"),
                        "broker_order_id": item.get("broker_order_id"),
                        "broker_position_id": item.get("broker_position_id"),
                        "created_at": item.get("created_at"),
                    }
                    for item in self.ledger.transitions(trade_id)
                ]
            except Exception as exc:
                transitions = [{"error": type(exc).__name__}]
        return 200, {"trade": _trade_view(row, full=True), "transitions": transitions}


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


def start_listener_if_configured(ledger: Any, log: Any = print,
                                 executor: Any = None) -> ListenerHandle:
    """Start the Telegram listener beside the API when MIRROR_RUN_LISTENER=1.

    The handle says what happened. A listener that was requested but did not
    start is an error, not a quiet degradation: the caller refuses to serve a
    health check that claims the mirror is running when it is not.
    """
    if not _truthy(os.getenv("MIRROR_RUN_LISTENER")):
        return ListenerHandle(expected=False)

    heartbeat = Path(os.getenv("MIRROR_HEARTBEAT", "data/mirror_listener.heartbeat.json"))
    handle = ListenerHandle(expected=True, heartbeat_path=heartbeat)

    equity_raw = os.getenv("MIRROR_EQUITY", "").strip()
    units_raw = os.getenv("MIRROR_CONTRACT_UNITS_PER_LOT", "").strip()
    confirmed = _truthy(os.getenv("MIRROR_CONTRACT_UNITS_CONFIRMED"))

    # The sizing inputs are required only when sizing is possible. While the
    # contract size is unconfirmed the listener still starts and records every
    # message as blocked, which is worth more than refusing to start and
    # archiving nothing at all.
    if confirmed and not units_raw:
        handle.error = ("MIRROR_CONTRACT_UNITS_PER_LOT is required once the "
                        "contract size is confirmed; it is never guessed")
    elif confirmed and not equity_raw and executor is None:
        # With the execution path running the equity is the broker's, so the
        # configured value is not required and is not used.
        handle.error = ("MIRROR_EQUITY is required once the contract size is "
                        "confirmed; it is never guessed")
    if handle.error:
        log(f"listener not started: {handle.error}")
        return handle

    # Validate the start point before the thread exists, so a bad value is a
    # startup failure with a readable reason rather than a thread that dies
    # silently a second later. The runtime refuses it too.
    start_at = os.getenv("MIRROR_START_AT", "latest").strip()
    if start_at.lower() not in {"latest", "now", "", "history", "all"}:
        try:
            usable = int(start_at) > 0
        except ValueError:
            usable = False
        if not usable:
            handle.error = (f"MIRROR_START_AT={start_at!r} is not usable; it must be "
                            "'latest', 'history' or a positive message id")
            log(f"listener not started: {handle.error}")
            return handle

    try:
        from rade.config.settings import get_settings
        from rade.ingestion.telegram.client import make_client
        from rade.observer.listener import MirrorListener, MirrorState, start_listener_thread
        from rade.observer.mirror_pipeline import InstrumentSpec, MarketQuote, RiskConfig
        from rade.observer.notify import notifier_from_env
        from rade.observer.quotes import tradelocker_quote_provider

        settings = get_settings()
        channel = os.getenv("MIRROR_CHANNEL") or settings.telegram_channel
        quote_provider = None
        if executor is not None and getattr(executor, "broker", None) is not None:
            # One client, one session: the listener asks the execution
            # gateway for the quote so the price a signal is sized against is
            # the same price the order will be sent into.
            gateway = executor.broker

            def _gateway_quote() -> MarketQuote:
                current = gateway.quote()
                return MarketQuote(current.bid, current.ask, current.quoted_at)

            quote_provider = _gateway_quote
        elif os.getenv("MIRROR_QUOTE_PROVIDER", "none").strip() == "tradelocker":
            quote_provider = tradelocker_quote_provider()

        # The contract size is only used when a human has confirmed it. Until
        # then every signal is blocked instead of sized from a number nobody
        # has checked.
        units = float(units_raw) if units_raw else 0.0
        equity = float(equity_raw) if equity_raw else 0.0
        if not confirmed:
            log("MIRROR_CONTRACT_UNITS_CONFIRMED is not set: signals will be "
                "recorded as blocked, not sized")

        # With the execution path running, the lot step, the minimum and the
        # maximum come from the broker rather than from literals in this file.
        # A hardcoded step cannot notice that the broker changed it.
        if executor is not None and getattr(executor, "broker", None) is not None:
            broker_spec = executor.broker.spec()
            instrument = InstrumentSpec(
                broker_spec.symbol,
                broker_spec.contract_units_per_lot,
                broker_spec.lot_step,
                broker_spec.min_lot,
                broker_spec.max_lot,
                units_confirmed=broker_spec.verified,
            )
            # The equity a new trade is sized against is the account's, read
            # from the broker. It is read once at startup: a large drift in
            # P&L is not re-read, which is recorded as a limitation.
            try:
                equity = executor.broker.equity()
                log(f"sizing equity from the broker: {equity:.2f}")
            except Exception as exc:
                equity = 0.0
                log(f"could not read the account equity: {type(exc).__name__}")
        else:
            instrument = InstrumentSpec("XAUUSD", units, 0.01, 0.01, 50.0,
                                        units_confirmed=confirmed)

        listener = MirrorListener(
            channel=channel,
            ledger=ledger,
            instrument=instrument,
            risk=RiskConfig(equity, 0.005,
                            float(os.getenv("MIRROR_QUOTE_MAX_AGE_S", "2"))),
            notifier=notifier_from_env(),
            quote_provider=quote_provider,
            state=MirrorState(os.getenv("MIRROR_STATE", "data/mirror_state.json")),
            log=log,
            executor=executor,
        )
        handle.thread = start_listener_thread(
            client_factory=make_client,
            listener=listener,
            channel=channel,
            reconnect_every=float(os.getenv("MIRROR_RECONNECT_S", "900")),
            heartbeat_path=heartbeat,
            log=log,
            start_at=start_at,
            allow_history_import=_truthy(os.getenv("MIRROR_ALLOW_HISTORY_IMPORT")),
            notify_retry_every=float(os.getenv("MIRROR_NOTIFY_RETRY_S", "300")),
        )
    except Exception as exc:
        handle.error = f"{type(exc).__name__}: {exc}"
        log(f"listener not started: {handle.error}")
    return handle


def start_executor_if_configured(ledger: Any, log: Any = print) -> "ExecutorHandle":
    """Start the demo execution path when MIRROR_EXECUTE=1.

    Three things happen in this order, and the order is the point:

    1. The broker specification is resolved and **verified**. An unconfirmed
       contract size stops the executor here: the service says why instead of
       starting something that would block every signal.
    2. The trades in the store are reconciled against the broker *before* the
       listener is allowed to act. A restart therefore adopts what the broker
       already has instead of placing a second order.
    3. Only then does the management loop start.
    """
    handle = ExecutorHandle()
    if not _truthy(os.getenv("MIRROR_EXECUTE")):
        return handle

    try:
        from rade.execution.engine import ExecutionEngine
        from rade.execution.gateway import tradelocker_gateway_from_env
        from rade.observer.notify import notifier_from_env

        confirmed = _truthy(os.getenv("MIRROR_CONTRACT_UNITS_CONFIRMED"))
        units_raw = os.getenv("MIRROR_CONTRACT_UNITS_PER_LOT", "").strip()
        confirmed_units = float(units_raw) if (confirmed and units_raw) else None

        gateway = tradelocker_gateway_from_env(
            confirmed_contract_units=confirmed_units, log=log)
        handle.spec = gateway.spec()
        if not handle.spec.verified:
            handle.error = (
                "the broker specification is not verified: "
                f"{handle.spec.unconfirmed_reason}")
            log(f"executor not started: {handle.error}")
            return handle

        engine = ExecutionEngine(
            broker=gateway,
            ledger=ledger,
            notifier=notifier_from_env(),
            log=log,
            manage_every_s=float(os.getenv("MIRROR_MANAGE_EVERY_S", "5")),
            fill_timeout_s=float(os.getenv("MIRROR_FILL_TIMEOUT_S", "300")),
        )
        log(f"execution reconciliation on startup: {engine.reconcile()}")
        handle.executor = engine
        handle.stop_event = threading.Event()
        handle.thread = threading.Thread(
            target=_management_loop,
            args=(engine, handle.stop_event, log, engine.manage_every_s),
            name="rade-execution-manager",
            daemon=True,
        )
        handle.thread.start()
        log("demo execution path started; account scope is demo only")
    except Exception as exc:
        handle.error = f"{type(exc).__name__}: {exc}"
        log(f"executor not started: {handle.error}")
    return handle


@dataclass
class ExecutorHandle:
    """What the execution half of the service is doing."""

    executor: Any = None
    spec: Any = None
    error: str | None = None
    thread: Any = None
    stop_event: Any = None


def _management_loop(engine: Any, stop_event: Any, log: Any, interval_s: float) -> None:
    """Manage open trades on a timer until the process stops."""
    while not stop_event.wait(max(1.0, float(interval_s))):
        try:
            summary = engine.manage()
        except Exception as exc:
            log(f"management pass failed: {type(exc).__name__}: {exc}")
            continue
        if summary.get("closed") or summary.get("partial") or summary.get("errors"):
            log(f"management: {summary}")


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

    # The execution path is started *before* the listener, so that a restart
    # reconciles against the broker before a new signal can act on anything.
    execution = start_executor_if_configured(ledger)
    if _truthy(os.getenv("MIRROR_EXECUTE")) and execution.error:
        print(f"execution was requested but did not start: {execution.error}",
              file=sys.stderr)
        return 3

    handle = start_listener_if_configured(ledger, executor=execution.executor)
    if handle.expected and handle.error:
        print(f"the listener was requested but did not start: {handle.error}",
              file=sys.stderr)
        return 3

    service = MirrorService(ledger, token=token, heartbeat_path=heartbeat,
                             listener_expected=handle.expected,
                             listener_thread=handle.thread,
                             listener_error=handle.error,
                             executor=execution.executor,
                             broker_spec=execution.spec,
                             execution_error=execution.error)
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
