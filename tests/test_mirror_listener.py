"""Listener behaviour: duplicates, edits, gap fill, restart state, soak guard.

No network. The Telegram client is faked with the three methods the runtime
uses, so these tests exercise the logic that decides what gets stored.
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from rade.observer.listener import MirrorListener, MirrorState, Runtime
from rade.observer.mirror_pipeline import (
    InstrumentSpec,
    MarketQuote,
    RiskConfig,
    SQLiteIntentLedger,
)

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 11, 12, 12, 0, tzinfo=timezone.utc)
INSTRUMENT = InstrumentSpec("XAUUSD", 100, 0.01, 0.01, 50, units_confirmed=True)
RISK = RiskConfig(10_000, 0.005, 2.0)
SELL = "XAUUSD SELL NOW 3915\nSl : 3925\nTp : 3910\nTp : 3905\nTp : 3900"


def msg(mid=100, text=SELL, *, at=NOW, edited=None):
    return SimpleNamespace(id=mid, date=at, message=text, edit_date=edited)


def quote_now(bid=3915.0, ask=3915.0):
    # A real quote is stamped when it is fetched, which is after the message
    # arrives. Stamping it with the message time would (correctly) look stale.
    return lambda: MarketQuote(bid, ask, datetime.now(timezone.utc))


def make_listener(tmp_path, *, quote=None, notifier=None):
    ledger = SQLiteIntentLedger(tmp_path / "l.sqlite")
    return MirrorListener(
        channel="@source",
        ledger=ledger,
        instrument=INSTRUMENT,
        risk=RISK,
        notifier=notifier,
        quote_provider=quote,
        state=MirrorState(tmp_path / "state.json"),
        log=lambda *_: None,
    )


class FakeClient:
    def __init__(self, messages):
        self.messages = list(messages)
        self.connected = False
        self.handlers = []
        self.iter_calls = 0

    async def connect(self):
        self.connected = True

    async def disconnect(self):
        self.connected = False

    async def is_user_authorized(self):
        return True

    async def get_entity(self, channel):
        return channel

    async def get_messages(self, entity, **kwargs):
        """The newest messages, newest first - what Telethon returns."""
        limit = kwargs.get("limit") or 1
        newest = sorted(self.messages, key=lambda m: m.id, reverse=True)[:limit]
        return list(newest)

    async def iter_messages(self, entity, **kwargs):
        self.iter_calls += 1
        floor = kwargs.get("min_id") or 0
        for message in self.messages:
            if message.id > floor:
                yield message

    def add_event_handler(self, fn, event):
        self.handlers.append(fn)

    def remove_event_handler(self, fn):
        if fn in self.handlers:
            self.handlers.remove(fn)


class NoLatestClient(FakeClient):
    """A client that cannot say where the channel currently ends."""

    get_messages = None


# --------------------------------------------------------------------------
# one message
# --------------------------------------------------------------------------

def test_market_signal_without_a_quote_is_blocked_but_stored(tmp_path):
    listener = make_listener(tmp_path)
    out = listener.process_message(msg())
    assert out["status"] == "blocked"
    assert out["quote_error"] == "no read-only quote provider configured"
    assert listener.ledger.counts() == {"blocked": 1}


def test_market_signal_with_a_quote_becomes_a_paper_intent(tmp_path):
    listener = make_listener(tmp_path, quote=quote_now(3915.1, 3915.3))
    out = listener.process_message(msg())
    assert out["status"] == "paper_intent"
    assert out["order_intent"]["filled"] is False
    assert out["order_intent"]["size_lots"] == 0.05


def test_a_failing_quote_provider_fails_closed(tmp_path):
    def boom():
        raise RuntimeError("broker unreachable")

    listener = make_listener(tmp_path, quote=boom)
    out = listener.process_message(msg())
    assert out["status"] == "blocked"
    assert "broker unreachable" in out["quote_error"]


def test_redelivery_is_a_duplicate(tmp_path):
    listener = make_listener(tmp_path, quote=quote_now())
    first = listener.process_message(msg())
    second = listener.process_message(msg())
    assert first["status"] == "paper_intent"
    assert second["status"] == "duplicate"
    assert listener.ledger.counts() == {"paper_intent": 1}


def test_a_redelivered_message_with_a_changed_body_is_a_conflict(tmp_path):
    listener = make_listener(tmp_path, quote=quote_now())
    listener.process_message(msg())
    out = listener.process_message(msg(text=SELL.replace("Sl : 3925", "Sl : 3920")))
    assert out["status"] == "conflict"
    assert out["requires_review"] is True


# --------------------------------------------------------------------------
# edits
# --------------------------------------------------------------------------

def test_edit_gets_the_next_revision_and_needs_review(tmp_path):
    listener = make_listener(tmp_path, quote=quote_now())
    listener.process_message(msg())
    out = asyncio.run(listener.on_edit(SimpleNamespace(message=msg(
        text=SELL.replace("Tp : 3900", "Tp : 3895"), edited=NOW + timedelta(minutes=1)))))
    assert out["status"] == "manual_review"
    assert out["order_intent"] is None
    assert listener.ledger.revision_for("@source", 100) == 1


def test_a_second_edit_gets_revision_two(tmp_path):
    listener = make_listener(tmp_path, quote=quote_now())
    listener.process_message(msg())
    for i, tp in enumerate(["3895", "3890"], start=1):
        asyncio.run(listener.on_edit(SimpleNamespace(message=msg(
            text=SELL.replace("Tp : 3900", f"Tp : {tp}"),
            edited=NOW + timedelta(minutes=i)))))
    assert listener.ledger.revision_for("@source", 100) == 2


def test_edit_of_a_message_never_seen_is_revision_one(tmp_path):
    listener = make_listener(tmp_path, quote=quote_now())
    out = asyncio.run(listener.on_edit(SimpleNamespace(message=msg(mid=555))))
    assert out["status"] == "manual_review"
    assert listener.ledger.revision_for("@source", 555) == 1


# --------------------------------------------------------------------------
# reconnect / gap fill
# --------------------------------------------------------------------------

def _runtime(tmp_path, messages, *, state=None, client=None, **kwargs):
    listener = make_listener(tmp_path, quote=quote_now())
    if state is not None:
        listener.state = state
    client = client or FakeClient(messages)
    return Runtime(client=client, listener=listener, channel="@source",
                   heartbeat_path=tmp_path / "beat.json", log=lambda *_: None, **kwargs)


def test_gap_fill_processes_every_message_after_the_cursor(tmp_path):
    runtime = _runtime(tmp_path, [msg(1), msg(2), msg(3)])
    seen = asyncio.run(runtime.gap_fill())
    assert seen == [1, 2, 3]
    assert runtime.events == 3


def test_gap_fill_twice_stores_nothing_new(tmp_path):
    runtime = _runtime(tmp_path, [msg(1), msg(2)])
    asyncio.run(runtime.gap_fill())
    before = runtime.listener.ledger.counts()
    asyncio.run(runtime.gap_fill())
    assert runtime.listener.ledger.counts() == before


def test_gap_fill_starts_from_the_persisted_cursor(tmp_path):
    state = MirrorState(tmp_path / "state.json")
    state.advance(2)
    runtime = _runtime(tmp_path, [msg(1), msg(2), msg(3)], state=state)
    seen = asyncio.run(runtime.gap_fill())
    assert seen == [3]
    assert runtime.events == 1


def test_reconnect_fills_the_gap_it_could_have_missed(tmp_path):
    runtime = _runtime(tmp_path, [msg(1)])
    asyncio.run(runtime.connect())
    asyncio.run(runtime.gap_fill())
    runtime.client.messages.append(msg(2))       # arrives while disconnected
    asyncio.run(runtime.reconnect())
    assert runtime.reconnects == 1
    assert runtime.listener.ledger.counts() == {"paper_intent": 2}


# --------------------------------------------------------------------------
# state and heartbeat
# --------------------------------------------------------------------------

def test_state_survives_a_restart(tmp_path):
    path = tmp_path / "state.json"
    first = MirrorState(path)
    first.begin_run()
    first.advance(4321)
    second = MirrorState(path)
    assert second.last_message_id == 4321
    assert second.runs == 1


def test_a_corrupt_state_file_does_not_stop_the_listener(tmp_path):
    path = tmp_path / "state.json"
    path.write_text("{not json", encoding="utf-8")
    assert MirrorState(path).last_message_id == 0


def test_heartbeat_is_written(tmp_path):
    runtime = _runtime(tmp_path, [])
    runtime.heartbeat("listening")
    data = json.loads((tmp_path / "beat.json").read_text(encoding="utf-8"))
    assert data["phase"] == "listening"
    assert data["channel"] == "@source"


def test_heartbeat_is_throttled_while_listening(tmp_path):
    runtime = _runtime(tmp_path, [])
    runtime.heartbeat("listening")
    first = (tmp_path / "beat.json").read_text(encoding="utf-8")
    time.sleep(0.01)
    runtime.heartbeat("listening")
    assert (tmp_path / "beat.json").read_text(encoding="utf-8") == first


# --------------------------------------------------------------------------
# the guard against a second listener, and the absence of a write path
# --------------------------------------------------------------------------

def _load_script(name):
    spec = importlib.util.spec_from_file_location(
        name, ROOT / "scripts" / "telegram_paper_listener.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_soak_guard_detects_an_active_listener(tmp_path, monkeypatch):
    module = _load_script("listener_script_guard")
    beat = tmp_path / "soak.json"
    beat.write_text(json.dumps({"phase": "listening", "channel": "@x"}), encoding="utf-8")
    monkeypatch.setattr(module, "SOAK_HEARTBEAT", beat)
    assert module.soak_is_active()["phase"] == "listening"


def test_soak_guard_ignores_a_stale_heartbeat(tmp_path, monkeypatch):
    module = _load_script("listener_script_stale")
    beat = tmp_path / "soak.json"
    beat.write_text(json.dumps({"phase": "listening"}), encoding="utf-8")
    old = time.time() - 3600
    import os
    os.utime(beat, (old, old))
    monkeypatch.setattr(module, "SOAK_HEARTBEAT", beat)
    assert module.soak_is_active() is None


def test_listener_module_contains_no_broker_write_call():
    """A paper phase that can call create_order is not a paper phase."""
    source = (ROOT / "src" / "rade" / "observer" / "listener.py").read_text(encoding="utf-8")
    for forbidden in ("create_order", "close_position", "modify_position", "place_order"):
        assert forbidden not in source


# --------------------------------------------------------------------------
# the cursor must survive an ephemeral disk
# --------------------------------------------------------------------------

def test_cursor_is_restored_from_the_store_when_the_file_is_gone(tmp_path):
    """A hosted restart has no local state file; the database is the cursor."""
    runtime = _runtime(tmp_path, [msg(1), msg(2), msg(3), msg(4), msg(5)])
    asyncio.run(runtime.connect())
    asyncio.run(runtime.gap_fill())
    assert runtime.listener.state.last_message_id == 5

    # simulate a restart: same database, but the state file is gone (Render's
    # disk is ephemeral, so this is the normal case after a redeploy)
    restarted = _runtime(tmp_path, [msg(1), msg(2), msg(3), msg(4), msg(5)],
                         state=MirrorState(tmp_path / "gone.json"))
    assert restarted.listener.state.last_message_id == 0
    assert restarted.restore_cursor() == 5
    assert asyncio.run(restarted.gap_fill()) == []
    assert restarted.events == 0


def test_cursor_restore_does_not_move_backwards(tmp_path):
    state = MirrorState(tmp_path / "state.json")
    state.advance(9)
    runtime = _runtime(tmp_path, [msg(1)], state=state)
    runtime.listener.process_message(msg(1))
    assert runtime.restore_cursor() == 9


# --------------------------------------------------------------------------
# the first activation must not import the channel's history
# --------------------------------------------------------------------------

def test_a_fresh_install_starts_from_the_newest_message(tmp_path):
    """Old calls must not become today's paper intents."""
    runtime = _runtime(tmp_path, [msg(1), msg(2), msg(3)])
    asyncio.run(runtime.connect())
    floor = asyncio.run(runtime.establish_baseline())
    assert floor == 3
    runtime.listener.state.advance(floor)
    seen = asyncio.run(runtime.gap_fill())
    assert seen == []
    assert runtime.events == 0
    assert runtime.listener.ledger.counts() == {}


def test_only_messages_posted_after_the_baseline_are_imported(tmp_path):
    runtime = _runtime(tmp_path, [msg(1), msg(2), msg(3)])
    asyncio.run(runtime.connect())
    floor = asyncio.run(runtime.establish_baseline())
    runtime.listener.state.advance(floor)
    runtime.client.messages.append(msg(4))
    seen = asyncio.run(runtime.gap_fill())
    assert seen == [4]
    assert runtime.events == 1


def test_an_explicit_start_at_imports_from_that_message(tmp_path):
    runtime = _runtime(tmp_path, [msg(1), msg(2), msg(3)], start_at="2")
    asyncio.run(runtime.connect())
    floor = asyncio.run(runtime.establish_baseline())
    assert floor == 2
    runtime.listener.state.advance(floor)
    assert asyncio.run(runtime.gap_fill()) == [3]


def test_importing_the_history_needs_a_second_switch(tmp_path):
    runtime = _runtime(tmp_path, [msg(1)], start_at="history")
    asyncio.run(runtime.connect())
    with pytest.raises(RuntimeError):
        asyncio.run(runtime.establish_baseline())
    runtime.allow_history_import = True
    assert asyncio.run(runtime.establish_baseline()) == 0


def test_an_unusable_start_setting_is_refused(tmp_path):
    runtime = _runtime(tmp_path, [msg(1)], start_at="yesterday")
    asyncio.run(runtime.connect())
    with pytest.raises(RuntimeError):
        asyncio.run(runtime.establish_baseline())


def test_the_start_source_is_recorded_for_the_operator(tmp_path):
    runtime = _runtime(tmp_path, [msg(7)])
    asyncio.run(runtime.connect())
    asyncio.run(runtime.establish_baseline())
    assert runtime.listener.state.start_source == "latest:7"


def test_the_baseline_survives_a_wiped_disk(tmp_path):
    """The floor lives in the database, because the hosted disk is ephemeral."""
    runtime = _runtime(tmp_path, [msg(1), msg(2)])
    asyncio.run(runtime.connect())
    floor = asyncio.run(runtime.establish_baseline())
    runtime.save_cursor(floor)

    restarted = _runtime(tmp_path, [msg(1), msg(2)],
                         state=MirrorState(tmp_path / "gone.json"))
    assert restarted.listener.state.last_message_id == 0
    assert restarted.restore_cursor() == 2
    assert asyncio.run(restarted.gap_fill()) == []


def test_a_client_that_cannot_report_the_newest_message_refuses_to_start(tmp_path):
    runtime = _runtime(tmp_path, [msg(1)], client=NoLatestClient([msg(1)]))
    asyncio.run(runtime.connect())
    with pytest.raises(RuntimeError):
        asyncio.run(runtime.establish_baseline())
