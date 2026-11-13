"""Tests for the demo execution path.

Every test here runs against a double, not the broker. The spec requires
mocks and simulations before the demo account is touched, and the double is
deliberately a *broker* double rather than an SDK double: the engine is
written against the contract in ``rade.execution.broker``, so a double that
implements the contract proves the engine's logic rather than proving that an
imitation of the SDK works.

What the double models, because the engine depends on it:

* a market order fills immediately and creates a position;
* the position carries a stop loss and a take profit, and those are *orders*
  the broker can report, so the engine can verify a move instead of trusting
  the call's return value;
* those protective orders live in the *open-order* feed, while the filled
  entry order keeps the ``stopLoss``/``takeProfit`` it was submitted with and
  never updates them. That split is what the demo account actually does, and
  modelling it is the only way to test that verification reads the live level;
* a reduction changes the quantity, and closing the remainder removes the
  position.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from rade.execution.broker import (
    BrokerError,
    BrokerOrder,
    BrokerPosition,
    BrokerQuote,
    BrokerRefused,
    BrokerSpec,
    OrderAck,
)
from rade.execution.engine import (
    STATE_ACCEPTED,
    STATE_BLOCKED,
    STATE_CLOSED,
    STATE_NOT_COPIED,
    STATE_OPEN,
    STATE_PARTIAL,
    STATE_REJECTED,
    STATE_REVIEW,
    ExecutionEngine,
    ManagedTrade,
    trade_id_for,
)
from rade.execution.gateway import TradeLockerGateway, assert_demo_environment
from rade.execution.ladder import (
    LadderError,
    build_exit_plan,
    round_down_to_step,
    stop_move_is_risk_reducing,
    target_reached,
)
from rade.observer.mirror_pipeline import (
    InstrumentSpec,
    MarketQuote,
    MessageEnvelope,
    RiskConfig,
    SQLiteIntentLedger,
    evaluate_message,
)
from rade.observer.notify import RecordingNotifier

NOW = datetime(2026, 11, 13, 12, 0, tzinfo=timezone.utc)
INSTRUMENT = InstrumentSpec("XAUUSD", 100, 0.01, 0.01, 50, units_confirmed=True)
RISK = RiskConfig(10_000, 0.005, 2.0)

THREE_TARGETS = "XAUUSD SELL NOW 3915\nSl : 3925\nTp : 3910\nTp : 3905\nTp : 3900"
TWO_TARGETS = "XAUUSD SELL NOW 3915\nSl : 3925\nTp : 3910\nTp : 3905"
ONE_TARGET = "XAUUSD SELL NOW 3915\nSl : 3925\nTp : 3910"
LIMIT = "XAUUSD SELL LIMIT 3915\nSl : 3925\nTp : 3910\nTp : 3905\nTp : 3900"


def envelope(text=THREE_TARGETS, *, mid=100, revision=0):
    return MessageEnvelope("@source", mid, revision, NOW, NOW, text)


def evaluated(text=THREE_TARGETS, *, mid=100, quote=(3915.0, 3915.0)):
    return evaluate_message(
        envelope(text, mid=mid),
        instrument=INSTRUMENT,
        risk=RISK,
        quote=MarketQuote(quote[0], quote[1], NOW),
    )


# ---------------------------------------------------------------------------
# the broker double
# ---------------------------------------------------------------------------


class FakeBroker:
    """A broker that implements the contract and remembers what it was asked."""

    def __init__(self, *, verified: bool = True, bid: float = 3915.0,
                 ask: float = 3915.2):
        self.bid = bid
        self.ask = ask
        self._verified = verified
        self._unconfirmed = None if verified else "contract units are not confirmed"
        #: Entry orders, which the broker only ever shows in the history feed.
        #: Their ``stop_loss``/``take_profit`` are a snapshot taken at
        #: submission and never change afterwards.
        self.order_book: list[BrokerOrder] = []
        #: The protective stop and limit orders behind a position. These are
        #: the live levels, they live in the open-order feed, and this is what
        #: the engine must read.
        self.open_book: list[BrokerOrder] = []
        self.position_book: list[BrokerPosition] = []
        self.place_calls: list[dict] = []
        self.close_calls: list[dict] = []
        self.modify_calls: list[dict] = []
        self.fail_place = False
        self.fail_close = False
        self.fail_modify = False
        self.fail_orders = False
        self.fail_equity = False
        #: What ``equity()`` reports. The guards compare it against the day's
        #: opening balance and the peak, so tests move it to trip them.
        self.account_equity = 10_000.0
        self.reject_place = False
        self.next_order_id = 1000
        self.next_position_id = 5000
        self._next_level_id = 9000
        #: When set, a fill is withheld: the order is accepted but no position
        #: appears, which is what a rejected or unfilled order looks like.
        self.withhold_fill = False

    # -- contract ---------------------------------------------------------
    def spec(self) -> BrokerSpec:
        return BrokerSpec(
            environment="demo",
            base_url="https://demo.tradelocker.com",
            account_id=2509174,
            account_number=2,
            symbol="XAUUSD",
            instrument_id=1808,
            trade_route_id="1869",
            contract_units_per_lot=100.0,
            lot_step=0.01,
            min_lot=0.01,
            max_lot=50.0,
            tick_size=0.01,
            symbol_status="FULLY_OPEN",
            confirmed_contract_units=100.0 if self._verified else None,
            verified=self._verified,
            unconfirmed_reason=self._unconfirmed,
        )

    def quote(self) -> BrokerQuote:
        return BrokerQuote(self.bid, self.ask, datetime.now(timezone.utc))

    def equity(self) -> float:
        """The account equity the guards and sizing read. Matches ``RISK``."""
        if self.fail_equity:
            raise BrokerError("the account state could not be read")
        return self.account_equity

    def positions(self) -> list[BrokerPosition]:
        return list(self.position_book)

    def orders(self, *, history: bool = False) -> list[BrokerOrder]:
        if self.fail_orders:
            raise BrokerError("the broker is unreachable")
        return list(self.order_book if history else self.open_book)

    def place_market(self, *, qty, side, stop_loss, take_profit, strategy_id) -> OrderAck:
        self.place_calls.append({
            "qty": qty, "side": side, "stop_loss": stop_loss,
            "take_profit": take_profit, "strategy_id": strategy_id,
        })
        if self.fail_place:
            raise BrokerError("the broker is unreachable")
        if self.reject_place:
            return OrderAck(accepted=False, detail="Rejected: insufficient margin")
        order_id = self.next_order_id
        self.next_order_id += 1
        fill_price = self.ask if side == "buy" else self.bid
        position_id = None
        if not self.withhold_fill:
            position_id = self.next_position_id
            self.next_position_id += 1
            sl_id, tp_id = self._attach(position_id, stop_loss, take_profit,
                                        qty=float(qty))
            self.position_book.append(BrokerPosition(
                id=position_id, instrument_id=1808, side=side, qty=float(qty),
                avg_price=fill_price, stop_loss_id=sl_id, take_profit_id=tp_id,
                strategy_id=strategy_id,
            ))
        self.order_book.append(BrokerOrder(
            id=order_id, instrument_id=1808, side=side, qty=float(qty),
            status="Filled" if position_id else "New", type="market",
            filled_qty=float(qty) if position_id else 0.0,
            avg_price=fill_price if position_id else 0.0,
            position_id=position_id,
            # The snapshot the broker froze at submission. It is deliberately
            # kept forever so a test can prove nothing reads it.
            stop_loss=stop_loss if position_id else None,
            take_profit=take_profit if position_id else None,
            strategy_id=strategy_id,
        ))
        return OrderAck(accepted=True, order_id=order_id, detail="accepted")

    def modify_position(self, *, position_id, stop_loss=None, take_profit=None) -> bool:
        self.modify_calls.append({"position_id": position_id,
                                  "stop_loss": stop_loss, "take_profit": take_profit})
        if self.fail_modify:
            raise BrokerError("the broker refused the modification")
        position = self._position(position_id)
        if position is None:
            return False
        # The broker edits the protective order in place: same id, new level.
        if stop_loss is not None:
            self._set_level(position.stop_loss_id, "stop", float(stop_loss))
        if take_profit is not None:
            self._set_level(position.take_profit_id, "limit", float(take_profit))
        return True

    def close_quantity(self, *, position_id, qty) -> bool:
        self.close_calls.append({"position_id": position_id, "qty": qty})
        if self.fail_close:
            raise BrokerError("the broker refused the close")
        position = self._position(position_id)
        if position is None:
            return False
        if qty <= 0 or qty >= position.qty - 1e-9:
            self.position_book = [p for p in self.position_book if p.id != position_id]
            # Closing a position takes its protective orders with it.
            self.open_book = [o for o in self.open_book
                              if o.position_id != position_id]
            return True
        remaining = round(position.qty - qty, 8)
        self._replace(position, qty=remaining)
        self._resize_levels(position_id, remaining)
        return True

    # -- helpers ----------------------------------------------------------
    def _position(self, position_id):
        for position in self.position_book:
            if position.id == position_id:
                return position
        return None

    def _level(self, level_id, kind):
        for order in self.open_book:
            if order.id == level_id:
                return order.stop_price if kind == "stop" else order.price
        return None

    def _set_level(self, level_id, kind, value):
        for index, order in enumerate(self.open_book):
            if order.id != level_id:
                continue
            field = "stop_price" if kind == "stop" else "price"
            from dataclasses import replace
            self.open_book[index] = replace(order, **{field: float(value)})
            return True
        return False

    def _resize_levels(self, position_id, qty):
        from dataclasses import replace
        self.open_book = [
            replace(order, qty=float(qty)) if order.position_id == position_id
            else order
            for order in self.open_book
        ]

    def _attach(self, position_id, stop_loss, take_profit, qty=0.0):
        """Create the protective orders the way the broker does.

        A ``stop`` order carries its level in ``stopPrice``; a ``limit`` order
        carries it in ``price``. Neither uses ``stopLoss``/``takeProfit``,
        which is exactly the mistake this models.
        """
        sl_id = tp_id = None
        if stop_loss is not None:
            sl_id = self._next_level_id
            self._next_level_id += 1
            self.open_book.append(BrokerOrder(
                id=sl_id, instrument_id=1808, side="sell", qty=float(qty),
                status="New", type="stop", stop_price=float(stop_loss),
                position_id=position_id,
            ))
        if take_profit is not None:
            tp_id = self._next_level_id
            self._next_level_id += 1
            self.open_book.append(BrokerOrder(
                id=tp_id, instrument_id=1808, side="sell", qty=float(qty),
                status="New", type="limit", price=float(take_profit),
                position_id=position_id,
            ))
        return sl_id, tp_id

    def _replace(self, position, **changes):
        from dataclasses import replace
        index = self.position_book.index(position)
        self.position_book[index] = replace(position, **changes)


def engine_for(tmp_path, broker=None, *, notifier=None):
    ledger = SQLiteIntentLedger(tmp_path / "exec.sqlite")
    broker = broker or FakeBroker()
    return ExecutionEngine(broker=broker, ledger=ledger,
                            notifier=notifier or RecordingNotifier(),
                            log=lambda line: None), ledger, broker


# ---------------------------------------------------------------------------
# the ladder policy
# ---------------------------------------------------------------------------


def test_three_targets_close_thirds_with_the_documented_stop_moves():
    plan = build_exit_plan(side="buy", entry=3900, stop=3890,
                           targets=[3910, 3920, 3930], size=1.0,
                           lot_step=0.01, min_lot=0.01)
    assert plan.policy == "three_targets"
    assert plan.final_target == 3930
    assert [leg.label for leg in plan.legs] == ["tp1", "tp2", "tp3"]
    assert [leg.quantity for leg in plan.legs] == [0.33, 0.33, 0.34]
    assert sum(leg.quantity for leg in plan.legs) == pytest.approx(1.0)
    # TP1 moves the stop to breakeven; TP2 moves it to TP1.
    assert plan.legs[0].stop_after_kind == "breakeven"
    assert plan.legs[1].stop_after_kind == "target"
    assert plan.legs[1].stop_after_value == 3910
    # The final leg is closed by the broker's own target, not by us.
    assert plan.legs[2].managed is False
    assert plan.legs[0].managed and plan.legs[1].managed


def test_targets_beyond_the_third_are_ignored_and_recorded():
    plan = build_exit_plan(side="buy", entry=3900, stop=3890,
                           targets=[3910, 3920, 3930, 3940, 3950], size=1.0,
                           lot_step=0.01, min_lot=0.01)
    assert plan.final_target == 3930
    assert plan.ignored_targets == (3940.0, 3950.0)
    assert "ignored" in plan.description()


def test_two_targets_close_halves_with_breakeven_after_the_first():
    plan = build_exit_plan(side="buy", entry=3900, stop=3890,
                           targets=[3910, 3920], size=1.0,
                           lot_step=0.01, min_lot=0.01)
    assert plan.policy == "two_targets"
    assert [leg.quantity for leg in plan.legs] == [0.5, 0.5]
    assert plan.legs[0].stop_after_kind == "breakeven"
    assert plan.legs[1].managed is False


def test_one_target_closes_everything():
    plan = build_exit_plan(side="buy", entry=3900, stop=3890,
                           targets=[3910], size=1.0, lot_step=0.01, min_lot=0.01)
    assert plan.policy == "one_target"
    assert len(plan.legs) == 1
    assert plan.legs[0].quantity == 1.0
    assert plan.legs[0].managed is False


def test_a_third_below_the_broker_minimum_blocks_the_ladder():
    # 0.02 / 3 rounds down to 0.00, which is below the broker's 0.01.
    with pytest.raises(LadderError, match="below the broker"):
        build_exit_plan(side="buy", entry=3900, stop=3890,
                        targets=[3910, 3920, 3930], size=0.02,
                        lot_step=0.01, min_lot=0.01)


def test_the_ladder_never_leaves_quantity_unaccounted_for():
    plan = build_exit_plan(side="buy", entry=3900, stop=3890,
                           targets=[3910, 3920, 3930], size=0.07,
                           lot_step=0.01, min_lot=0.01)
    assert sum(leg.quantity for leg in plan.legs) == pytest.approx(0.07)


def test_rounding_is_always_down():
    assert round_down_to_step(0.3339, 0.01) == 0.33
    assert round_down_to_step(0.339, 0.01) == 0.33
    assert round_down_to_step(1.0, 0.01) == 1.0


def test_a_stop_may_only_move_towards_the_price():
    ok, _ = stop_move_is_risk_reducing(side="buy", old_stop=3890,
                                       new_stop=3900, price=3915)
    assert ok
    widened, reason = stop_move_is_risk_reducing(side="buy", old_stop=3900,
                                                 new_stop=3890, price=3915)
    assert not widened and "widen" in reason
    past, reason = stop_move_is_risk_reducing(side="buy", old_stop=3890,
                                              new_stop=3920, price=3915)
    assert not past and "below the market" in reason


def test_a_short_stop_may_only_move_down():
    ok, _ = stop_move_is_risk_reducing(side="sell", old_stop=3925,
                                       new_stop=3915, price=3910)
    assert ok
    widened, _ = stop_move_is_risk_reducing(side="sell", old_stop=3915,
                                            new_stop=3925, price=3910)
    assert not widened
    past, _ = stop_move_is_risk_reducing(side="sell", old_stop=3925,
                                         new_stop=3905, price=3910)
    assert not past


def test_a_target_is_reached_at_the_price_the_close_would_get():
    # A long is sold at the bid; a short is bought back at the ask.
    assert target_reached(side="buy", target=3910, bid=3910.0, ask=3912.0)
    assert not target_reached(side="buy", target=3910, bid=3909.9, ask=3912.0)
    assert target_reached(side="sell", target=3910, bid=3908.0, ask=3910.0)
    assert not target_reached(side="sell", target=3910, bid=3908.0, ask=3910.1)


# ---------------------------------------------------------------------------
# the demo gate
# ---------------------------------------------------------------------------


def test_a_live_environment_is_refused():
    with pytest.raises(BrokerRefused, match="non-demo"):
        assert_demo_environment("live", "https://live.tradelocker.com")


def test_a_demo_name_pointing_at_the_live_host_is_refused():
    with pytest.raises(BrokerRefused, match="not the demo host"):
        assert_demo_environment("demo", "https://live.tradelocker.com")


def test_the_demo_environment_is_accepted():
    assert_demo_environment("demo", "https://demo.tradelocker.com")


class _FakeClient:
    """The smallest client the gateway will accept."""

    account_id = 2509174
    acc_num = 2

    def __init__(self, details=None, accounts=None):
        self._details = details if details is not None else {
            "lotSize": 100, "lotStep": 0.01, "minLot": 0.01, "maxLot": 50,
            "tickSize": [{"tickSize": 0.01}], "symbolStatus": "FULLY_OPEN",
        }
        self._accounts = accounts if accounts is not None else [
            {"id": 2509174, "accNum": 2, "currency": "USD"},
        ]
        self.created: list[dict] = []

    def get_instrument_id_from_symbol_name(self, symbol):
        return 1808

    def get_instrument_details(self, instrument_id):
        return self._details

    def get_trade_route_id(self, instrument_id):
        return "1869"

    def get_trade_accounts(self):
        return self._accounts

    def get_latest_bid_price(self, instrument_id):
        return 3915.0

    def get_latest_asking_price(self, instrument_id):
        return 3915.2

    def create_order(self, **kwargs):
        self.created.append(kwargs)
        return 1234

    def modify_position(self, position_id, params):
        return True

    def close_position(self, position_id=0, close_quantity=0):
        return True

    def get_all_positions(self):
        return None

    def get_all_orders(self, history=False):
        return None


def gateway_for(client=None, *, confirmed=100.0, env="demo",
                base_url="https://demo.tradelocker.com"):
    return TradeLockerGateway(client or _FakeClient(), environment=env,
                              base_url=base_url, confirmed_contract_units=confirmed,
                              log=lambda line: None)


def test_the_specification_comes_from_the_broker():
    spec = gateway_for().spec()
    assert spec.verified
    assert spec.instrument_id == 1808
    assert spec.trade_route_id == "1869"
    assert spec.account_number == 2
    assert spec.contract_units_per_lot == 100
    assert spec.lot_step == 0.01
    assert spec.min_lot == 0.01
    assert spec.max_lot == 50


def test_an_unconfirmed_contract_size_leaves_the_specification_unverified():
    spec = gateway_for(confirmed=None).spec()
    assert not spec.verified
    assert "not been confirmed" in spec.unconfirmed_reason


def test_a_contract_size_that_disagrees_with_the_broker_is_unconfirmed():
    spec = gateway_for(confirmed=1.0).spec()
    assert not spec.verified
    assert "disagree" in spec.unconfirmed_reason


def test_a_missing_contract_size_blocks_the_specification():
    client = _FakeClient(details={"lotStep": 0.01, "minLot": 0.01, "maxLot": 50})
    spec = gateway_for(client).spec()
    assert not spec.verified
    assert "contract size" in spec.unconfirmed_reason


def test_a_closed_symbol_blocks_the_specification():
    client = _FakeClient(details={
        "lotSize": 100, "lotStep": 0.01, "minLot": 0.01, "maxLot": 50,
        "tickSize": [{"tickSize": 0.01}], "symbolStatus": "CLOSED",
    })
    spec = gateway_for(client).spec()
    assert not spec.verified
    assert "CLOSED" in spec.unconfirmed_reason


def test_an_unverified_specification_cannot_place_an_order():
    gateway = gateway_for(confirmed=None)
    with pytest.raises(BrokerRefused, match="not verified"):
        gateway.place_market(qty=0.1, side="buy", stop_loss=3900,
                             take_profit=3920, strategy_id="RADE-x")


def test_the_order_carries_the_stop_and_the_final_target():
    client = _FakeClient()
    gateway = gateway_for(client)
    ack = gateway.place_market(qty=0.1, side="buy", stop_loss=3900,
                               take_profit=3920, strategy_id="RADE-abc")
    assert ack.accepted and ack.order_id == 1234
    sent = client.created[0]
    assert sent["stop_loss"] == 3900
    assert sent["stop_loss_type"] == "absolute"
    assert sent["take_profit"] == 3920
    assert sent["take_profit_type"] == "absolute"
    assert sent["validity"] == "IOC"
    assert sent["strategy_id"] == "RADE-abc"


# ---------------------------------------------------------------------------
# the engine
# ---------------------------------------------------------------------------


def test_a_limit_signal_is_recorded_as_not_copied(tmp_path):
    engine, ledger, broker = engine_for(tmp_path)
    result = evaluated(LIMIT)
    assert result.status == "paper_intent"

    outcome = engine.execute(result, envelope(LIMIT))

    assert outcome["executed"] is False
    assert outcome["state"] == STATE_NOT_COPIED
    assert "LIMIT" in outcome["reason"]
    assert broker.place_calls == []
    rows = ledger.list_trades()
    assert rows and rows[0]["state"] == STATE_NOT_COPIED


def test_an_unverified_broker_specification_blocks_the_order(tmp_path):
    engine, ledger, broker = engine_for(tmp_path, FakeBroker(verified=False))
    outcome = engine.execute(evaluated(), envelope())

    assert outcome["executed"] is False
    assert outcome["state"] == STATE_BLOCKED
    assert broker.place_calls == []
    assert "not confirmed" in outcome["reason"]


def test_a_market_signal_is_submitted_with_the_correlation_tag(tmp_path):
    engine, _ledger, broker = engine_for(tmp_path)
    result = evaluated()
    outcome = engine.execute(result, envelope())

    assert outcome["executed"] is True
    assert outcome["state"] == STATE_ACCEPTED
    call = broker.place_calls[0]
    assert call["strategy_id"] == trade_id_for(result.event_key)
    assert len(call["strategy_id"]) <= 31
    # The stop and the final target are attached at entry.
    assert call["stop_loss"] == 3925
    assert call["take_profit"] == 3900


def test_the_fill_is_only_recorded_when_the_position_exists(tmp_path):
    broker = FakeBroker()
    engine, ledger, _ = engine_for(tmp_path, broker)
    result = evaluated()

    engine.execute(result, envelope())
    trade = ManagedTrade.from_row(ledger.get_trade(trade_id_for(result.event_key)))
    assert trade.state == STATE_ACCEPTED

    outcome = engine.confirm_fill(trade)
    assert outcome["filled"] is True
    trade = ManagedTrade.from_row(ledger.get_trade(trade.trade_id))
    assert trade.state == STATE_OPEN
    assert trade.broker_position_id is not None
    assert trade.fill_price == pytest.approx(3915.0)


def test_a_rejected_order_is_recorded_as_rejected(tmp_path):
    broker = FakeBroker()
    broker.reject_place = True
    engine, ledger, _ = engine_for(tmp_path, broker)

    outcome = engine.execute(evaluated(), envelope())
    assert outcome["executed"] is False
    assert outcome["state"] == STATE_REJECTED


def test_an_order_is_never_resubmitted_after_a_restart(tmp_path):
    broker = FakeBroker()
    engine, ledger, _ = engine_for(tmp_path, broker)
    result = evaluated()
    engine.execute(result, envelope())
    assert len(broker.place_calls) == 1

    # A second engine over the same ledger is a restart.
    restarted = ExecutionEngine(broker=broker, ledger=ledger,
                                notifier=RecordingNotifier(), log=lambda line: None)
    restarted.execute(result, envelope())

    assert len(broker.place_calls) == 1, "the order was placed twice"


def test_tp1_closes_a_third_and_moves_the_stop_to_breakeven(tmp_path):
    broker = FakeBroker()
    engine, ledger, _ = engine_for(tmp_path, broker)
    result = evaluated()
    engine.execute(result, envelope())
    trade = ManagedTrade.from_row(ledger.get_trade(trade_id_for(result.event_key)))
    engine.confirm_fill(trade)

    # Price reaches TP1 (3910) for a short: the ask falls to 3910.
    broker.bid, broker.ask = 3909.8, 3910.0
    outcome = engine.manage_trade(ManagedTrade.from_row(
        ledger.get_trade(trade.trade_id)))

    assert outcome["partial"] is True
    assert broker.close_calls[0]["qty"] == pytest.approx(0.01)
    # Breakeven is the real fill price, not the published entry.
    assert broker.modify_calls[0]["stop_loss"] == pytest.approx(3915.0)
    trade = ManagedTrade.from_row(ledger.get_trade(trade.trade_id))
    assert trade.state == STATE_PARTIAL
    assert trade.legs_done == 1
    assert trade.current_stop == pytest.approx(3915.0)


def test_tp2_closes_another_third_and_moves_the_stop_to_tp1(tmp_path):
    broker = FakeBroker()
    engine, ledger, _ = engine_for(tmp_path, broker)
    result = evaluated()
    engine.execute(result, envelope())
    engine.confirm_fill(ManagedTrade.from_row(
        ledger.get_trade(trade_id_for(result.event_key))))

    broker.bid, broker.ask = 3909.8, 3910.0
    engine.manage_trade(ManagedTrade.from_row(ledger.get_trade(
        trade_id_for(result.event_key))))

    # Price reaches TP2 (3905).
    broker.bid, broker.ask = 3904.8, 3905.0
    outcome = engine.manage_trade(ManagedTrade.from_row(ledger.get_trade(
        trade_id_for(result.event_key))))

    assert outcome["partial"] is True
    trade = ManagedTrade.from_row(ledger.get_trade(trade_id_for(result.event_key)))
    assert trade.legs_done == 2
    assert trade.current_stop == pytest.approx(3910.0)


def test_the_final_target_closes_the_remainder(tmp_path):
    broker = FakeBroker()
    engine, ledger, _ = engine_for(tmp_path, broker)
    result = evaluated()
    engine.execute(result, envelope())
    engine.confirm_fill(ManagedTrade.from_row(
        ledger.get_trade(trade_id_for(result.event_key))))

    for bid, ask in ((3909.8, 3910.0), (3904.8, 3905.0)):
        broker.bid, broker.ask = bid, ask
        engine.manage_trade(ManagedTrade.from_row(ledger.get_trade(
            trade_id_for(result.event_key))))

    # Price reaches TP3 (3900); the position is closed.
    broker.bid, broker.ask = 3899.8, 3900.0
    engine.manage_trade(ManagedTrade.from_row(ledger.get_trade(
        trade_id_for(result.event_key))))

    trade = ManagedTrade.from_row(ledger.get_trade(trade_id_for(result.event_key)))
    assert trade.state in {STATE_CLOSED, STATE_PARTIAL}
    assert broker.position_book == []


def test_a_close_below_the_broker_minimum_stops_management(tmp_path):
    broker = FakeBroker()
    engine, ledger, _ = engine_for(tmp_path, broker)
    result = evaluated()
    engine.execute(result, envelope())
    engine.confirm_fill(ManagedTrade.from_row(
        ledger.get_trade(trade_id_for(result.event_key))))

    # Shrink the position so the next close would be below the minimum.
    trade = ManagedTrade.from_row(ledger.get_trade(trade_id_for(result.event_key)))
    broker._replace(broker.position_book[0], qty=0.005)
    broker.bid, broker.ask = 3909.8, 3910.0

    outcome = engine.manage_trade(trade)
    assert outcome["stopped"] is True
    trade = ManagedTrade.from_row(ledger.get_trade(trade.trade_id))
    assert trade.state == STATE_REVIEW
    assert trade.management_stopped is True


def test_a_position_that_no_longer_matches_the_plan_stops_management(tmp_path):
    broker = FakeBroker()
    engine, ledger, _ = engine_for(tmp_path, broker)
    result = evaluated()
    engine.execute(result, envelope())
    engine.confirm_fill(ManagedTrade.from_row(
        ledger.get_trade(trade_id_for(result.event_key))))

    trade = ManagedTrade.from_row(ledger.get_trade(trade_id_for(result.event_key)))
    # Fewer lots than the next step needs.
    broker._replace(broker.position_book[0], qty=0.001)
    broker.bid, broker.ask = 3909.8, 3910.0

    outcome = engine.manage_trade(trade)
    assert outcome["stopped"] is True
    assert "expects to close" in outcome["reason"]


def test_a_stop_that_would_widen_the_risk_stops_management(tmp_path):
    broker = FakeBroker()
    engine, ledger, _ = engine_for(tmp_path, broker)
    result = evaluated()
    engine.execute(result, envelope())
    engine.confirm_fill(ManagedTrade.from_row(
        ledger.get_trade(trade_id_for(result.event_key))))

    trade = ManagedTrade.from_row(ledger.get_trade(trade_id_for(result.event_key)))
    # Pretend the stop already sits better than breakeven would put it.
    trade.current_stop = 3910.0
    broker.bid, broker.ask = 3909.8, 3910.0

    outcome = engine.manage_trade(trade)
    assert outcome["stopped"] is True
    assert "widen" in outcome["reason"] or "refused" in outcome["reason"]


def test_reconciliation_closes_a_trade_that_ended_while_the_process_was_down(tmp_path):
    broker = FakeBroker()
    engine, ledger, _ = engine_for(tmp_path, broker)
    result = evaluated()
    engine.execute(result, envelope())
    engine.confirm_fill(ManagedTrade.from_row(
        ledger.get_trade(trade_id_for(result.event_key))))

    # The position disappears while nothing is watching.
    broker.position_book = []
    summary = engine.reconcile()
    assert summary["closed"] == 1
    trade = ManagedTrade.from_row(ledger.get_trade(trade_id_for(result.event_key)))
    assert trade.state == STATE_CLOSED


def test_reconciliation_stops_management_when_the_target_is_not_the_plan(tmp_path):
    broker = FakeBroker()
    engine, ledger, _ = engine_for(tmp_path, broker)
    result = evaluated()
    engine.execute(result, envelope())
    engine.confirm_fill(ManagedTrade.from_row(
        ledger.get_trade(trade_id_for(result.event_key))))

    # Someone changed the target by hand at the broker.
    trade = ManagedTrade.from_row(ledger.get_trade(trade_id_for(result.event_key)))
    engine.broker.modify_position(position_id=trade.broker_position_id,
                                  take_profit=3905.0)

    summary = engine.reconcile()
    assert summary["review"] == 1
    trade = ManagedTrade.from_row(ledger.get_trade(trade.trade_id))
    assert trade.state == STATE_REVIEW
    assert "final target" in trade.note


def test_reconciliation_ignores_positions_this_project_does_not_own(tmp_path):
    broker = FakeBroker()
    engine, ledger, _ = engine_for(tmp_path, broker)
    # A manual position with no correlation tag.
    broker.position_book.append(BrokerPosition(
        id=1, instrument_id=1808, side="buy", qty=1.0, avg_price=3900.0,
        strategy_id=None))

    summary = engine.reconcile()
    assert summary["considered"] == 0
    assert broker.position_book[0].strategy_id is None


def test_every_lifecycle_stage_is_recorded_as_a_transition(tmp_path):
    broker = FakeBroker()
    notifier = RecordingNotifier()
    engine, ledger, _ = engine_for(tmp_path, broker, notifier=notifier)
    result = evaluated()
    engine.execute(result, envelope())
    engine.confirm_fill(ManagedTrade.from_row(
        ledger.get_trade(trade_id_for(result.event_key))))

    stages = [t["stage"] for t in ledger.transitions(trade_id_for(result.event_key))]
    assert "validated" in stages
    assert "submit" in stages
    assert "accept" in stages
    assert "fill" in stages
    # The transitions carry the Telegram id and the broker ids.
    fill = [t for t in ledger.transitions(trade_id_for(result.event_key))
            if t["stage"] == "fill"][0]
    assert fill["message_id"] == 100
    assert fill["broker_position_id"] is not None


def test_copied_is_never_said_before_the_broker_confirms(tmp_path):
    broker = FakeBroker()
    notifier = RecordingNotifier()
    engine, ledger, _ = engine_for(tmp_path, broker, notifier=notifier)
    result = evaluated()

    engine.execute(result, envelope())
    sent_before_fill = list(notifier.sent)
    assert not any("COPIED" in text.upper() for text in sent_before_fill)
    assert any("ACCEPTED" in text for text in sent_before_fill)

    engine.confirm_fill(ManagedTrade.from_row(
        ledger.get_trade(trade_id_for(result.event_key))))
    assert any("FILLED" in text for text in notifier.sent)


def test_each_lifecycle_event_gets_its_own_notification(tmp_path):
    broker = FakeBroker()
    notifier = RecordingNotifier()
    engine, ledger, _ = engine_for(tmp_path, broker, notifier=notifier)
    result = evaluated()
    engine.execute(result, envelope())
    engine.confirm_fill(ManagedTrade.from_row(
        ledger.get_trade(trade_id_for(result.event_key))))
    broker.bid, broker.ask = 3909.8, 3910.0
    engine.manage_trade(ManagedTrade.from_row(ledger.get_trade(
        trade_id_for(result.event_key))))

    kinds = [text.split("]")[0].strip("[") for text in notifier.sent]
    assert "SIGNAL" in kinds
    assert "ACCEPTED" in kinds
    assert "FILLED" in kinds
    assert "TP1 HIT" in kinds
    assert "STOP MOVED" in kinds


def test_the_correlation_tag_is_stable_and_bounded():
    key = "a" * 64
    assert trade_id_for(key) == trade_id_for(key)
    assert len(trade_id_for(key)) <= 31
    assert trade_id_for("short") != trade_id_for("short2")


def test_a_refused_stop_move_is_retried_before_the_ladder_advances(tmp_path):
    """A stop that could not be applied must not be skipped.

    The broker can refuse a modification when the price has moved between the
    close and the request. Advancing the ladder past that point would lose the
    move for good, so the next pass retries it first.
    """
    broker = FakeBroker()
    engine, ledger, _ = engine_for(tmp_path, broker)
    result = evaluated()
    engine.execute(result, envelope())
    engine.confirm_fill(ManagedTrade.from_row(
        ledger.get_trade(trade_id_for(result.event_key))))
    trade_id = trade_id_for(result.event_key)

    broker.fail_modify = True
    broker.bid, broker.ask = 3909.8, 3910.0
    first = engine.manage_trade(ManagedTrade.from_row(ledger.get_trade(trade_id)))

    assert first.get("retry") is True
    trade = ManagedTrade.from_row(ledger.get_trade(trade_id))
    assert trade.legs_done == 1, "the close happened"
    assert trade.current_stop == pytest.approx(3925.0), "the stop did not move"

    # The broker recovers. The next pass applies the pending stop, and does not
    # jump ahead to the second target.
    broker.fail_modify = False
    second = engine.manage_trade(ManagedTrade.from_row(ledger.get_trade(trade_id)))
    assert second.get("stop_retried") == "tp1"
    trade = ManagedTrade.from_row(ledger.get_trade(trade_id))
    assert trade.current_stop == pytest.approx(3915.0)
    assert trade.legs_done == 1


def test_the_stop_is_read_from_the_live_order_not_the_entry_snapshot(tmp_path):
    """The entry order's stop is frozen at submission; the live one is elsewhere.

    The demo broker keeps ``stopLoss`` on the filled entry order exactly as it
    was sent, and publishes the level that actually protects the position on
    the linked ``stop`` order in the open-order feed. Reading the frozen field
    would confirm a stop move that never reached the broker, which is the one
    failure this project cannot tolerate.
    """
    broker = FakeBroker()
    engine, ledger, _ = engine_for(tmp_path, broker)
    result = evaluated()
    engine.execute(result, envelope())
    trade_id = trade_id_for(result.event_key)
    engine.confirm_fill(ManagedTrade.from_row(ledger.get_trade(trade_id)))

    entry = broker.order_book[0]
    frozen = entry.stop_loss
    assert frozen == pytest.approx(3925.0)

    broker.bid, broker.ask = 3909.8, 3910.0
    engine.manage_trade(ManagedTrade.from_row(ledger.get_trade(trade_id)))

    position = broker.position_book[0]
    levels = engine._attached_levels(position)
    assert levels.known is True
    assert levels.stop == pytest.approx(3915.0), "the live stop moved"
    # The snapshot is deliberately unchanged, so the assertion above can only
    # pass if the level came from the open stop order.
    assert broker.order_book[0].stop_loss == pytest.approx(frozen)
    assert levels.take_profit == pytest.approx(3900.0)


def test_an_unreadable_open_order_feed_never_confirms_a_stop_move(tmp_path):
    """Unknown is not the same as applied.

    If the open-order feed cannot be read, the stop may or may not have moved.
    Verification must fail, and the failed move must be retried rather than
    assumed, even though ``modify_position`` itself returned success.
    """
    broker = FakeBroker()
    engine, ledger, _ = engine_for(tmp_path, broker)
    result = evaluated()
    engine.execute(result, envelope())
    trade_id = trade_id_for(result.event_key)
    engine.confirm_fill(ManagedTrade.from_row(ledger.get_trade(trade_id)))

    broker.fail_orders = True
    broker.bid, broker.ask = 3909.8, 3910.0
    outcome = engine.manage_trade(ManagedTrade.from_row(ledger.get_trade(trade_id)))

    assert outcome.get("retry") is True
    assert "could not be read" in outcome["reason"]
    trade = ManagedTrade.from_row(ledger.get_trade(trade_id))
    assert trade.current_stop == pytest.approx(3925.0), "the stop is not recorded as moved"


def test_reconciliation_refuses_to_judge_levels_it_cannot_read(tmp_path):
    """After an outage the levels are unknown, and management stops for review.

    The broker's own stop and target keep protecting the position, but the
    ladder ordering cannot be established, so nothing automatic continues.
    """
    broker = FakeBroker()
    engine, ledger, _ = engine_for(tmp_path, broker)
    result = evaluated()
    engine.execute(result, envelope())
    trade_id = trade_id_for(result.event_key)
    engine.confirm_fill(ManagedTrade.from_row(ledger.get_trade(trade_id)))

    broker.fail_orders = True
    outcome = engine.reconcile_trade(ManagedTrade.from_row(ledger.get_trade(trade_id)))

    assert outcome.get("stopped") is True
    trade = ManagedTrade.from_row(ledger.get_trade(trade_id))
    assert trade.state == STATE_REVIEW
    assert "could not be read" in trade.note


def test_a_fill_beyond_the_first_target_stops_before_any_leg(tmp_path):
    """Adverse slippage can put the fill past TP1, and the ladder is then invalid.

    The published ladder says "after TP2 move the stop to TP1". If the fill is
    already beyond TP1, that stop is behind breakeven and the move would have to
    *widen* the risk. The policy is not rewritten to fit the fill: management
    stops at the fill and the broker-side stop and target stay attached.

    The demo account produced exactly this — published entry 3887.59, fill
    3888.33, TP1 3887.79 — which is why the check exists.
    """
    broker = FakeBroker(bid=3915.0, ask=3915.2)
    engine, ledger, _ = engine_for(tmp_path, broker)
    # A SELL whose TP1 sits close to the entry, so an adverse fill can pass it
    # while still being inside the slippage limit. A gap wide enough to trip
    # the slippage guard would test that guard instead of this one.
    text = "XAUUSD SELL NOW 3915\nSl : 3925\nTp : 3912\nTp : 3905\nTp : 3900"
    result = evaluated(text)
    # The broker fills the short at 3911.4, already past TP1 (3912).
    broker.bid, broker.ask = 3911.4, 3911.6
    engine.execute(result, envelope(text))
    trade_id = trade_id_for(result.event_key)

    outcome = engine.confirm_fill(ManagedTrade.from_row(ledger.get_trade(trade_id)))

    assert outcome.get("stopped") is True
    assert "beyond the first target" in outcome["reason"]
    trade = ManagedTrade.from_row(ledger.get_trade(trade_id))
    assert trade.state == STATE_REVIEW
    assert trade.legs_done == 0
    assert broker.close_calls == [], "no leg may run on an invalid ladder"
    assert broker.modify_calls == [], "the stop must not be moved"


def test_a_stop_that_keeps_failing_eventually_asks_a_human(tmp_path):
    broker = FakeBroker()
    engine, ledger, _ = engine_for(tmp_path, broker)
    result = evaluated()
    engine.execute(result, envelope())
    engine.confirm_fill(ManagedTrade.from_row(
        ledger.get_trade(trade_id_for(result.event_key))))
    trade_id = trade_id_for(result.event_key)

    broker.fail_modify = True
    broker.bid, broker.ask = 3909.8, 3910.0
    outcome = engine.manage_trade(ManagedTrade.from_row(ledger.get_trade(trade_id)))
    for _ in range(engine.max_stop_attempts + 2):
        if outcome.get("stopped"):
            break
        outcome = engine.manage_trade(ManagedTrade.from_row(ledger.get_trade(trade_id)))

    assert outcome.get("stopped") is True
    trade = ManagedTrade.from_row(ledger.get_trade(trade_id))
    assert trade.state == STATE_REVIEW
    assert "still protected" in trade.note
