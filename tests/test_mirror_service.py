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
    _fixed_lots,
    _max_trade_risk_usd,
    main as service_main,
    make_handler,
    send_startup_report,
    start_listener_if_configured,
)

TOKEN = "test-token-value"
NOW = datetime(2026, 11, 12, 12, 0, tzinfo=timezone.utc)
INSTRUMENT = InstrumentSpec("XAUUSD", 100, 0.01, 0.01, 50, units_confirmed=True)
RISK = RiskConfig(10_000, 0.005, 2.0)
VALID = "XAUUSD SELL NOW 3915\nSl : 3925\nTp : 3910\nTp : 3905\nTp : 3900"


def seed(ledger):
    def env(mid, text, revision=0):
        # Both stamps must be the real clock: the quote is stamped when it is
        # fetched (a quote "from the future" is correctly rejected), and a
        # signal older than the age limit is correctly refused as stale.
        now = datetime.now(timezone.utc)
        return MessageEnvelope("@source", mid, revision, now, now, text)

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


def call(port, path, *, token=None, method="GET", body=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    payload = None
    if body is not None:
        payload = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    conn.request(method, path, body=payload, headers=headers)
    resp = conn.getresponse()
    raw = resp.read()
    conn.close()
    return resp.status, (json.loads(raw) if raw else None)


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


# ---------------------------------------------------------------------------
# the pause switch
# ---------------------------------------------------------------------------


class _PauseableExecutor:
    """Just enough executor for the pause routes: the switch is the ledger row."""

    def __init__(self, ledger):
        self.ledger = ledger

    def pause(self, *, reason, changed_by):
        return self.ledger.set_paused(paused=True, reason=reason,
                                      changed_by=changed_by)

    def resume(self, *, reason, changed_by):
        return self.ledger.set_paused(paused=False, reason=reason,
                                      changed_by=changed_by)

    def pause_state(self):
        control = self.ledger.control()
        if control is None:
            return {"paused": False, "reason": "the pause switch has never been set",
                    "changed_at": None, "changed_by": ""}
        return control

    def guard_state(self):
        from rade.execution.guards import RiskLimits
        return {"limits": RiskLimits().describe(), "last_verdict": None}


@pytest.fixture()
def live_pause(tmp_path):
    ledger = SQLiteIntentLedger(tmp_path / "p.sqlite")
    seed(ledger)
    heartbeat = tmp_path / "hb.json"
    heartbeat.write_text(json.dumps({"phase": "listening"}), encoding="utf-8")
    service = MirrorService(ledger, token=TOKEN, heartbeat_path=heartbeat,
                            executor=_PauseableExecutor(ledger), broker_spec=_Spec())
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(service))
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield httpd.server_address[1]
    httpd.shutdown()
    httpd.server_close()
    thread.join(timeout=5)


def test_pausing_requires_a_reason(live_pause):
    status, body = call(live_pause, "/api/pause", token=TOKEN, method="POST",
                        body={"by": "operator"})
    assert status == 400
    assert "reason is required" in body["error"]


def test_pause_and_resume_are_recorded_and_reported(live_pause):
    status, body = call(live_pause, "/api/pause", token=TOKEN, method="POST",
                        body={"reason": "checking the account", "by": "operator"})
    assert status == 200
    assert body["paused"]["paused"] is True
    assert body["paused"]["changed_by"] == "operator"

    _, status_body = call(live_pause, "/api/status", token=TOKEN)
    assert status_body["execution"]["paused"]["paused"] is True
    assert status_body["execution"]["paused"]["reason"] == "checking the account"

    status, body = call(live_pause, "/api/resume", token=TOKEN, method="POST",
                        body={"reason": "done", "by": "operator"})
    assert status == 200
    assert body["paused"]["paused"] is False


def test_the_pause_routes_need_the_token(live_pause):
    status, _ = call(live_pause, "/api/pause", method="POST",
                     body={"reason": "no token"})
    assert status == 401


def test_a_malformed_body_is_a_400_not_a_crash(live_pause):
    conn = http.client.HTTPConnection("127.0.0.1", live_pause, timeout=10)
    conn.request("POST", "/api/pause",
                 body=b"not json",
                 headers={"Authorization": f"Bearer {TOKEN}",
                          "Content-Type": "application/json"})
    resp = conn.getresponse()
    payload = json.loads(resp.read())
    conn.close()
    assert resp.status == 400
    assert "JSON object" in payload["error"]


def test_other_writes_are_still_rejected(live_pause):
    for path in ("/api/events", "/api/trades", "/api/status"):
        status, _ = call(live_pause, path, token=TOKEN, method="POST", body={})
        assert status == 405, path


def test_pausing_without_an_executor_is_a_conflict(monkeypatch, tmp_path):
    ledger = SQLiteIntentLedger(tmp_path / "n.sqlite")
    seed(ledger)
    heartbeat = tmp_path / "hb.json"
    heartbeat.write_text(json.dumps({"phase": "listening"}), encoding="utf-8")
    service = MirrorService(ledger, token=TOKEN, heartbeat_path=heartbeat)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(service))
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        status, body = call(httpd.server_address[1], "/api/pause", token=TOKEN,
                            method="POST", body={"reason": "why not"})
        assert status == 409
        assert "nothing to pause" in body["error"]
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


# --------------------------------------------------------------------------
# the delivery interval must fit inside the signal-age limit
# --------------------------------------------------------------------------
#
# The age guard refuses a late signal, which is correct and also means that a
# delivery path slower than the limit copies nothing at all. Live push has not
# been observed to arrive in this deployment, so the poll interval is the number
# that decides whether a signal can be copied.


def test_the_poll_interval_is_reported_and_fits_inside_the_age_limit(tmp_path, monkeypatch):
    monkeypatch.setenv("MIRROR_MAX_SIGNAL_AGE_S", "120")
    monkeypatch.setenv("MIRROR_CATCHUP_EVERY_S", "15")
    monkeypatch.setenv("MIRROR_RECONNECT_S", "900")
    service = MirrorService(SQLiteIntentLedger(tmp_path / "l.sqlite"), token=TOKEN,
                            heartbeat_path=tmp_path / "hb.json")

    intake = service.intake_limits()
    assert intake["catchup_every_seconds"] == 15
    assert intake["delivery_interval_seconds"] == 15
    assert intake["delivery_within_age_limit"] is True
    # The reconnect is still longer than the limit; that is what the poll is for,
    # and the status says so rather than hiding it.
    assert intake["catchup_can_exceed_age_limit"] is True


def test_a_delivery_interval_longer_than_the_limit_is_reported_as_such(tmp_path, monkeypatch):
    monkeypatch.setenv("MIRROR_MAX_SIGNAL_AGE_S", "10")
    monkeypatch.setenv("MIRROR_CATCHUP_EVERY_S", "30")
    monkeypatch.setenv("MIRROR_RECONNECT_S", "60")
    service = MirrorService(SQLiteIntentLedger(tmp_path / "l.sqlite"), token=TOKEN,
                            heartbeat_path=tmp_path / "hb.json")

    intake = service.intake_limits()
    assert intake["delivery_interval_seconds"] == 30
    assert intake["delivery_within_age_limit"] is False


def test_a_disabled_poll_falls_back_to_the_reconnect_interval(tmp_path, monkeypatch):
    monkeypatch.setenv("MIRROR_MAX_SIGNAL_AGE_S", "120")
    monkeypatch.setenv("MIRROR_CATCHUP_EVERY_S", "0")
    monkeypatch.setenv("MIRROR_RECONNECT_S", "900")
    service = MirrorService(SQLiteIntentLedger(tmp_path / "l.sqlite"), token=TOKEN,
                            heartbeat_path=tmp_path / "hb.json")

    intake = service.intake_limits()
    assert intake["catchup_every_seconds"] == 0
    assert intake["delivery_interval_seconds"] == 900
    assert intake["delivery_within_age_limit"] is False


# ---------------------------------------------------------------------------
# the sizing rule, as reported
# ---------------------------------------------------------------------------


def test_the_sizing_rule_defaults_to_risk_fraction_when_no_lot_is_set(tmp_path, monkeypatch):
    monkeypatch.delenv("MIRROR_FIXED_LOTS", raising=False)
    monkeypatch.delenv("MIRROR_MAX_TRADE_RISK_USD", raising=False)
    service = MirrorService(SQLiteIntentLedger(tmp_path / "l.sqlite"), token=TOKEN,
                            heartbeat_path=tmp_path / "hb.json")

    intake = service.intake_limits()
    assert intake["sizing_mode"] == "risk_fraction"
    assert intake["fixed_lot_ready"] is True


def test_a_fixed_lot_without_a_ceiling_is_reported_as_not_ready(tmp_path, monkeypatch):
    """A rule that can accept no signal must not read as a rule that passes."""
    monkeypatch.setenv("MIRROR_FIXED_LOTS", "0.03")
    monkeypatch.delenv("MIRROR_MAX_TRADE_RISK_USD", raising=False)
    service = MirrorService(SQLiteIntentLedger(tmp_path / "l.sqlite"), token=TOKEN,
                            heartbeat_path=tmp_path / "hb.json")

    intake = service.intake_limits()
    assert intake["sizing_mode"] == "fixed_lots"
    assert intake["fixed_lot_ready"] is False
    assert "ceiling" in intake["fixed_lot_problem"]


def test_a_complete_sizing_rule_is_reported_as_ready(tmp_path, monkeypatch):
    monkeypatch.setenv("MIRROR_FIXED_LOTS", "0.03")
    monkeypatch.setenv("MIRROR_MAX_TRADE_RISK_USD", "25")
    service = MirrorService(SQLiteIntentLedger(tmp_path / "l.sqlite"), token=TOKEN,
                            heartbeat_path=tmp_path / "hb.json")

    intake = service.intake_limits()
    assert intake["fixed_lot_ready"] is True
    assert intake["sizing"]["fixed_lots"] == 0.03
    assert intake["sizing"]["max_trade_risk_usd"] == 25.0


def test_a_nonsense_fixed_lot_stops_the_service_rather_than_being_ignored(monkeypatch):
    monkeypatch.setenv("MIRROR_FIXED_LOTS", "three")
    with pytest.raises(SystemExit, match="not a number"):
        _fixed_lots()


def test_a_negative_fixed_lot_stops_the_service(monkeypatch):
    monkeypatch.setenv("MIRROR_FIXED_LOTS", "-0.03")
    with pytest.raises(SystemExit, match="must be a finite positive"):
        _fixed_lots()


def test_an_infinite_fixed_lot_stops_the_service(monkeypatch):
    """``inf > 0`` is true, so positivity alone let infinity through."""
    monkeypatch.setenv("MIRROR_FIXED_LOTS", "inf")
    with pytest.raises(SystemExit, match="must be a finite positive"):
        _fixed_lots()


def test_a_nan_ceiling_stops_the_service(monkeypatch):
    monkeypatch.setenv("MIRROR_MAX_TRADE_RISK_USD", "nan")
    with pytest.raises(SystemExit, match="must be a finite positive"):
        _max_trade_risk_usd()


def test_an_infinite_ceiling_stops_the_service(monkeypatch):
    monkeypatch.setenv("MIRROR_MAX_TRADE_RISK_USD", "inf")
    with pytest.raises(SystemExit, match="must be a finite positive"):
        _max_trade_risk_usd()


# ---------------------------------------------------------------------------
# the executor refuses to start without a size rule
# ---------------------------------------------------------------------------


class _VerifiedGateway:
    """Enough of the gateway for the startup checks to reach the size rule."""

    def __init__(self):
        from rade.execution.broker import BrokerSpec
        self._spec = BrokerSpec(
            environment="demo", base_url="https://demo.tradelocker.com",
            account_id=1, account_number=1, symbol="XAUUSD", instrument_id=1808,
            trade_route_id="1869", contract_units_per_lot=100.0, lot_step=0.01,
            min_lot=0.01, max_lot=50.0, tick_size=0.01,
            symbol_status="FULLY_OPEN", confirmed_contract_units=100.0,
            verified=True,
        )

    def spec(self):
        return self._spec

    # The startup reconciliation and the guard preflight read these. An empty
    # account with a flat equity is enough: the test is about the size rule,
    # not about what the engine does with a position.
    def positions(self):
        return []

    def equity(self):
        return 10_000.0


def _start_with(monkeypatch, tmp_path, **env):
    monkeypatch.setenv("MIRROR_EXECUTE", "1")
    for key in ("MIRROR_FIXED_LOTS", "MIRROR_MAX_TRADE_RISK_USD"):
        monkeypatch.delenv(key, raising=False)
    # The destination pin is set by default so the tests that are about the
    # size rule can pass; the pin has its own test below.
    monkeypatch.setenv("MIRROR_DESTINATION_SERVER", "BLBRY")
    monkeypatch.setenv("MIRROR_DESTINATION_ACCOUNT_ID", "2346225")
    for key, value in env.items():
        monkeypatch.setenv(key, value)

    import rade.execution.gateway as gateway_module
    monkeypatch.setattr(gateway_module, "tradelocker_gateway_from_env",
                        lambda **kwargs: _VerifiedGateway())

    from rade.observer.service import start_executor_if_configured
    return start_executor_if_configured(SQLiteIntentLedger(tmp_path / "l.sqlite"))


def test_the_executor_does_not_start_without_a_fixed_lot(monkeypatch, tmp_path):
    """Unset used to mean "size from equity", which is the opposite of the rule."""
    handle = _start_with(monkeypatch, tmp_path,
                         MIRROR_MAX_TRADE_RISK_USD="50")

    assert handle.executor is None
    assert "MIRROR_FIXED_LOTS is not set" in handle.error
    assert "risk-fraction" in handle.error


def test_the_executor_does_not_start_without_a_ceiling(monkeypatch, tmp_path):
    """A fixed lot with no ceiling cannot accept any signal; say so at boot."""
    handle = _start_with(monkeypatch, tmp_path, MIRROR_FIXED_LOTS="0.03")

    assert handle.executor is None
    assert "MIRROR_MAX_TRADE_RISK_USD is not set" in handle.error


def test_the_executor_does_not_start_without_a_pinned_destination(monkeypatch, tmp_path):
    """An unset pin is fail-open: the copier would run against whatever
    account the credentials resolve to, which is not a risk a configuration
    should take by omission."""
    for key in ("MIRROR_DESTINATION_SERVER", "MIRROR_DESTINATION_ACCOUNT_ID"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("MIRROR_EXECUTE", "1")
    monkeypatch.setenv("MIRROR_FIXED_LOTS", "0.03")
    monkeypatch.setenv("MIRROR_MAX_TRADE_RISK_USD", "25")

    import rade.execution.gateway as gateway_module
    monkeypatch.setattr(gateway_module, "tradelocker_gateway_from_env",
                        lambda **kwargs: _VerifiedGateway())

    from rade.observer.service import start_executor_if_configured
    handle = start_executor_if_configured(SQLiteIntentLedger(tmp_path / "l.sqlite"))

    assert handle.executor is None
    assert "destination is not pinned" in handle.error


def test_the_executor_starts_when_both_numbers_are_configured(monkeypatch, tmp_path):
    """The happy path, so the two refusals above mean something."""
    handle = _start_with(monkeypatch, tmp_path, MIRROR_FIXED_LOTS="0.03",
                         MIRROR_MAX_TRADE_RISK_USD="50")
    try:
        assert handle.error is None, handle.error
        assert handle.executor is not None
        assert handle.executor.fixed_lots == pytest.approx(0.03)
        assert handle.executor.max_trade_risk_usd == pytest.approx(50.0)
    finally:
        if handle.stop_event is not None:
            handle.stop_event.set()
        if handle.thread is not None:
            handle.thread.join(timeout=5)


def test_execution_mode_reports_a_missing_lot_as_blocking(tmp_path, monkeypatch):
    """The status must not describe a mode the executor refuses to run in."""
    monkeypatch.setenv("MIRROR_EXECUTE", "1")
    monkeypatch.delenv("MIRROR_FIXED_LOTS", raising=False)
    monkeypatch.setenv("MIRROR_MAX_TRADE_RISK_USD", "50")
    service = MirrorService(SQLiteIntentLedger(tmp_path / "l.sqlite"), token=TOKEN,
                            heartbeat_path=tmp_path / "hb.json")

    intake = service.intake_limits()
    assert intake["execution_sizing_ready"] is False
    assert "MIRROR_FIXED_LOTS is not set" in intake["execution_sizing_problem"]
    assert intake["sizing"]["ready"] is False
    assert intake["sizing"]["blocks_execution"] is True


def test_paper_mode_is_not_reported_as_blocked_by_a_missing_lot(tmp_path, monkeypatch):
    """The paper mirror may size from equity; only execution may not."""
    monkeypatch.delenv("MIRROR_EXECUTE", raising=False)
    monkeypatch.delenv("MIRROR_FIXED_LOTS", raising=False)
    service = MirrorService(SQLiteIntentLedger(tmp_path / "l.sqlite"), token=TOKEN,
                            heartbeat_path=tmp_path / "hb.json")

    intake = service.intake_limits()
    assert intake["sizing"]["ready"] is True
    assert intake["sizing"].get("blocks_execution") is None
    assert intake["execution_sizing_ready"] is False


def test_execution_mode_with_a_complete_rule_is_ready(tmp_path, monkeypatch):
    monkeypatch.setenv("MIRROR_EXECUTE", "1")
    monkeypatch.setenv("MIRROR_FIXED_LOTS", "0.03")
    monkeypatch.setenv("MIRROR_MAX_TRADE_RISK_USD", "50")
    service = MirrorService(SQLiteIntentLedger(tmp_path / "l.sqlite"), token=TOKEN,
                            heartbeat_path=tmp_path / "hb.json")

    intake = service.intake_limits()
    assert intake["execution_sizing_ready"] is True
    assert intake["execution_sizing_problem"] == ""
    assert intake["sizing"]["ready"] is True


def test_the_startup_report_says_what_is_switched_off(tmp_path, monkeypatch):
    """A silent boot and a broken boot look the same; the report must name
    every guard and say DISABLED when one is off."""
    monkeypatch.setenv("MIRROR_EXECUTE", "0")
    monkeypatch.setenv("MIRROR_CHANNEL", "@XAUUSD_FOREX4")
    monkeypatch.setenv("MIRROR_FIXED_LOTS", "0.03")
    monkeypatch.setenv("MIRROR_MAX_TRADE_RISK_USD", "25")
    monkeypatch.setenv("MIRROR_MAX_SPREAD", "1.0")
    monkeypatch.setenv("MIRROR_MAX_SLIPPAGE", "2.0")
    monkeypatch.setenv("MIRROR_MAX_DRAWDOWN_FRACTION", "0.0335")
    monkeypatch.setenv("MIRROR_MAX_OPEN_POSITIONS", "1")
    monkeypatch.setenv("MIRROR_DAILY_LOSS_FRACTION", "0")  # switched off
    heartbeat = tmp_path / "hb.json"
    heartbeat.write_text(json.dumps({"phase": "listening"}), encoding="utf-8")
    service = MirrorService(SQLiteIntentLedger(tmp_path / "l.sqlite"), token=TOKEN,
                            heartbeat_path=heartbeat)

    sent: list[str] = []

    class Recorder:
        def send(self, text: str):
            sent.append(text)
            return True, "ok"

    logs: list[str] = []
    send_startup_report(service, Recorder(), logs.append)

    assert len(sent) == 1
    report = sent[0]
    assert "RADE BOT - STARTUP" in report
    assert "mode: PAPER (no orders)" in report
    assert "@XAUUSD_FOREX4" in report
    assert "daily loss DISABLED" in report
    assert "max spread 1.0" in report
    assert "max slippage 2.0" in report
    assert "max drawdown 0.0335" in report
    assert any("startup report sent: True" in line for line in logs)


def test_the_startup_report_names_the_executing_mode(tmp_path, monkeypatch):
    """The single most important line: is this process allowed to spend money."""
    monkeypatch.setenv("MIRROR_EXECUTE", "1")
    service = MirrorService(SQLiteIntentLedger(tmp_path / "l.sqlite"), token=TOKEN,
                            heartbeat_path=tmp_path / "hb.json",
                            executor=object())
    sent: list[str] = []

    class Recorder:
        def send(self, text: str):
            sent.append(text)
            return True, "ok"

    send_startup_report(service, Recorder(), lambda _line: None)
    assert "mode: EXECUTING ORDERS" in sent[0]
