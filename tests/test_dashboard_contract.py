"""The dashboard's contract with /api/status.

The dashboard is a static page that reads a fixed set of fields. If the service
renames or drops one, the page does not error — it silently renders an empty
panel, which is the failure mode that already shipped once as a blank page.

This asserts the fields the page depends on are actually produced, so a change
on either side has to be deliberate.
"""
from __future__ import annotations

import pytest

from rade.observer.service import MirrorService


class _Ledger:
    """Just enough ledger for /api/status."""

    def counts(self):
        return {"paper_intent": 1, "blocked": 2, "ignored": 64}

    def recent(self, *, limit=50, statuses=None):
        return []

    def list_trades(self, *, states=None):
        return []


def _status() -> dict:
    service = MirrorService(ledger=_Ledger(), token="t")
    code, payload = service.route("/api/status", {})
    assert code == 200, payload
    return payload


def _dig(payload: dict, path: str):
    node = payload
    for part in path.split("."):
        assert isinstance(node, dict), f"{path}: {part} reached a non-dict"
        assert part in node, f"/api/status no longer provides {path}"
        node = node[part]
    return node


@pytest.mark.parametrize("path", [
    # header and footer
    "paper_only", "store", "account_scope", "exit_policy", "uptime_s", "time",
    "counts",
    # badges
    "execution.enabled", "execution.paused",
    "config.notifier",
    # overview cards
    "listener.known", "listener.stale", "listener.phase",
    "listener.heartbeat_age_s", "listener.reason",
    # intake panel
    "intake.signal_max_age_seconds", "intake.quote_max_age_seconds",
    "intake.delivery_interval_seconds", "intake.delivery_within_age_limit",
    "intake.intake_ready", "intake.intake_problem",
    "intake.units_confirmed", "intake.max_stop_distance",
    "intake.sizing.mode", "intake.sizing.fixed_lots",
    "intake.sizing.max_trade_risk_usd", "intake.sizing.ready",
    "intake.sizing.problem", "intake.sizing.max_stop_distance",
])
def test_status_provides_every_field_the_dashboard_reads(path):
    _dig(_status(), path)


def test_an_execution_error_is_present_only_when_there_is_one():
    """The dashboard reads exec.error unguarded, so absence must mean no error.

    It is added conditionally by the service rather than always set to None,
    which is fine for a falsy read but is worth pinning: a change that made the
    key always present would not break the page, and a change that renamed it
    would silently disable the badge.
    """
    status = _status()
    assert "error" not in status["execution"]
    service = MirrorService(ledger=_Ledger(), token="t",
                            execution_error="BrokerRefused: wrong host")
    _, payload = service.route("/api/status", {})
    assert payload["execution"]["error"] == "BrokerRefused: wrong host"


def test_the_stop_width_is_a_number_when_the_rule_is_configured(monkeypatch):
    """The dashboard calls toFixed on this, so it must not be a string or None."""
    monkeypatch.setenv("MIRROR_FIXED_LOTS", "0.03")
    monkeypatch.setenv("MIRROR_MAX_TRADE_RISK_USD", "25")
    monkeypatch.setenv("MIRROR_CONTRACT_UNITS_PER_LOT", "100")
    monkeypatch.setenv("MIRROR_CONTRACT_UNITS_CONFIRMED", "1")
    monkeypatch.delenv("MIRROR_EXECUTE", raising=False)
    value = _dig(_status(), "intake.max_stop_distance")
    assert isinstance(value, float)
    assert value == pytest.approx(25.0 / 3.0)


def test_the_stop_width_is_null_rather_than_guessed_when_it_cannot_be_computed(
        monkeypatch):
    monkeypatch.setenv("MIRROR_FIXED_LOTS", "0.03")
    monkeypatch.setenv("MIRROR_MAX_TRADE_RISK_USD", "25")
    monkeypatch.delenv("MIRROR_CONTRACT_UNITS_PER_LOT", raising=False)
    monkeypatch.delenv("MIRROR_CONTRACT_UNITS_CONFIRMED", raising=False)
    assert _dig(_status(), "intake.max_stop_distance") is None
