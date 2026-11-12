"""Tests for the storage adapters and the notification adapter.

The Supabase tests use a fake PostgREST that implements the semantics the
adapter depends on (``on_conflict`` + ``resolution=ignore-duplicates`` returning
an empty representation when the row already exists). They test the adapter's
protocol usage, not Supabase itself.
"""
from __future__ import annotations

import pytest

from rade.observer.mirror_pipeline import (
    CLAIM_CONFLICT,
    CLAIM_DUPLICATE,
    CLAIM_INSERTED,
    InstrumentSpec,
    MarketQuote,
    MessageEnvelope,
    RiskConfig,
    SQLiteIntentLedger,
    evaluate_message,
)
from rade.observer.notify import (
    BotFatherNotifier,
    RecordingNotifier,
    build_notification,
    notify_result,
)
from rade.observer.store import RequestsTransport, StorageError, SupabaseLedger, make_ledger
from datetime import datetime, timezone

NOW = datetime(2026, 11, 12, 12, 0, tzinfo=timezone.utc)
INSTRUMENT = InstrumentSpec("XAUUSD", 100, 0.01, 0.01, 50)
RISK = RiskConfig(10_000, 0.005, 2.0)
VALID = "XAUUSD SELL NOW 3915\nSl : 3925\nTp : 3910\nTp : 3905\nTp : 3900"


def envelope(text=VALID, *, mid=100, revision=0):
    return MessageEnvelope("@source", mid, revision, NOW, NOW, text)


def accepted(text=VALID, *, mid=100, revision=0):
    return evaluate_message(
        envelope(text, mid=mid, revision=revision),
        instrument=INSTRUMENT, risk=RISK, quote=MarketQuote(3915, 3915, NOW),
    )


class FakePostgrest:
    """Enough PostgREST to exercise the adapter's decisions."""

    def __init__(self):
        self.events: dict[str, dict] = {}
        self.notifications: dict[tuple[str, str], dict] = {}
        self.calls: list[tuple[str, str, dict]] = []

    def request(self, method, path, *, params=None, json_body=None, prefer=None):
        params = dict(params or {})
        self.calls.append((method, path, params))
        if path == "mirror_events" and method == "POST":
            key = json_body["event_key"]
            if key in self.events:
                return 201, []
            self.events[key] = dict(json_body)
            return 201, [{"event_key": key}]

        if path == "mirror_events" and method == "GET":
            if "event_key" in params:
                key = params["event_key"].split("eq.", 1)[1]
                row = self.events.get(key)
                return 200, [{"text_sha256": row["text_sha256"]}] if row else []
            if params.get("select") == "revision":
                channel = params["channel"].split("eq.", 1)[1]
                mid = int(params["message_id"].split("eq.", 1)[1])
                revs = [r["revision"] for r in self.events.values()
                        if r["channel"] == channel and r["message_id"] == mid]
                return 200, [{"revision": max(revs)}] if revs else []
            if params.get("select") == "status":
                return 200, [{"status": r["status"]} for r in self.events.values()]
            rows = sorted(self.events.values(), key=lambda r: r["received_at"], reverse=True)
            if "status" in params:
                allowed = params["status"].replace("in.(", "").rstrip(")").split(",")
                rows = [r for r in rows if r["status"] in allowed]
            return 200, rows[: int(params.get("limit", 50))]

        if path == "notification_log" and method == "POST":
            slot = (json_body["event_key"], json_body["kind"])
            if slot in self.notifications:
                return 201, []
            self.notifications[slot] = dict(json_body)
            return 201, [{"id": len(self.notifications)}]

        raise AssertionError(f"unexpected call {method} {path} {params}")


# --------------------------------------------------------------------------
# Supabase ledger
# --------------------------------------------------------------------------

def test_supabase_claim_reports_insert_then_duplicate():
    fake = FakePostgrest()
    ledger = SupabaseLedger(fake)
    result = accepted()
    assert ledger.claim(envelope(), result) == CLAIM_INSERTED
    assert ledger.claim(envelope(), result) == CLAIM_DUPLICATE
    assert len(fake.events) == 1


def test_supabase_claim_detects_a_changed_body_as_conflict():
    fake = FakePostgrest()
    ledger = SupabaseLedger(fake)
    ledger.claim(envelope(), accepted())
    tampered = accepted(VALID.replace("Sl : 3925", "Sl : 3920"))
    assert ledger.claim(envelope(VALID.replace("Sl : 3925", "Sl : 3920")), tampered) == CLAIM_CONFLICT
    # the stored body is untouched
    assert "3925" in next(iter(fake.events.values()))["raw_text"]


def test_supabase_revision_and_counts():
    fake = FakePostgrest()
    ledger = SupabaseLedger(fake)
    assert ledger.revision_for("@source", 100) == 0
    ledger.claim(envelope(), accepted())
    ledger.claim(envelope(revision=1), accepted(revision=1))
    assert ledger.revision_for("@source", 100) == 1
    # revision 1 is an edit, so it is stored for review rather than sized
    assert ledger.counts() == {"paper_intent": 1, "manual_review": 1}


def test_supabase_recent_filters_by_status():
    fake = FakePostgrest()
    ledger = SupabaseLedger(fake)
    ledger.claim(envelope(), accepted())
    ledger.claim(envelope("hello", mid=101), accepted("hello", mid=101))
    review = ledger.recent(statuses=["manual_review"])
    assert review == []
    everything = ledger.recent()
    assert len(everything) == 2


def test_notification_slot_is_unique_in_the_store():
    fake = FakePostgrest()
    ledger = SupabaseLedger(fake)
    ledger.claim(envelope(), accepted())
    key = accepted().event_key
    assert ledger.record_notification(key, "accepted", True) is True
    assert ledger.record_notification(key, "accepted", True) is False


def test_make_ledger_falls_back_to_sqlite(monkeypatch, tmp_path):
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    monkeypatch.delenv("SUPABASE_SERVICE_ROLE_KEY", raising=False)
    monkeypatch.setenv("MIRROR_SQLITE_PATH", str(tmp_path / "l.sqlite"))
    assert isinstance(make_ledger(), SQLiteIntentLedger)


def test_make_ledger_prefers_supabase_when_configured(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "service-role-placeholder")
    assert isinstance(make_ledger(), SupabaseLedger)


# --------------------------------------------------------------------------
# secrets
# --------------------------------------------------------------------------

def test_transport_repr_does_not_contain_the_key():
    transport = RequestsTransport("https://example.supabase.co", "super-secret-key")
    assert "super-secret-key" not in repr(transport)


def test_transport_requires_credentials():
    with pytest.raises(StorageError):
        RequestsTransport("", "key")
    with pytest.raises(StorageError):
        RequestsTransport("https://example.supabase.co", "")


def test_transport_error_does_not_echo_the_key(monkeypatch):
    transport = RequestsTransport("https://example.supabase.co", "super-secret-key")

    class Boom:
        status_code = 401
        text = "unauthorized"
        content = b"x"

    monkeypatch.setattr(transport._session, "request", lambda *a, **k: Boom())
    with pytest.raises(StorageError) as excinfo:
        transport.request("GET", "mirror_events")
    assert "super-secret-key" not in str(excinfo.value)


# --------------------------------------------------------------------------
# notifications
# --------------------------------------------------------------------------

def test_accepted_signal_produces_a_notification():
    text = build_notification(accepted())
    assert text is not None
    assert "PAPER INTENT" in text
    assert "no broker order was sent" in text


def test_ignored_and_duplicate_are_silent():
    ignored = evaluate_message(envelope("hello"), instrument=INSTRUMENT, risk=RISK)
    assert build_notification(ignored) is None


def test_blocked_signal_notifies_as_blocked():
    blocked = evaluate_message(envelope(), instrument=INSTRUMENT, risk=RISK)
    text = build_notification(blocked)
    assert text is not None and "BLOCKED" in text


def test_notify_result_sends_once_per_event():
    notifier = RecordingNotifier()
    result = accepted()
    first = notify_result(result, notifier)
    assert first["notified"] is True
    assert len(notifier.sent) == 1


def test_notify_result_uses_the_ledger_slot(tmp_path):
    from rade.observer.mirror_pipeline import process_once

    ledger = SQLiteIntentLedger(tmp_path / "l.sqlite")
    result = process_once(envelope(), ledger=ledger, instrument=INSTRUMENT,
                          risk=RISK, quote=MarketQuote(3915, 3915, NOW))
    notifier = RecordingNotifier()
    assert notify_result(result, notifier, ledger=ledger)["notified"] is True
    again = notify_result(result, notifier, ledger=ledger)
    assert again["notified"] is False
    assert again["reason"] == "already notified"
    assert len(notifier.sent) == 1


def test_botfather_notifier_scrubs_the_token_from_errors():
    def boom(*a, **k):
        raise RuntimeError("POST https://api.telegram.org/botSECRET-TOKEN/sendMessage failed")

    notifier = BotFatherNotifier(token="SECRET-TOKEN", chat_id="123", post=boom)
    ok, detail = notifier.send("hello")
    assert ok is False
    assert "SECRET-TOKEN" not in detail
    assert "***" in detail


def test_botfather_notifier_reports_missing_configuration():
    notifier = BotFatherNotifier(token="", chat_id="")
    ok, detail = notifier.send("hello")
    assert ok is False
    assert "not configured" in detail


def test_botfather_notifier_returns_sent_on_200():
    class Ok:
        status_code = 200
        text = ""

    notifier = BotFatherNotifier(token="T", chat_id="1", post=lambda *a, **k: Ok())
    assert notifier.send("hello") == (True, "sent")


def test_notifier_repr_hides_the_token():
    notifier = BotFatherNotifier(token="SECRET-TOKEN", chat_id="1")
    assert "SECRET-TOKEN" not in repr(notifier)


def test_sqlite_last_message_id(tmp_path):
    ledger = SQLiteIntentLedger(tmp_path / "l.sqlite")
    assert ledger.last_message_id() == 0
    from rade.observer.mirror_pipeline import process_once
    process_once(envelope(mid=4321), ledger=ledger, instrument=INSTRUMENT, risk=RISK,
                 quote=MarketQuote(3915, 3915, datetime.now(timezone.utc)))
    assert ledger.last_message_id() == 4321


def test_supabase_last_message_id():
    fake = FakePostgrest()
    ledger = SupabaseLedger(fake)
    assert ledger.last_message_id() == 0
    from rade.observer.mirror_pipeline import process_once
    process_once(envelope(mid=4321), ledger=ledger, instrument=INSTRUMENT, risk=RISK,
                 quote=MarketQuote(3915, 3915, datetime.now(timezone.utc)))
    assert ledger.last_message_id() == 4321
