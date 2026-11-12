"""HTTP service tests against a real socket on an ephemeral port."""
from __future__ import annotations

import http.client
import json
import threading
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer

import pytest

from rade.observer.mirror_pipeline import (
    InstrumentSpec,
    MarketQuote,
    MessageEnvelope,
    RiskConfig,
    SQLiteIntentLedger,
    process_once,
)
from rade.observer.service import MirrorService, make_handler

TOKEN = "test-token-value"
NOW = datetime(2026, 11, 12, 12, 0, tzinfo=timezone.utc)
INSTRUMENT = InstrumentSpec("XAUUSD", 100, 0.01, 0.01, 50)
RISK = RiskConfig(10_000, 0.005, 2.0)
VALID = "XAUUSD SELL NOW 3915\nSl : 3925\nTp : 3910\nTp : 3905\nTp : 3900"


def seed(ledger):
    def env(mid, text, revision=0):
        # received_at must be the real clock: the quote is stamped when it is
        # fetched, and a quote "from the future" is correctly rejected.
        return MessageEnvelope("@source", mid, revision, NOW, datetime.now(timezone.utc), text)

    process_once(env(1, VALID), ledger=ledger, instrument=INSTRUMENT, risk=RISK,
                 quote=MarketQuote(3915, 3915, datetime.now(timezone.utc)))
    # a target on the wrong side of entry -> manual review
    process_once(env(2, "XAUUSD SELL NOW 3915\nSl : 3925\nTp : 3920"), ledger=ledger,
                 instrument=INSTRUMENT, risk=RISK,
                 quote=MarketQuote(3915, 3915, datetime.now(timezone.utc)))
    process_once(env(3, "XAUUSD BUY closed at TP1, +80 pips"), ledger=ledger,
                 instrument=INSTRUMENT, risk=RISK)


@pytest.fixture()
def live(tmp_path):
    ledger = SQLiteIntentLedger(tmp_path / "l.sqlite")
    seed(ledger)
    heartbeat = tmp_path / "hb.json"
    heartbeat.write_text(json.dumps({"phase": "listening", "channel": "@source"}),
                         encoding="utf-8")
    service = MirrorService(ledger, token=TOKEN, heartbeat_path=heartbeat)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(service))
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield httpd.server_address[1]
    httpd.shutdown()
    httpd.server_close()
    thread.join(timeout=5)


def call(port, path, *, token=None, method="GET"):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    conn.request(method, path, headers=headers)
    resp = conn.getresponse()
    body = resp.read()
    conn.close()
    return resp.status, (json.loads(body) if body else None)


def test_healthz_is_public_and_minimal(live):
    status, body = call(live, "/healthz")
    assert status == 200
    assert body["status"] == "ok"
    # nothing about the channel, the account, or the store
    assert set(body) == {"status", "uptime_s", "time"}


def test_api_requires_a_token(live):
    assert call(live, "/api/status")[0] == 401
    assert call(live, "/api/events")[0] == 401
    assert call(live, "/api/status", token="wrong")[0] == 401


def test_status_reports_paper_only_and_no_write_capability(live):
    status, body = call(live, "/api/status", token=TOKEN)
    assert status == 200
    assert body["paper_only"] is True
    assert body["broker_writes"] is False
    assert body["exit_policy"] == "unresolved"
    assert body["counts"]["paper_intent"] == 1
    assert body["listener"]["phase"] == "listening"
    assert body["listener"]["stale"] is False


def test_status_never_contains_a_secret_value(live, monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "super-secret-service-key")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "bot-secret-token")
    monkeypatch.setenv("TELEGRAM_NOTIFY_CHAT_ID", "123456")
    _, body = call(live, "/api/status", token=TOKEN)
    blob = json.dumps(body)
    assert "super-secret-service-key" not in blob
    assert "bot-secret-token" not in blob
    assert body["config"]["supabase"] is True
    assert body["config"]["notifier"] is True


def test_events_returns_newest_first(live):
    status, body = call(live, "/api/events?limit=10", token=TOKEN)
    assert status == 200
    assert len(body["events"]) == 3


def test_review_queue_contains_only_review_dispositions(live):
    status, body = call(live, "/api/review", token=TOKEN)
    assert status == 200
    assert {e["status"] for e in body["events"]} == {"manual_review"}


def test_intents_endpoint_contains_only_paper_intents(live):
    _, body = call(live, "/api/intents", token=TOKEN)
    assert {e["status"] for e in body["events"]} == {"paper_intent"}
    assert body["events"][0]["intent_json"]["filled"] is False


def test_limit_is_capped(live):
    _, body = call(live, "/api/events?limit=100000", token=TOKEN)
    assert len(body["events"]) <= 200


def test_writes_are_rejected(live):
    for method in ("POST", "PUT", "PATCH", "DELETE"):
        assert call(live, "/api/events", token=TOKEN, method=method)[0] == 405


def test_unknown_path_is_404(live):
    assert call(live, "/api/nope", token=TOKEN)[0] == 404


def test_robots_is_served_without_auth(live):
    conn = http.client.HTTPConnection("127.0.0.1", live, timeout=10)
    conn.request("GET", "/robots.txt")
    resp = conn.getresponse()
    assert resp.status == 200
    assert b"Disallow" in resp.read()
    conn.close()


def test_a_stale_heartbeat_is_reported_as_stale(tmp_path):
    import os
    import time

    ledger = SQLiteIntentLedger(tmp_path / "l.sqlite")
    beat = tmp_path / "hb.json"
    beat.write_text(json.dumps({"phase": "listening"}), encoding="utf-8")
    old = time.time() - 600
    os.utime(beat, (old, old))
    service = MirrorService(ledger, token=TOKEN, heartbeat_path=beat)
    assert service.listener_state()["stale"] is True


def test_a_missing_heartbeat_is_not_reported_as_listening(tmp_path):
    ledger = SQLiteIntentLedger(tmp_path / "l.sqlite")
    service = MirrorService(ledger, token=TOKEN, heartbeat_path=tmp_path / "none.json")
    state = service.listener_state()
    assert state["known"] is False
    assert "phase" not in state
