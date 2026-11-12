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
from rade.observer.notify import Notifier, notify_result

HEARTBEAT_INTERVAL_S = 30.0


def _now() -> datetime:
    return datetime.now(timezone.utc)


class MirrorState:
    """Persisted cursor. A restart resumes instead of replaying the archive."""

    def __init__(self, path: str | Path | None):
        self.path = Path(path) if path else None
        self.last_message_id = 0
        self.runs = 0
        if self.path and self.path.exists():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                self.last_message_id = int(data.get("last_message_id") or 0)
                self.runs = int(data.get("runs") or 0)
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
                                         source=source)

        payload = {
            **result.to_dict(),
            "channel": self.channel,
            "source": source,
            "quote_error": quote_error,
            "notification": notification,
        }
        self.state.advance(mid)
        self.log(
            f"{source} id={mid} rev={revision} -> {result.status}"
            + (f" ({result.reason})" if result.status != "paper_intent" else "")
        )
        return payload

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

    def __post_init__(self) -> None:
        self.entity = None
        self.started_at = _now()
        self.reconnects = 0
        self.events = 0
        self.errors: list[str] = []
        self._stop = False
        self._last_beat = 0.0

    def request_stop(self) -> None:
        self._stop = True

    # -- connection -------------------------------------------------------
    async def connect(self) -> None:
        await self.client.connect()
        if hasattr(self.client, "is_user_authorized") and not await self.client.is_user_authorized():
            raise RuntimeError("Telegram session is not authorised; run the login flow first")
        self.entity = await self.client.get_entity(self.channel)

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
            payload = self.listener.process_message(message, source="catchup")
            seen.append(int(message.id))
            if payload["status"] not in {"duplicate"}:
                self.events += 1
        return seen

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
        self.log("reconnected; gap filled")

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
            "events": self.events,
            "reconnects": self.reconnects,
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

        self.log(f"connected; catch-up from id {self.listener.state.last_message_id}")
        await self.gap_fill()

        deadline = None if seconds is None else time.monotonic() + seconds
        next_reconnect = time.monotonic() + self.reconnect_every
        self.heartbeat("listening")
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
                    self.errors.append(f"reconnect: {type(exc).__name__}: {exc}")
                    self.log(f"reconnect failed: {type(exc).__name__}")
            self.heartbeat("listening")
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
                          log: Callable[[str], None] = print) -> "threading.Thread":
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
