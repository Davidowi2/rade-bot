"""Telegram listener for the paper mirror.

The parts that must not be wrong:

* **No loss.** A reconnect is followed by an explicit gap fill
  (``iter_messages(min_id=last_seen)``), not just ``catch_up()``. Telethon's
  catch-up relies on the server keeping the update in its queue; an explicit
  fetch does not.
* **No duplication.** Every message goes through the ledger, whose unique
  constraint on ``(channel, message_id, revision)`` is the only authority.
* **No silent edits.** An edit gets ``revision + 1`` from the ledger, so it is
  stored separately and routed to review. A re-delivered message whose body
  changed under the same revision is reported as a conflict, not applied.
* **No broker writes.** There is no broker object in this module. The only
  broker interaction is an optional read-only quote callable.

The listener is intentionally unaware of exits: it produces intents, not
trades, and no exit policy has been approved.
"""
from __future__ import annotations

import asyncio
import json
import os
import signal
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from rade.observer.mirror_pipeline import (
    InstrumentSpec,
    MarketQuote,
    RiskConfig,
    envelope_from_telegram,
    parse_message_signal,
    process_once,
)
from rade.observer.notify import (
    DEFAULT_ATTEMPTS,
    Notifier,
    notify_result,
    retry_failed_notifications,
)

HEARTBEAT_INTERVAL_S = 30.0

#: How soon to try again after a failed reconnect, instead of waiting out the
#: whole reconnect interval with the intake disconnected.
RECONNECT_RETRY_DELAY_S = 30.0

#: The default first-activation start point: the newest message in the channel
#: at the moment of the first connect. Nothing older is imported.
START_FROM_LATEST = "latest"


def _now() -> datetime:
    return datetime.now(timezone.utc)


class MirrorState:
    """Persisted cursor. A restart resumes instead of replaying the archive."""

    def __init__(self, path: str | Path | None):
        self.path = Path(path) if path else None
        self.last_message_id = 0
        self.runs = 0
        # How the start point was decided ("latest:18786", "id:18048", ...).
        # It is written down so an operator can see where the mirror began.
        self.start_source = ""
        if self.path and self.path.exists():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                self.last_message_id = int(data.get("last_message_id") or 0)
                self.runs = int(data.get("runs") or 0)
                self.start_source = str(data.get("start_source") or "")
            except (ValueError, OSError):
                # A corrupt cursor must not stop the listener; it only means
                # the first catch-up window is larger.
                self.last_message_id = 0

    def advance(self, message_id: int) -> None:
        if message_id <= self.last_message_id:
            return
        self.last_message_id = message_id
        self._write()

    def begin_run(self) -> None:
        self.runs += 1
        self._write()

    def _write(self) -> None:
        if not self.path:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps({
            "last_message_id": self.last_message_id,
            "runs": self.runs,
            "start_source": self.start_source,
            "updated_at": _now().isoformat(),
        }, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)


@dataclass
class MirrorListener:
    channel: str
    ledger: Any
    instrument: InstrumentSpec
    risk: RiskConfig
    notifier: Notifier | None = None
    quote_provider: Callable[[], MarketQuote] | None = None
    state: MirrorState = field(default_factory=lambda: MirrorState(None))
    log: Callable[[str], None] = print
    notify_attempts: int = DEFAULT_ATTEMPTS
    #: Optional demo execution engine. When absent the listener is exactly the
    #: paper mirror it was before: it produces intents and nothing else.
    executor: Any = None

    # -- per message ------------------------------------------------------
    def process_message(self, message: Any, *, revision: int | None = None,
                        source: str = "telegram_live") -> dict:
        """Validate, record and (maybe) notify for one Telegram message."""
        mid = int(message.id)
        if revision is None:
            # 0 for a message never stored; the stored revision for a
            # redelivery, so the ledger reports a duplicate rather than
            # inventing revision 1.
            revision = self.ledger.revision_for(self.channel, mid)

        envelope = envelope_from_telegram(
            message, channel=self.channel, revision=revision, source=source,
        )

        quote: MarketQuote | None = None
        quote_error: str | None = None
        parsed = parse_message_signal(envelope.text)
        if parsed and parsed.get("entry_type") == "market":
            if self.quote_provider is None:
                quote_error = "no read-only quote provider configured"
            else:
                try:
                    quote = self.quote_provider()
                except Exception as exc:  # fail closed, keep the message
                    quote_error = f"{type(exc).__name__}: {exc}"

        result = process_once(
            envelope, ledger=self.ledger, instrument=self.instrument,
            risk=self.risk, quote=quote,
        )

        notification = {"notified": False, "kind": None, "reason": "notifier not configured"}
        if self.notifier is not None:
            notification = notify_result(result, self.notifier, ledger=self.ledger,
                                         source=source, attempts=self.notify_attempts)

        # Execution happens only for a newly stored event. A redelivery is a
        # duplicate and has already been acted on; re-running it would be the
        # one path that could double a position.
        execution: dict | None = None
        if self.executor is not None and not result.duplicate:
            try:
                execution = self.executor.execute(result, envelope)
            except Exception as exc:
                # A broker failure must not lose the message: the event is
                # already stored, and the engine's ledger records the failure.
                execution = {"executed": False,
                             "error": f"{type(exc).__name__}: {exc}"}
                self.log(f"execution error for id={mid}: {execution['error']}")

        payload = {
            **result.to_dict(),
            "channel": self.channel,
            "source": source,
            "quote_error": quote_error,
            "notification": notification,
        }
        if execution is not None:
            payload["execution"] = execution
        self.state.advance(mid)
        self.log(
            f"{source} id={mid} rev={revision} -> {result.status}"
            + (f" ({result.reason})" if result.status != "paper_intent" else "")
        )
        return payload

    # -- notifications ----------------------------------------------------
    def retry_notifications(self, *, limit: int = 20) -> dict:
        """Re-send notifications whose last attempt failed.

        A send that failed is recorded as failed, never as delivered, so this
        pass has something to work with.
        """
        if self.notifier is None:
            return {"considered": 0, "sent": 0, "failed": 0, "skipped": 0}
        summary = retry_failed_notifications(self.ledger, self.notifier, limit=limit)
        if summary.get("sent") or summary.get("failed"):
            self.log(f"notification retry: {summary}")
        return summary

    # -- event handlers ---------------------------------------------------
    async def on_new(self, event: Any) -> dict:
        return self.process_message(event.message, source="telegram_live")

    async def on_edit(self, event: Any) -> dict:
        message = event.message
        stored = self.ledger.revision_for(self.channel, int(message.id))
        return self.process_message(message, revision=stored + 1, source="telegram_edit")


@dataclass
class Runtime:
    """Connection lifecycle: connect, gap fill, periodic reconnect, stop."""

    client: Any
    listener: MirrorListener
    channel: str
    reconnect_every: float = 900.0
    heartbeat_path: Path | None = None
    log: Callable[[str], None] = print
    #: First-activation start point: "latest" (default), a message id, or
    #: "history" (which needs allow_history_import).
    start_at: str | None = None
    allow_history_import: bool = False
    notify_retry_every: float = 300.0

    def __post_init__(self) -> None:
        self.entity = None
        self.started_at = _now()
        self.reconnects = 0
        self.events = 0
        self.errors: list[str] = []
        self._stop = False
        self._last_beat = 0.0
        self.baseline = 0
        # A failed reconnect leaves the process alive but not connected to
        # Telegram. Reporting "listening" then would keep the health check
        # green through an outage.
        self.reconnect_failed = False
        self.last_reconnect_error: str | None = None

    def request_stop(self) -> None:
        self._stop = True

    # -- connection -------------------------------------------------------
    async def connect(self) -> None:
        await self.client.connect()
        if hasattr(self.client, "is_user_authorized") and not await self.client.is_user_authorized():
            raise RuntimeError("Telegram session is not authorised; run the login flow first")
        self.entity = await self.client.get_entity(self.channel)

    async def latest_message_id(self) -> int:
        """The newest message id in the channel, or 0 when the channel is empty."""
        getter = getattr(self.client, "get_messages", None)
        if getter is None:
            # Without this the first activation cannot know where the channel
            # currently ends, and the only alternative left is importing the
            # whole archive. That is exactly what must not happen by accident.
            raise RuntimeError(
                "the Telegram client cannot report the newest message id, so the "
                "first-activation start point cannot be established"
            )
        messages = await getter(self.entity, limit=1)
        if not messages:
            return 0
        return int(getattr(messages[0], "id", 0) or 0)

    async def establish_baseline(self) -> int:
        """Decide where this channel's import starts.

        A fresh install has no cursor, and the channel's history is full of
        old calls. Importing them would turn yesterday's signals into today's
        paper intents and notifications, so the default start point is the
        newest message at the moment of the first connect: nothing older is
        imported.

        An explicit start point can be configured (``MIRROR_START_AT=<id>``),
        and importing the whole history at all takes a second, separate
        switch.
        """
        floor = self.listener.state.last_message_id
        if floor:
            return floor

        setting = (self.start_at or START_FROM_LATEST).strip().lower()
        if setting in {"history", "all"}:
            if not self.allow_history_import:
                raise RuntimeError(
                    "importing the channel history requires "
                    "MIRROR_ALLOW_HISTORY_IMPORT=1"
                )
            self.listener.state.start_source = "history"
            self.log("first activation: importing the whole channel history "
                     "(explicitly enabled); old calls will become intents")
            return 0

        if setting not in {"latest", "now", ""}:
            try:
                floor = int(setting)
            except ValueError:
                raise RuntimeError(
                    "MIRROR_START_AT must be 'latest', 'history' or a message id, "
                    f"not {setting!r}"
                )
            if floor <= 0:
                # Zero is the "no cursor" sentinel, so accepting it would leave
                # the cursor at zero and let the gap fill read the whole
                # archive - the one thing the history switch exists to prevent.
                raise RuntimeError(
                    f"MIRROR_START_AT={setting!r} is not a usable message id; "
                    "it must be positive. Importing the channel history needs "
                    "MIRROR_START_AT=history together with "
                    "MIRROR_ALLOW_HISTORY_IMPORT=1"
                )
            self.listener.state.start_source = f"id:{floor}"
            self.log(f"first activation: importing messages posted after id {floor}")
            try:
                latest = await self.latest_message_id()
            except Exception:
                latest = 0
            if latest and floor > latest:
                self.log(f"WARNING: the start point {floor} is beyond the newest "
                         f"message ({latest}); nothing will be imported until the "
                         "channel passes it")
            return floor

        latest = await self.latest_message_id()
        self.listener.state.start_source = f"latest:{latest}"
        self.log(
            f"first activation: starting from the newest message (id {latest}); "
            "no earlier message will be imported"
        )
        return latest

    async def gap_fill(self) -> list[int]:
        """Fetch every message after the persisted cursor.

        This is the part that makes a reconnect lossless. It is safe to
        over-fetch: the ledger suppresses anything already stored.
        """
        floor = self.listener.state.last_message_id
        seen: list[int] = []
        kwargs: dict[str, Any] = {"reverse": True, "limit": None}
        if floor:
            kwargs["min_id"] = floor
        async for message in self.client.iter_messages(self.entity, **kwargs):
            payload = self._process_safely(message, source="catchup")
            seen.append(int(message.id))
            if payload.get("status") not in {"duplicate"}:
                self.events += 1
        self.save_cursor(self.listener.state.last_message_id)
        return seen

    def _process_safely(self, message: Any, *, source: str,
                        revision: int | None = None) -> dict:
        """Process one message without letting it take the intake down.

        An unexpected exception in one message - a rendering bug, a store
        error - used to propagate out of the loop and stop the listener, which
        means the copier silently stops copying until someone notices the
        heartbeat. A message that cannot be processed is recorded as an error
        and the next one is still handled; the message itself is left
        unclaimed, so a later catch-up will retry it.
        """
        try:
            return self.listener.process_message(
                message, revision=revision, source=source)
        except Exception as exc:
            detail = f"{type(exc).__name__}: {exc}"
            self.errors.append(f"process id={getattr(message, 'id', '?')}: {detail}")
            self.log(f"could not process id={getattr(message, 'id', '?')}: {detail}")
            return {"status": "error", "error": detail}

    def save_cursor(self, message_id: int) -> None:
        """Persist the start point in the store.

        The hosted disk is wiped on every restart, so the cursor has to live
        in the database as well as in the state file.
        """
        if message_id <= 0:
            return
        ledger = self.listener.ledger
        if not hasattr(ledger, "save_cursor"):
            return
        try:
            ledger.save_cursor(self.channel, int(message_id),
                               source=self.listener.state.start_source)
        except Exception as exc:
            self.errors.append(f"cursor save: {type(exc).__name__}: {exc}")

    def restore_cursor(self) -> int:
        """Recover the cursor from the store when the local file is gone.

        A hosted deployment has an ephemeral disk, so the state file disappears
        on every restart. Without this, a restart would refetch the channel's
        entire history; with it, the restart resumes where the database says it
        stopped.
        """
        stored = 0
        ledger = self.listener.ledger
        if hasattr(ledger, "last_message_id"):
            try:
                stored = int(ledger.last_message_id())
            except Exception as exc:
                self.errors.append(f"cursor restore: {type(exc).__name__}: {exc}")
                stored = 0
        if hasattr(ledger, "load_cursor"):
            try:
                stored = max(stored, int(ledger.load_cursor(self.channel)))
            except Exception as exc:
                self.errors.append(f"cursor restore: {type(exc).__name__}: {exc}")
        if stored > self.listener.state.last_message_id:
            self.listener.state.last_message_id = stored
            self.log(f"cursor restored from the store: {stored}")
        return self.listener.state.last_message_id

    async def reconnect(self) -> None:
        self.log("reconnecting")
        try:
            await self.client.disconnect()
        except Exception:
            pass
        await self.connect()
        self.reconnects += 1
        await self.gap_fill()
        self.reconnect_failed = False
        self.last_reconnect_error = None
        self.log("reconnected; gap filled")

    def phase(self) -> str:
        """What monitoring is told about the intake.

        'degraded' means the process is up but not connected to Telegram: the
        reconnect failed and has not yet succeeded again.
        """
        return "degraded" if self.reconnect_failed else "listening"

    # -- heartbeat --------------------------------------------------------
    def heartbeat(self, phase: str) -> None:
        if not self.heartbeat_path:
            return
        now = time.time()
        if phase == "listening" and now - self._last_beat < HEARTBEAT_INTERVAL_S:
            return
        self._last_beat = now
        self.heartbeat_path.parent.mkdir(parents=True, exist_ok=True)
        self.heartbeat_path.write_text(json.dumps({
            "channel": self.channel,
            "phase": phase,
            "beat_at": _now().isoformat(),
            "started_at": self.started_at.isoformat(),
            "elapsed_s": round((_now() - self.started_at).total_seconds(), 1),
            "last_message_id": self.listener.state.last_message_id,
            "start_source": self.listener.state.start_source,
            "events": self.events,
            "reconnects": self.reconnects,
            "reconnect_failed": self.reconnect_failed,
            "last_reconnect_error": self.last_reconnect_error,
            "errors": self.errors[-3:],
        }, indent=2), encoding="utf-8")

    # -- main loop --------------------------------------------------------
    async def run(self, seconds: float | None = None) -> dict:
        from telethon import events as tl_events

        self.heartbeat("connecting")
        await self.connect()
        self.restore_cursor()

        self.client.add_event_handler(
            self.listener.on_new, tl_events.NewMessage(chats=self.entity))
        self.client.add_event_handler(
            self.listener.on_edit, tl_events.MessageEdited(chats=self.entity))

        # The start point is decided before the first fetch. On a fresh install
        # this is what stops the channel's history from being imported as
        # today's calls.
        floor = await self.establish_baseline()
        self.baseline = floor
        if floor:
            self.listener.state.advance(floor)
            self.save_cursor(floor)

        self.log(f"connected; catch-up from id {self.listener.state.last_message_id}")
        await self.gap_fill()

        deadline = None if seconds is None else time.monotonic() + seconds
        next_reconnect = time.monotonic() + self.reconnect_every
        next_notify_retry = time.monotonic() + self.notify_retry_every
        self.heartbeat(self.phase())
        self.log(f"listening; reconnect every {self.reconnect_every:.0f}s; "
                 "no broker write path exists in this process")

        while not self._stop:
            if deadline is not None and time.monotonic() >= deadline:
                break
            if self.reconnect_every > 0 and time.monotonic() >= next_reconnect:
                next_reconnect = time.monotonic() + self.reconnect_every
                try:
                    await self.reconnect()
                except Exception as exc:
                    self.reconnect_failed = True
                    self.last_reconnect_error = f"{type(exc).__name__}: {exc}"
                    self.errors.append(f"reconnect: {type(exc).__name__}: {exc}")
                    self.log(f"reconnect failed: {type(exc).__name__}")
                    # Do not sit out the whole interval disconnected: try
                    # again shortly, and report the intake as degraded until
                    # a reconnect succeeds.
                    next_reconnect = time.monotonic() + RECONNECT_RETRY_DELAY_S
            if self.notify_retry_every > 0 and time.monotonic() >= next_notify_retry:
                next_notify_retry = time.monotonic() + self.notify_retry_every
                try:
                    self.listener.retry_notifications()
                except Exception as exc:
                    self.errors.append(f"notify retry: {type(exc).__name__}: {exc}")
            self.heartbeat(self.phase())
            await asyncio.sleep(0.5)

        self.heartbeat("stopped")
        try:
            self.client.remove_event_handler(self.listener.on_new)
            self.client.remove_event_handler(self.listener.on_edit)
        except Exception:
            pass
        try:
            await self.client.disconnect()
        except Exception:
            pass
        return {
            "events": self.events,
            "reconnects": self.reconnects,
            "errors": self.errors,
            "last_message_id": self.listener.state.last_message_id,
            "stopped_at": _now().isoformat(),
        }


def install_signal_handlers(runtime: Runtime, loop: asyncio.AbstractEventLoop | None = None) -> None:
    """Stop cleanly on SIGTERM/SIGINT so Render's redeploy does not cut a handler."""
    def _handler(signum, _frame):
        runtime.log(f"signal {signum}: stopping after the current message")
        runtime.request_stop()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            (loop or asyncio.get_event_loop()).add_signal_handler(sig, _handler, sig, None)
        except (NotImplementedError, RuntimeError):
            # Windows proactor loop: fall back to the classic handler.
            try:
                signal.signal(sig, _handler)
            except (ValueError, OSError):  # not the main thread
                pass


def start_listener_thread(*, client_factory: Callable[[], Any], listener: MirrorListener,
                          channel: str, reconnect_every: float = 900.0,
                          heartbeat_path: Path | None = None,
                          log: Callable[[str], None] = print,
                          start_at: str | None = None,
                          allow_history_import: bool = False,
                          notify_retry_every: float = 300.0) -> "threading.Thread":
    """Run the listener in a daemon thread beside the HTTP service.

    Render's free tier offers web services only - there is no free background
    worker - so the listener shares the web process. The consequence is honest
    and unavoidable: a spin-down or restart kills the listener, and it resumes
    from the persisted cursor with the database suppressing duplicates. It is
    not a 24/7 listener and this function does not pretend otherwise.
    """
    import threading

    def _run() -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            runtime = Runtime(
                client=client_factory(), listener=listener, channel=channel,
                reconnect_every=reconnect_every, heartbeat_path=heartbeat_path, log=log,
                start_at=start_at, allow_history_import=allow_history_import,
                notify_retry_every=notify_retry_every,
            )
            install_signal_handlers(runtime, loop)
            loop.run_until_complete(runtime.run())
        except Exception as exc:  # the HTTP service must survive a listener failure
            log(f"listener stopped: {type(exc).__name__}: {exc}")
        finally:
            try:
                loop.close()
            except Exception:
                pass

    thread = threading.Thread(target=_run, daemon=True, name="mirror-listener")
    thread.start()
    return thread
