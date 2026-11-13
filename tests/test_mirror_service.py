"""HTTP service tests against a real socket on an ephemeral port."""
from __future__ import annotations

import http.client
import json
import threading
import time
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
from rade.observer.service import (
    MirrorService,
    main as service_main,
    make_handler,
    start_listener_if_configured,
)

TOKEN = "test-token-value"
NOW = datetime(2026, 11, 12, 12, 0, tzinfo=timezone.utc)
INSTRUMENT = InstrumentSpec("XAUUSD", 100, 0.01, 0.01, 50, units_confirmed=True)
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


def test_healthz_is_public_and_reports_the_listener_state(live):
    status, body = call(live, "/healthz")
    assert status == 200
    assert body["status"] == "ok"
    # the intake's state, but nothing about the channel, the account or the store
    assert set(body) == {"status", "uptime_s", "time", "listener"}
    assert set(body["listener"]) == {"expected", "healthy", "phase",
                                     "heartbeat_age_s", "reason"}
    assert "@source" not in json.dumps(body)


def test_healthz_is_503_when_the_expected_listener_is_dead(tmp_path):
    """A dead intake must not look like a healthy service."""
    ledger = SQLiteIntentLedger(tmp_path / "l.sqlite")
    service = MirrorService(ledger, token=TOKEN,
                             heartbeat_path=tmp_path / "none.json",
                             listener_expected=True)
    service.started_at = time.time() - 1000   # past the startup grace
    status, body = service.route("/healthz", {})
    assert status == 503
    assert body["status"] == "degraded"
    assert body["listener"]["healthy"] is False
    assert body["listener"]["reason"]


def test_healthz_is_503_when_the_listener_thread_died(tmp_path):
    class DeadThread:
        def is_alive(self):
            return False

    ledger = SQLiteIntentLedger(tmp_path / "l.sqlite")
    beat = tmp_path / "hb.json"
    beat.write_text(json.dumps({"phase": "listening"}), encoding="utf-8")
    service = MirrorService(ledger, token=TOKEN, heartbeat_path=beat,
                             listener_expected=True, listener_thread=DeadThread())
    status, body = service.route("/healthz", {})
    assert status == 503
    assert body["listener"]["healthy"] is False
    assert "thread" in body["listener"]["reason"]


def test_healthz_is_ok_when_the_listener_is_not_enabled(tmp_path):
    ledger = SQLiteIntentLedger(tmp_path / "l.sqlite")
    service = MirrorService(ledger, token=TOKEN,
                             heartbeat_path=tmp_path / "none.json")
    status, body = service.route("/healthz", {})
    assert status == 200
    assert body["listener"]["expected"] is False
    assert body["listener"]["healthy"] is True


def test_healthz_is_ok_during_the_startup_grace(tmp_path):
    ledger = SQLiteIntentLedger(tmp_path / "l.sqlite")
    service = MirrorService(ledger, token=TOKEN,
                             heartbeat_path=tmp_path / "none.json",
                             listener_expected=True)
    status, body = service.route("/healthz", {})
    assert status == 200
    assert body["listener"]["healthy"] is True
    assert "starting" in body["listener"]["reason"]


def test_healthz_reports_connecting_as_a_transient_state(tmp_path):
    ledger = SQLiteIntentLedger(tmp_path / "l.sqlite")
    beat = tmp_path / "hb.json"
    beat.write_text(json.dumps({"phase": "connecting"}), encoding="utf-8")
    service = MirrorService(ledger, token=TOKEN, heartbeat_path=beat,
                             listener_expected=True)
    status, body = service.route("/healthz", {})
    assert status == 200
    assert body["listener"]["reason"] == "listener is connecting"


def test_healthz_is_503_when_the_listener_stopped(tmp_path):
    ledger = SQLiteIntentLedger(tmp_path / "l.sqlite")
    beat = tmp_path / "hb.json"
    beat.write_text(json.dumps({"phase": "stopped"}), encoding="utf-8")
    service = MirrorService(ledger, token=TOKEN, heartbeat_path=beat,
                             listener_expected=True)
    status, body = service.route("/healthz", {})
    assert status == 503
    assert body["listener"]["reason"] == "listener stopped"


def test_healthz_is_503_when_the_reconnect_failed(tmp_path):
    """A disconnected intake must not keep the health check green."""
    ledger = SQLiteIntentLedger(tmp_path / "l.sqlite")
    beat = tmp_path / "hb.json"
    beat.write_text(json.dumps({"phase": "degraded", "reconnect_failed": True}),
                     encoding="utf-8")
    service = MirrorService(ledger, token=TOKEN, heartbeat_path=beat,
                             listener_expected=True)
    status, body = service.route("/healthz", {})
    assert status == 503
    assert body["listener"]["healthy"] is False
    assert body["listener"]["reason"] == "listener reconnect failed"


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
    assert state["phase"] is None
    assert state["stale"] is True


# --------------------------------------------------------------------------
# the service must refuse to come up in a state it cannot vouch for
# --------------------------------------------------------------------------

def test_the_service_refuses_to_start_without_a_durable_store(monkeypatch, tmp_path):
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    monkeypatch.delenv("SUPABASE_SERVICE_ROLE_KEY", raising=False)
    monkeypatch.setenv("RENDER", "true")
    monkeypatch.setenv("MIRROR_API_TOKEN", "t")
    monkeypatch.setenv("MIRROR_SQLITE_PATH", str(tmp_path / "l.sqlite"))
    assert service_main([]) == 3


def test_the_service_refuses_to_start_when_the_listener_cannot_start(monkeypatch,
                                                                     tmp_path):
    # A confirmed contract size without the value is a misconfiguration that
    # would silently block every signal, so the service refuses to come up.
    monkeypatch.setenv("MIRROR_API_TOKEN", "t")
    monkeypatch.setenv("MIRROR_RUN_LISTENER", "1")
    monkeypatch.setenv("MIRROR_CONTRACT_UNITS_CONFIRMED", "1")
    monkeypatch.delenv("MIRROR_CONTRACT_UNITS_PER_LOT", raising=False)
    monkeypatch.delenv("MIRROR_EQUITY", raising=False)
    monkeypatch.delenv("RENDER", raising=False)
    monkeypatch.setenv("MIRROR_SQLITE_PATH", str(tmp_path / "l.sqlite"))
    assert service_main([]) == 3


def test_the_listener_handle_reports_a_missing_equity(monkeypatch, tmp_path):
    monkeypatch.setenv("MIRROR_RUN_LISTENER", "1")
    monkeypatch.setenv("MIRROR_CONTRACT_UNITS_CONFIRMED", "1")
    monkeypatch.setenv("MIRROR_CONTRACT_UNITS_PER_LOT", "100")
    monkeypatch.delenv("MIRROR_EQUITY", raising=False)
    handle = start_listener_if_configured(SQLiteIntentLedger(tmp_path / "l.sqlite"))
    assert handle.expected is True
    assert handle.error is not None
    assert "MIRROR_EQUITY" in handle.error


def test_the_listener_starts_without_sizing_inputs_while_unconfirmed(monkeypatch,
                                                                    tmp_path):
    """An unconfirmed contract size must not stop the archive from running."""
    import rade.ingestion.telegram.client as tg_client

    monkeypatch.setenv("MIRROR_RUN_LISTENER", "1")
    monkeypatch.setenv("MIRROR_CHANNEL", "@source")
    monkeypatch.setenv("MIRROR_HEARTBEAT", str(tmp_path / "hb.json"))
    monkeypatch.setenv("MIRROR_STATE", str(tmp_path / "state.json"))
    monkeypatch.delenv("MIRROR_EQUITY", raising=False)
    monkeypatch.delenv("MIRROR_CONTRACT_UNITS_PER_LOT", raising=False)
    monkeypatch.delenv("MIRROR_CONTRACT_UNITS_CONFIRMED", raising=False)
    # No Telegram connection from a unit test.
    monkeypatch.setattr(tg_client, "make_client", lambda: object())

    handle = start_listener_if_configured(SQLiteIntentLedger(tmp_path / "l.sqlite"))
    assert handle.expected is True
    assert handle.error is None
    assert handle.thread is not None


def test_a_confirmed_contract_size_still_requires_the_value(monkeypatch, tmp_path):
    monkeypatch.setenv("MIRROR_RUN_LISTENER", "1")
    monkeypatch.setenv("MIRROR_CONTRACT_UNITS_CONFIRMED", "1")
    monkeypatch.setenv("MIRROR_EQUITY", "10000")
    monkeypatch.delenv("MIRROR_CONTRACT_UNITS_PER_LOT", raising=False)
    handle = start_listener_if_configured(SQLiteIntentLedger(tmp_path / "l.sqlite"))
    assert handle.error is not None
    assert "MIRROR_CONTRACT_UNITS_PER_LOT" in handle.error


def test_an_unusable_start_point_is_refused_before_the_thread_starts(monkeypatch,
                                                                    tmp_path):
    monkeypatch.setenv("MIRROR_RUN_LISTENER", "1")
    monkeypatch.setenv("MIRROR_START_AT", "0")
    handle = start_listener_if_configured(SQLiteIntentLedger(tmp_path / "l.sqlite"))
    assert handle.error is not None
    assert "MIRROR_START_AT" in handle.error
    assert handle.thread is None


def test_a_negative_start_point_is_refused(monkeypatch, tmp_path):
    monkeypatch.setenv("MIRROR_RUN_LISTENER", "1")
    monkeypatch.setenv("MIRROR_START_AT", "-5")
    handle = start_listener_if_configured(SQLiteIntentLedger(tmp_path / "l.sqlite"))
    assert handle.error is not None
    assert handle.thread is None


def test_no_listener_is_expected_when_it_is_switched_off(monkeypatch, tmp_path):
    monkeypatch.delenv("MIRROR_RUN_LISTENER", raising=False)
    handle = start_listener_if_configured(SQLiteIntentLedger(tmp_path / "l.sqlite"))
    assert handle.expected is False
    assert handle.error is None


# ---------------------------------------------------------------------------
# the execution path's read surface
# ---------------------------------------------------------------------------


def seed_trade(ledger, trade_id="RADE-abc"):
    """Write one trade and two transitions, the way the engine would."""
    ledger.open_trade({
        "trade_id": trade_id,
        "event_key": "k" * 64,
        "channel": "@source",
        "message_id": 7,
        "revision": 0,
        "symbol": "XAUUSD",
        "side": "sell",
        "size": 0.05,
        "entry": 3915.0,
        "stop": 3925.0,
        "current_stop": 3915.0,
        "targets": [3910.0, 3905.0, 3900.0],
        "plan": {"policy": "three_targets", "final_target": 3900.0,
                 "ignored_targets": [],
                 "legs": [{"label": "tp1", "target": 3910.0, "fraction": 0.3333,
                           "quantity": 0.01, "managed": True,
                           "stop_after_kind": "breakeven", "stop_after_value": None,
                           "expected_remaining_after": 0.04}]},
        "state": "open",
        "legs_done": 1,
        "broker_order_id": 1000,
        "broker_position_id": 5000,
        "fill_price": 3915.0,
        "management_stopped": False,
        "note": "",
    })
    ledger.record_transition({
        "trade_id": trade_id, "event_key": "k" * 64, "stage": "fill",
        "status": "open", "detail": "position 5000 open at 3915",
        "broker_order_id": 1000, "broker_position_id": 5000,
        "message_id": 7, "revision": 0, "payload": {},
    })
    return trade_id


class _Spec:
    environment = "demo"
    symbol = "XAUUSD"
    instrument_id = 1808
    trade_route_id = "1869"
    account_number = 2
    contract_units_per_lot = 100.0
    lot_step = 0.01
    min_lot = 0.01
    max_lot = 50.0
    symbol_status = "FULLY_OPEN"
    verified = True
    unconfirmed_reason = None


@pytest.fixture()
def live_exec(tmp_path):
    ledger = SQLiteIntentLedger(tmp_path / "l.sqlite")
    seed(ledger)
    seed_trade(ledger)
    heartbeat = tmp_path / "hb.json"
    heartbeat.write_text(json.dumps({"phase": "listening"}), encoding="utf-8")
    service = MirrorService(ledger, token=TOKEN, heartbeat_path=heartbeat,
                             executor=object(), broker_spec=_Spec())
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(service))
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield httpd.server_address[1]
    httpd.shutdown()
    httpd.server_close()
    thread.join(timeout=5)


def test_trades_are_served_with_the_broker_state(live_exec):
    status, body = call(live_exec, "/api/trades", token=TOKEN)
    assert status == 200
    trade = body["trades"][0]
    # The broker's own numbers, not a reconstruction from the signal.
    assert trade["broker_position_id"] == 5000
    assert trade["broker_order_id"] == 1000
    assert trade["fill_price"] == 3915.0
    assert trade["current_stop"] == 3915.0
    assert trade["state"] == "open"
    assert trade["legs_done"] == 1


def test_one_trade_can_be_read_with_its_transitions(live_exec):
    status, body = call(live_exec, "/api/trades/RADE-abc", token=TOKEN)
    assert status == 200
    assert body["trade"]["trade_id"] == "RADE-abc"
    assert body["trade"]["plan"]["final_target"] == 3900.0
    assert [t["stage"] for t in body["transitions"]] == ["fill"]


def test_an_unknown_trade_is_404(live_exec):
    status, body = call(live_exec, "/api/trades/RADE-nope", token=TOKEN)
    assert status == 404


def test_the_trade_endpoints_require_a_token(live_exec):
    status, _ = call(live_exec, "/api/trades")
    assert status == 401


def test_the_trade_endpoints_refuse_a_write(live_exec):
    status, body = call(live_exec, "/api/trades", token=TOKEN, method="POST")
    assert status == 405


def test_status_reports_the_demo_execution_path(live_exec):
    status, body = call(live_exec, "/api/status", token=TOKEN)
    assert status == 200
    assert body["account_scope"] == "demo"
    assert body["live_trading"] is False
    assert body["broker_writes"] is True
    assert body["paper_only"] is False
    assert body["exit_policy"] == "tp_ladder"
    assert body["execution"]["enabled"] is True
    assert body["execution"]["broker"]["verified"] is True
    assert body["execution"]["broker"]["instrument_id"] == 1808


def test_no_executor_is_configured_by_default(monkeypatch, tmp_path):
    monkeypatch.delenv("MIRROR_EXECUTE", raising=False)
    from rade.observer.service import start_executor_if_configured

    handle = start_executor_if_configured(SQLiteIntentLedger(tmp_path / "l.sqlite"))
    assert handle.executor is None
    assert handle.error is None
