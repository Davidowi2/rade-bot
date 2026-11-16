"""Regressions for three failures that all read as "the copier is fine".

* A cursor write that failed used to leave the in-memory cursor advanced past a
  message that was never claimed, so the store's cursor skipped it too.
* A crossed quote produced a negative spread, which passes every
  ``spread <= limit`` check, so the engine could send an order against it.
* The status page reported the sizing rule as ready while the contract size was
  unconfirmed and every signal was refused before it was ever sized.
"""
from __future__ import annotations

import pytest

from rade.execution.broker import BrokerQuote, BrokerSpec
from rade.execution.engine import ManagedTrade
from rade.execution.engine import ExecutionEngine
from rade.execution.ladder import LadderError, POLICY_HOLD_TO_FINAL, build_exit_plan
from rade.observer.listener import MirrorState
from rade.observer.service import MirrorService

NOW = __import__("datetime").datetime.now(__import__("datetime").timezone.utc)


# -- the cursor --------------------------------------------------------------


def test_a_failed_cursor_write_does_not_advance_the_cursor(tmp_path):
    state = MirrorState(tmp_path / "state.json")
    state.advance(10)

    def explode() -> None:
        raise OSError("the file is locked")

    state._write = explode  # type: ignore[assignment]
    with pytest.raises(OSError):
        state.advance(11)
    assert state.last_message_id == 10


def test_a_failed_cursor_write_leaves_no_tmp_file(tmp_path):
    """The leftover ``.tmp`` was the only trace of the failed write."""
    target = tmp_path / "state.json"
    state = MirrorState(target)
    assert state.last_message_id == 0

    not_a_dir = tmp_path / "file"
    not_a_dir.write_text("x", encoding="utf-8")
    # A directory that cannot be created makes the write fail for real.
    broken = MirrorState(not_a_dir / "state.json")
    with pytest.raises(OSError):
        broken.advance(1)
    assert broken.last_message_id == 0
    assert not (not_a_dir / "state.json.tmp").exists()


# -- the quote ---------------------------------------------------------------


class _LedgerStub:
    def record_transition(self, transition):
        return None

    def update_trade(self, row):
        return True

    def risk_state(self):
        return None

    def save_risk_state(self, **kwargs):
        return None

    def control(self):
        return None

    def list_trades(self, *, states=None):
        return []


class _BrokerStub:
    def __init__(self, quote):
        self._quote = quote

    def quote(self):
        return self._quote

    def equity(self):
        return 100_000.0


def _spec() -> BrokerSpec:
    return BrokerSpec(
        environment="demo", base_url="https://demo.tradelocker.com",
        account_id=1, account_number=1, symbol="XAUUSD", instrument_id=1808,
        trade_route_id="816807", contract_units_per_lot=100.0, lot_step=0.01,
        min_lot=0.01, max_lot=50.0, tick_size=0.01, symbol_status="TRADING",
        confirmed_contract_units=100.0, verified=True,
    )


def _trade() -> ManagedTrade:
    plan = build_exit_plan(side="buy", entry=4000.0, stop=3990.0,
                           targets=[4020.0], size=0.03, lot_step=0.01,
                           min_lot=0.01, policy=POLICY_HOLD_TO_FINAL)
    return ManagedTrade(
        trade_id="RADE-test", event_key="e" * 64, channel="@c", message_id=1,
        revision=0, symbol="XAUUSD", side="buy", size=0.03, entry=4000.0,
        stop=3990.0, targets=[4020.0], plan=plan,
    )


def _engine(quote: BrokerQuote) -> ExecutionEngine:
    return ExecutionEngine(broker=_BrokerStub(quote), ledger=_LedgerStub(),
                           notifier=None, log=lambda _msg: None,
                           fixed_lots=0.03, max_trade_risk_usd=50.0)


def test_a_crossed_quote_blocks_before_the_spread_check():
    crossed = BrokerQuote(bid=3903.11, ask=3863.40, quoted_at=NOW)
    outcome = _engine(crossed)._pre_trade_check(_trade(), _spec())
    assert outcome is not None
    assert outcome["guard"] is True
    assert "crossed" in outcome["reason"]


def test_a_non_positive_quote_blocks():
    broken = BrokerQuote(bid=0.0, ask=0.0, quoted_at=NOW)
    outcome = _engine(broken)._pre_trade_check(_trade(), _spec())
    assert outcome is not None
    assert "not positive" in outcome["reason"]


def test_a_healthy_quote_reaches_the_size_check():
    healthy = BrokerQuote(bid=3999.7, ask=4000.0, quoted_at=NOW)
    outcome = _engine(healthy)._pre_trade_check(_trade(), _spec())
    # The geometry is valid and inside the ceiling, so nothing blocks here.
    assert outcome is None


# -- what the status page says ----------------------------------------------


def test_unconfirmed_units_are_reported_as_not_ready(monkeypatch):
    """A configured rule must not read as a working intake."""
    monkeypatch.delenv("MIRROR_CONTRACT_UNITS_CONFIRMED", raising=False)
    monkeypatch.delenv("MIRROR_EXECUTE", raising=False)
    service = MirrorService(ledger=object(), token="t")
    intake = service.intake_limits()
    assert intake["units_confirmed"] is False
    assert intake["intake_ready"] is False
    assert "MIRROR_CONTRACT_UNITS_CONFIRMED" in intake["intake_problem"]
    # The rule itself is still a real rule; only the intake is blocked.
    assert intake["sizing"]["ready"] is True


def test_confirmed_units_report_the_intake_as_ready(monkeypatch):
    monkeypatch.setenv("MIRROR_CONTRACT_UNITS_CONFIRMED", "1")
    monkeypatch.delenv("MIRROR_EXECUTE", raising=False)
    monkeypatch.delenv("MIRROR_FIXED_LOTS", raising=False)
    service = MirrorService(ledger=object(), token="t")
    intake = service.intake_limits()
    assert intake["units_confirmed"] is True
    assert intake["intake_ready"] is True
    assert intake["intake_problem"] == ""


def test_the_intake_reports_the_stop_width_the_ceiling_allows(monkeypatch):
    """The mismatch between the lot and the ceiling must be visible, not silent."""
    monkeypatch.setenv("MIRROR_FIXED_LOTS", "0.03")
    monkeypatch.setenv("MIRROR_MAX_TRADE_RISK_USD", "25")
    monkeypatch.setenv("MIRROR_CONTRACT_UNITS_PER_LOT", "100")
    monkeypatch.setenv("MIRROR_CONTRACT_UNITS_CONFIRMED", "1")
    monkeypatch.delenv("MIRROR_EXECUTE", raising=False)
    intake = MirrorService(ledger=object(), token="t").intake_limits()
    assert intake["max_stop_distance"] == pytest.approx(25.0 / 3.0)


# -- the ladder's own geometry ----------------------------------------------


def test_a_stop_on_the_wrong_side_of_the_entry_is_refused():
    """The ladder is a second entry point, so it validates what it is handed.

    A plan built from a stop already triggered would be sent to the broker as
    a stop that is live on arrival.
    """
    with pytest.raises(LadderError) as excinfo:
        build_exit_plan(side="buy", entry=3900.0, stop=3910.0,
                        targets=[3920.0], size=0.03, lot_step=0.01, min_lot=0.01)
    assert "wrong side" in str(excinfo.value)


def test_a_target_on_the_wrong_side_of_the_entry_is_refused():
    with pytest.raises(LadderError) as excinfo:
        build_exit_plan(side="sell", entry=3900.0, stop=3910.0,
                        targets=[3890.0, 3905.0], size=0.03, lot_step=0.01,
                        min_lot=0.01)
    assert "wrong side" in str(excinfo.value)


def test_a_non_positive_entry_or_stop_is_refused():
    for kwargs in ({"entry": 0.0, "stop": 3990.0},
                   {"entry": 4000.0, "stop": 0.0},
                   {"entry": float("nan"), "stop": 3990.0}):
        with pytest.raises(LadderError):
            build_exit_plan(side="buy", targets=[4020.0], size=0.03,
                            lot_step=0.01, min_lot=0.01, **kwargs)
