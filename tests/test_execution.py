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
    ACTIVE_STATES,
    STATE_ACCEPTED,
    STATE_BLOCKED,
    STATE_CLOSED,
    STATE_NOT_COPIED,
    STATE_OPEN,
    STATE_PARTIAL,
    STATE_REJECTED,
    STATE_REVIEW,
    STATE_SUBMITTED,
    STATE_VALIDATED,
    ExecutionEngine,
    ManagedTrade,
    trade_id_for,
)
from rade.execution.gateway import TradeLockerGateway, assert_demo_environment, assert_destination
from rade.execution.guards import RiskLimits, utc_day
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
#: The confirmed sizing rule, used by the pipeline and the engine alike. The
#: stop on the sample signals is 10.00, so 0.03 lots is $30.00 of risk against a
#: $50.00 ceiling - inside it, but not by so much that a slip would hide.
FIXED_LOTS = 0.03
MAX_RISK_USD = 50.0
RISK = RiskConfig(10_000, 0.005, 2.0, fixed_lots=FIXED_LOTS,
                  max_trade_risk_usd=MAX_RISK_USD)

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
        #: A read failure, not an empty account. The two must never be
        #: confused: one is evidence, the other is the absence of evidence.
        self.fail_positions = False
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
        if self.fail_positions:
            raise BrokerError(
                "reading positions failed: HTTPError: 429 Client Error: "
                "Too Many Requests")
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
        # If the order is gone, it creates a new one and repoints the position.
        if stop_loss is not None:
            if position.stop_loss_id is None:
                self._reattach(position, "stop", stop_loss)
            else:
                self._set_level(position.stop_loss_id, "stop", float(stop_loss))
        if take_profit is not None:
            if position.take_profit_id is None:
                self._reattach(position, "limit", take_profit)
            else:
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

    def _reattach(self, position, kind, value):
        """Put a dropped protective order back, the way the broker does.

        The broker creates a fresh order and points the position at it, so the
        id changes - which is exactly why the engine reads the position back
        rather than trusting the modification call's return value.
        """
        level_id = self._next_level_id
        self._next_level_id += 1
        if kind == "stop":
            self.open_book.append(BrokerOrder(
                id=level_id, instrument_id=1808, side="sell", qty=position.qty,
                status="New", type="stop", stop_price=float(value),
                position_id=position.id))
            self._replace(position, stop_loss_id=level_id)
        else:
            self.open_book.append(BrokerOrder(
                id=level_id, instrument_id=1808, side="sell", qty=position.qty,
                status="New", type="limit", price=float(value),
                position_id=position.id))
            self._replace(position, take_profit_id=level_id)
        return True

    def _replace(self, position, **changes):
        from dataclasses import replace
        index = self.position_book.index(position)
        self.position_book[index] = replace(position, **changes)


def engine_for(tmp_path, broker=None, *, notifier=None, exit_policy=None, **kwargs):
    ledger = SQLiteIntentLedger(tmp_path / "exec.sqlite")
    broker = broker or FakeBroker()
    extra = {} if exit_policy is None else {"exit_policy": exit_policy}
    kwargs.setdefault("fixed_lots", FIXED_LOTS)
    kwargs.setdefault("max_trade_risk_usd", MAX_RISK_USD)
    return ExecutionEngine(broker=broker, ledger=ledger,
                            notifier=notifier or RecordingNotifier(),
                            log=lambda line: None, **extra, **kwargs), ledger, broker


# ---------------------------------------------------------------------------
# the guards' preflight
# ---------------------------------------------------------------------------


def test_the_preflight_reports_the_room_left_without_seeding_state(tmp_path):
    """The drawdown and daily breaker must be visible before the first trade.

    The risk state is only written when a signal is evaluated, so on a quiet
    deployment it is empty; without this the dashboard shows nothing about the
    room the account has left. Reporting it must not write anything.
    """
    broker = FakeBroker()
    engine, ledger, _ = engine_for(tmp_path, broker)
    assert ledger.risk_state() is None

    room = engine.guard_preflight()["room"]

    assert room["stored"] is False
    assert room["equity"] == pytest.approx(broker.equity())
    assert room["peak_equity"] == pytest.approx(broker.equity())
    # The tight defaults: 3.35% drawdown, 1% daily.
    assert room["drawdown_room"] == pytest.approx(broker.equity() * 0.0335, abs=0.01)
    assert room["daily_room"] == pytest.approx(broker.equity() * 0.01, abs=0.01)
    assert room["drawdown_floor"] == pytest.approx(broker.equity() * 0.9665, abs=0.01)
    assert ledger.risk_state() is None, "the preflight must not seed the state"


def test_the_reported_room_uses_the_stored_peak_once_there_is_one(tmp_path):
    """A stored high-water mark must be used, not today's equity."""
    broker = FakeBroker()
    engine, ledger, _ = engine_for(tmp_path, broker)
    ledger.save_risk_state(day=utc_day(), day_start_equity=10_000.0,
                           peak_equity=10_000.0)

    room = engine.guard_preflight()["room"]

    assert room["stored"] is True
    assert room["peak_equity"] == 10_000.0
    assert room["drawdown_floor"] == pytest.approx(10_000.0 * 0.9665, abs=0.01)


# ---------------------------------------------------------------------------
# the ladder policy
# ---------------------------------------------------------------------------


def test_the_confirmed_policy_holds_through_tp1_and_closes_at_tp3():
    """No partial at TP1 or TP2: one stop move, one exit."""
    plan = build_exit_plan(side="buy", entry=3900, stop=3890,
                           targets=[3910, 3920, 3930], size=0.03,
                           lot_step=0.01, min_lot=0.01)
    assert plan.policy == "hold_to_final"
    assert plan.final_target == 3930
    assert [leg.label for leg in plan.legs] == ["tp1", "tp3"]
    # TP1 moves the stop to TP1 and closes nothing.
    assert plan.legs[0].action == "stop_move"
    assert plan.legs[0].quantity == 0.0
    assert plan.legs[0].closes_quantity is False
    assert plan.legs[0].stop_after_kind == "target"
    assert plan.legs[0].stop_after_value == 3910
    # TP2 has no leg at all: it is neither an exit nor a stop move.
    assert all(leg.label != "tp2" for leg in plan.legs)
    # The whole position closes at the final target, via the broker's own TP.
    assert plan.legs[1].action == "broker_target"
    assert plan.legs[1].quantity == pytest.approx(0.03)
    assert plan.legs[1].managed is False


def test_the_confirmed_policy_with_two_targets_moves_at_tp1_and_closes_at_tp2():
    plan = build_exit_plan(side="buy", entry=3900, stop=3890,
                           targets=[3910, 3920], size=0.03,
                           lot_step=0.01, min_lot=0.01)
    assert plan.final_target == 3920
    assert [leg.label for leg in plan.legs] == ["tp1", "tp2"]
    assert [leg.action for leg in plan.legs] == ["stop_move", "broker_target"]
    assert plan.legs[0].stop_after_value == 3910


def test_the_confirmed_policy_with_one_target_invents_no_stop_move():
    plan = build_exit_plan(side="buy", entry=3900, stop=3890,
                           targets=[3910], size=0.03,
                           lot_step=0.01, min_lot=0.01)
    assert len(plan.legs) == 1
    assert plan.legs[0].action == "broker_target"
    assert plan.legs[0].stop_after_kind == "none"
    assert plan.legs[0].quantity == pytest.approx(0.03)


def test_the_earlier_ladder_still_closes_thirds_when_asked_for_by_name():
    plan = build_exit_plan(side="buy", entry=3900, stop=3890,
                           targets=[3910, 3920, 3930], size=1.0,
                           lot_step=0.01, min_lot=0.01, policy="ladder")
    assert plan.policy == "three_targets"
    assert [leg.label for leg in plan.legs] == ["tp1", "tp2", "tp3"]
    assert [leg.quantity for leg in plan.legs] == [0.33, 0.33, 0.34]
    assert sum(leg.quantity for leg in plan.legs) == pytest.approx(1.0)
    # TP1 moves the stop to breakeven; TP2 moves it to TP1.
    assert plan.legs[0].stop_after_kind == "breakeven"
    assert plan.legs[1].stop_after_kind == "target"
    assert plan.legs[1].stop_after_value == 3910
    assert plan.legs[2].managed is False
    assert plan.legs[0].managed and plan.legs[1].managed


def test_the_earlier_ladder_still_closes_halves_and_whole_positions():
    halves = build_exit_plan(side="buy", entry=3900, stop=3890,
                             targets=[3910, 3920], size=1.0,
                             lot_step=0.01, min_lot=0.01, policy="ladder")
    assert [leg.quantity for leg in halves.legs] == [0.5, 0.5]
    whole = build_exit_plan(side="buy", entry=3900, stop=3890,
                            targets=[3910], size=1.0,
                            lot_step=0.01, min_lot=0.01, policy="ladder")
    assert whole.policy == "one_target"
    assert whole.legs[0].quantity == 1.0


def test_an_unknown_exit_policy_is_refused():
    with pytest.raises(LadderError, match="unknown exit policy"):
        build_exit_plan(side="buy", entry=3900, stop=3890, targets=[3910],
                        size=0.03, lot_step=0.01, min_lot=0.01,
                        policy="take-profit-somehow")


def test_targets_beyond_the_third_are_ignored_and_recorded():
    plan = build_exit_plan(side="buy", entry=3900, stop=3890,
                           targets=[3910, 3920, 3930, 3940, 3950], size=1.0,
                           lot_step=0.01, min_lot=0.01)
    assert plan.final_target == 3930
    assert plan.ignored_targets == (3940.0, 3950.0)
    assert "ignored" in plan.description()


def test_a_third_below_the_broker_minimum_blocks_the_earlier_ladder():
    # 0.02 / 3 rounds down to 0.00, which is below the broker's 0.01.
    with pytest.raises(LadderError, match="below the broker"):
        build_exit_plan(side="buy", entry=3900, stop=3890,
                        targets=[3910, 3920, 3930], size=0.02,
                        lot_step=0.01, min_lot=0.01, policy="ladder")


def test_a_position_below_the_broker_minimum_cannot_be_closed():
    """The confirmed policy has no partials, but the exit still has to be placeable."""
    with pytest.raises(LadderError, match="below the broker"):
        build_exit_plan(side="buy", entry=3900, stop=3890, targets=[3910],
                        size=0.005, lot_step=0.01, min_lot=0.01)


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


def test_the_approved_destination_is_accepted():
    assert_destination("demo", "https://demo.tradelocker.com",
                       server="BLBRY", account_id=2346225,
                       expected_server="BLBRY", expected_account_id=2346225)


def test_a_wrong_server_is_refused_even_on_the_demo_host():
    with pytest.raises(BrokerRefused, match="approved destination"):
        assert_destination("demo", "https://demo.tradelocker.com",
                           server="PLEXY", account_id=2346225,
                           expected_server="BLBRY", expected_account_id=2346225)


def test_a_wrong_account_is_refused():
    with pytest.raises(BrokerRefused, match="refusing TradeLocker account"):
        assert_destination("demo", "https://demo.tradelocker.com",
                           server="BLBRY", account_id=2509174,
                           expected_server="BLBRY", expected_account_id=2346225)


def test_the_destination_guard_still_refuses_a_live_host():
    with pytest.raises(BrokerRefused, match="not the demo host"):
        assert_destination("demo", "https://live.tradelocker.com",
                           server="BLBRY", account_id=2346225,
                           expected_server="BLBRY", expected_account_id=2346225)


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

    def get_quotes(self, instrument_id):
        return {"bp": 3915.0, "ap": 3915.2}

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


def test_an_absent_position_feed_is_unreadable_not_empty():
    """``None`` is not an empty account.

    Reading it as empty would let a broken feed look like "no position open",
    which is how a live position gets written off as closed.
    """
    with pytest.raises(BrokerError):
        gateway_for().positions()


def test_an_absent_order_feed_is_unreadable_not_empty():
    """Same for orders: "cannot tell" must not read as "no order here"."""
    with pytest.raises(BrokerError):
        gateway_for().orders(history=True)


def test_a_readable_empty_feed_is_empty():
    class _EmptyFeed(_FakeClient):
        def get_all_positions(self):
            class _Frame:
                empty = True
            return _Frame()

        def get_all_orders(self, history=False):
            return self.get_all_positions()

    gateway = gateway_for(_EmptyFeed())
    assert gateway.positions() == []
    assert gateway.orders(history=True) == []


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
                                notifier=RecordingNotifier(), log=lambda line: None,
                                fixed_lots=FIXED_LOTS,
                                max_trade_risk_usd=MAX_RISK_USD)
    restarted.execute(result, envelope())

    assert len(broker.place_calls) == 1, "the order was placed twice"


def _managed_trade(result) -> ManagedTrade:
    """A validated trade built from a pipeline result, as execute() would."""
    signal = result.signal
    plan = build_exit_plan(side=signal["side"], entry=signal["published_entry"],
                           stop=signal["stop_loss"], targets=signal["take_profits"],
                           size=result.order_intent["size_lots"],
                           lot_step=0.01, min_lot=0.01)
    return ManagedTrade(
        trade_id=trade_id_for(result.event_key), event_key=result.event_key,
        channel="@source", message_id=100, revision=0, symbol="XAUUSD",
        side=signal["side"], size=result.order_intent["size_lots"],
        entry=signal["published_entry"], stop=signal["stop_loss"],
        targets=signal["take_profits"], plan=plan,
        current_stop=signal["stop_loss"], state=STATE_VALIDATED,
    )


def test_an_unreadable_order_feed_does_not_cause_a_second_order(tmp_path):
    """The check that prevents resubmission is the order lookup itself.

    If a broker hiccup makes that lookup unreadable and the failure reads as
    "the broker has no order", the retry places a second order for a trade the
    broker already has - doubling the position. Nothing may be sent.
    """
    broker = FakeBroker()
    engine, ledger, _ = engine_for(tmp_path, broker)
    trade = _managed_trade(evaluated())

    engine.submit(trade)
    assert len(broker.place_calls) == 1

    broker.fail_orders = True
    outcome = engine.submit(trade)

    assert len(broker.place_calls) == 1, "an unreadable order feed caused a second order"
    assert outcome.get("retry") is True
    assert "could not be read" in outcome["reason"]


def test_reconciliation_leaves_a_trade_alone_when_the_orders_are_unreadable(
        tmp_path):
    """Reconciliation must not conclude "never submitted" from a failed read.

    That conclusion is what makes a later pass send the order again, so it may
    only be reached when the order feed was actually read.
    """
    broker = FakeBroker()
    engine, ledger, _ = engine_for(tmp_path, broker)
    result = evaluated()
    engine.execute(result, envelope())
    trade_id = trade_id_for(result.event_key)
    before = ledger.get_trade(trade_id)["state"]

    # The position is gone, and the order feed cannot be read. Reconciliation
    # cannot tell whether the order was ever placed.
    broker.position_book = []
    broker.fail_orders = True
    outcome = engine.reconcile_trade(ManagedTrade.from_row(ledger.get_trade(trade_id)))

    assert outcome.get("unreadable") is True
    assert ledger.get_trade(trade_id)["state"] == before, \
        "reconciliation concluded 'never submitted' from a read it could not make"


def test_a_market_that_crossed_the_stop_since_intake_is_not_submitted(tmp_path):
    """The stop side is a property of the market, so it is re-checked late.

    The pipeline validated the stop against the quote it had at intake. This
    engine runs on a second quote, and in that gap the market can cross the
    published stop - sending then fills and stops out immediately.
    """
    broker = FakeBroker(bid=3914.0, ask=3914.2)
    engine, ledger, _ = engine_for(tmp_path, broker)
    msg = envelope("XAUUSD BUY NOW 3915\nSl : 3914.5\nTp : 3920")
    result = evaluate_message(msg, instrument=INSTRUMENT, risk=RISK,
                              quote=MarketQuote(3915.0, 3915.2, NOW))
    # Intake was happy: the stop was below the entry when the signal was priced.
    assert result.status == "paper_intent"

    outcome = engine.execute(result, msg)

    assert outcome.get("executed") is False
    assert outcome.get("guard") is True
    assert "crossed the published stop" in outcome["reason"]
    assert broker.place_calls == []


def test_a_submitted_trade_is_reconciled_rather_than_ignored(tmp_path):
    """A trade interrupted between submission and acknowledgement.

    It used to be skipped by every management pass: the broker may already hold
    the position, so it has to be reconciled rather than managed or ignored.
    """
    broker = FakeBroker()
    engine, ledger, _ = engine_for(tmp_path, broker)
    result = evaluated()
    engine.execute(result, envelope())
    trade_id = trade_id_for(result.event_key)
    engine.confirm_fill(ManagedTrade.from_row(ledger.get_trade(trade_id)))
    row = ledger.get_trade(trade_id)
    row["state"] = STATE_SUBMITTED
    ledger.update_trade(row)

    summary = engine.manage()

    assert summary["considered"] == 1
    assert ManagedTrade.from_row(ledger.get_trade(trade_id)).state == STATE_OPEN


def test_the_deployed_configuration_copies_the_channels_own_call(tmp_path):
    """The numbers actually deployed must admit the channel's real stop width.

    The channel posts ~10-point stops. At 0.02 lots x 100 units/lot that is
    $2.00 a point, so the $25 ceiling admits 12.5 points. At the 0.03 lots an
    earlier configuration used, the same ceiling admitted 8.33 points and
    refused every signal the channel posts. Every other test here sizes at
    0.03/$50, so nothing else would notice this pair drifting apart.
    """
    deployed_risk = RiskConfig(
        equity=4862.73, risk_fraction=0.005, quote_max_age_seconds=2.0,
        fixed_lots=0.02, max_trade_risk_usd=25.0)
    # Verbatim from the channel's held review queue.
    text = "XAUUSD SELL NOW 3877\ufe0f\nSl : 3887\nTp : 3870\nTp : 3865"
    result = evaluate_message(
        envelope(text, mid=18845), instrument=INSTRUMENT, risk=deployed_risk,
        quote=MarketQuote(3876.8, 3877.0, NOW))

    assert result.status == "paper_intent"
    assert result.order_intent["size_lots"] == pytest.approx(0.02)
    assert result.order_intent["risk_budget"] == pytest.approx(20.0, abs=0.5)

    broker = FakeBroker(bid=3876.8, ask=3877.0)
    engine, ledger, _ = engine_for(
        tmp_path, broker, fixed_lots=0.02, max_trade_risk_usd=25.0)
    outcome = engine.execute(result, envelope(text, mid=18845))

    assert outcome.get("executed") is True
    assert len(broker.place_calls) == 1
    assert broker.place_calls[0]["qty"] == pytest.approx(0.02)
    assert broker.place_calls[0]["stop_loss"] == pytest.approx(3887.0)
    assert broker.place_calls[0]["take_profit"] == pytest.approx(3865.0)


def test_the_earlier_lot_size_is_fitted_down_to_the_ceiling(tmp_path):
    """0.03 lots against a $25 ceiling is now sized down, not refused.

    This used to block, and the block was documented as the reason "the copier
    looked dead". Refusing was the wrong repair: $25 buys 0.02 lots over this
    10.2-point stop, which is the size the deployed configuration uses anyway.
    The lot is a ceiling on size, and the ceiling is the risk limit.
    """
    tight = RiskConfig(equity=4862.73, risk_fraction=0.005,
                       quote_max_age_seconds=2.0, fixed_lots=0.03,
                       max_trade_risk_usd=25.0)
    text = "XAUUSD SELL NOW 3877\ufe0f\nSl : 3887\nTp : 3870\nTp : 3865"
    result = evaluate_message(
        envelope(text, mid=18845), instrument=INSTRUMENT, risk=tight,
        quote=MarketQuote(3876.8, 3877.0, NOW))

    assert result.status == "paper_intent"
    assert result.order_intent["size_lots"] == pytest.approx(0.02)
    assert result.order_intent["estimated_stop_risk_before_slippage_and_fees"] \
        == pytest.approx(20.4, abs=0.1)


def test_a_trade_opened_but_never_submitted_is_submitted_after_a_restart(tmp_path):
    """The window between open_trade and place_market must not be a dead end.

    ``execute`` writes the trade row before it sends the order. If the process
    dies in between, the row is left in ``validated`` - and the management pass
    used to walk only accepted and open trades, so the signal was recorded and
    then never acted on, with nothing in the ledger saying so.
    """
    broker = FakeBroker()
    engine, ledger, _ = engine_for(tmp_path, broker)
    result = evaluated()
    trade_id = trade_id_for(result.event_key)
    signal = result.signal

    plan = build_exit_plan(side=signal["side"], entry=signal["published_entry"],
                           stop=signal["stop_loss"], targets=signal["take_profits"],
                           size=result.order_intent["size_lots"],
                           lot_step=0.01, min_lot=0.01)
    row = ManagedTrade(
        trade_id=trade_id, event_key=result.event_key, channel="@source",
        message_id=100, revision=0, symbol="XAUUSD", side=signal["side"],
        size=result.order_intent["size_lots"], entry=signal["published_entry"],
        stop=signal["stop_loss"], targets=signal["take_profits"], plan=plan,
        current_stop=signal["stop_loss"], state=STATE_VALIDATED,
    ).to_row()
    assert ledger.open_trade(row) == "inserted"

    # A restart: reconcile, then one management pass.
    restarted = ExecutionEngine(broker=broker, ledger=ledger,
                                notifier=RecordingNotifier(), log=lambda line: None,
                                fixed_lots=FIXED_LOTS,
                                max_trade_risk_usd=MAX_RISK_USD)
    restarted.reconcile()
    restarted.manage()

    assert len(broker.place_calls) == 1, "the unsubmitted trade was never sent"
    after = ManagedTrade.from_row(ledger.get_trade(trade_id))
    assert after.state != STATE_VALIDATED


def test_the_management_pass_does_not_submit_the_same_trade_twice(tmp_path):
    """Two threads reaching one unsubmitted trade must still place one order."""
    import threading

    broker = FakeBroker()
    engine, ledger, _ = engine_for(tmp_path, broker)
    result = evaluated()
    trade_id = trade_id_for(result.event_key)
    signal = result.signal
    plan = build_exit_plan(side=signal["side"], entry=signal["published_entry"],
                           stop=signal["stop_loss"], targets=signal["take_profits"],
                           size=result.order_intent["size_lots"],
                           lot_step=0.01, min_lot=0.01)
    ledger.open_trade(ManagedTrade(
        trade_id=trade_id, event_key=result.event_key, channel="@source",
        message_id=100, revision=0, symbol="XAUUSD", side=signal["side"],
        size=result.order_intent["size_lots"], entry=signal["published_entry"],
        stop=signal["stop_loss"], targets=signal["take_profits"], plan=plan,
        current_stop=signal["stop_loss"], state=STATE_VALIDATED,
    ).to_row())

    outcomes = []
    barrier = threading.Barrier(2)

    def run() -> None:
        barrier.wait()
        outcomes.append(engine.manage())

    threads = [threading.Thread(target=run) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(broker.place_calls) == 1, "concurrent passes placed two orders"


def test_tp1_moves_the_stop_to_tp1_and_closes_nothing(tmp_path):
    """The confirmed policy: passing TP1 removes risk, it does not bank profit."""
    broker = FakeBroker()
    engine, ledger, _ = engine_for(tmp_path, broker)
    result = evaluated()
    engine.execute(result, envelope())
    trade_id = trade_id_for(result.event_key)
    engine.confirm_fill(ManagedTrade.from_row(ledger.get_trade(trade_id)))
    assert broker.position_book[0].qty == pytest.approx(FIXED_LOTS)

    # Price reaches TP1 (3910) for a short: the ask falls to 3910.
    broker.bid, broker.ask = 3909.5, 3909.7
    outcome = engine.manage_trade(ManagedTrade.from_row(ledger.get_trade(trade_id)))

    assert outcome["stop_moved"] is True
    assert outcome["closed_qty"] == 0.0
    assert broker.close_calls == [], "nothing may be closed at TP1"
    assert broker.position_book[0].qty == pytest.approx(FIXED_LOTS), "the position is held"
    # The stop moves to TP1 itself.
    assert broker.modify_calls[0]["stop_loss"] == pytest.approx(3910.0)
    trade = ManagedTrade.from_row(ledger.get_trade(trade_id))
    assert trade.state == STATE_OPEN
    assert trade.legs_done == 1
    assert trade.current_stop == pytest.approx(3910.0)


def test_tp2_is_not_an_exit_and_moves_nothing(tmp_path):
    broker = FakeBroker()
    engine, ledger, _ = engine_for(tmp_path, broker)
    result = evaluated()
    engine.execute(result, envelope())
    trade_id = trade_id_for(result.event_key)
    engine.confirm_fill(ManagedTrade.from_row(ledger.get_trade(trade_id)))

    broker.bid, broker.ask = 3909.5, 3909.7
    engine.manage_trade(ManagedTrade.from_row(ledger.get_trade(trade_id)))

    # Price reaches TP2 (3905).
    broker.bid, broker.ask = 3904.8, 3905.0
    outcome = engine.manage_trade(ManagedTrade.from_row(ledger.get_trade(trade_id)))

    assert outcome.get("waiting") is True
    assert outcome["next_target"] == pytest.approx(3900.0)
    assert broker.close_calls == []
    assert len(broker.modify_calls) == 1, "the stop moves once, at TP1"
    trade = ManagedTrade.from_row(ledger.get_trade(trade_id))
    assert trade.legs_done == 1
    assert trade.current_stop == pytest.approx(3910.0)


def test_the_final_target_closes_the_remainder(tmp_path):
    broker = FakeBroker()
    engine, ledger, _ = engine_for(tmp_path, broker)
    result = evaluated()
    engine.execute(result, envelope())
    engine.confirm_fill(ManagedTrade.from_row(
        ledger.get_trade(trade_id_for(result.event_key))))

    for bid, ask in ((3909.5, 3909.7), (3904.8, 3905.0)):
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


def test_a_close_whose_confirming_read_failed_is_not_closed_again(tmp_path):
    """A close that worked but could not be confirmed must not be repeated.

    The first pass closes and then cannot read the position, so it reports an
    error without advancing the ladder. Retrying the same leg would close the
    same step a second time; the broker's quantity is the truth about what has
    closed, so the ledger is brought forward to match it.
    """
    class FlakyPositions(FakeBroker):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            self.fail_next_position_read = False

        def positions(self):
            if self.fail_next_position_read:
                self.fail_next_position_read = False
                raise BrokerError("429 on /positions")
            return super().positions()

        def close_quantity(self, *, position_id, qty):
            result = super().close_quantity(position_id=position_id, qty=qty)
            self.fail_next_position_read = True
            return result

    broker = FlakyPositions()
    engine, ledger, _ = engine_for(tmp_path, broker, exit_policy="ladder")
    result = evaluated()
    engine.execute(result, envelope())
    trade_id = trade_id_for(result.event_key)
    engine.confirm_fill(ManagedTrade.from_row(ledger.get_trade(trade_id)))
    broker.bid, broker.ask = 3909.5, 3909.7

    first = engine.manage_trade(ManagedTrade.from_row(ledger.get_trade(trade_id)))
    assert first.get("error") is True, "an unconfirmed close is not a completed one"
    assert len(broker.close_calls) == 1

    engine.manage_trade(ManagedTrade.from_row(ledger.get_trade(trade_id)))

    assert len(broker.close_calls) == 1, "the same ladder step was closed twice"
    row = ledger.get_trade(trade_id)
    assert row["legs_done"] >= 1, "the close that did happen was never recorded"


def _drop_attached_target(broker):
    """Make the position carry no take profit, as a reduction can."""
    from dataclasses import replace

    position = broker.position_book[0]
    broker.open_book = [o for o in broker.open_book
                        if o.id != position.take_profit_id]
    broker.position_book[0] = replace(position, take_profit_id=None)


def test_a_restored_final_target_is_read_back(tmp_path):
    """The target path verifies the broker, as the stop path does."""
    broker = FakeBroker()
    engine, ledger, _ = engine_for(tmp_path, broker)
    result = evaluated()
    engine.execute(result, envelope())
    trade_id = trade_id_for(result.event_key)
    engine.confirm_fill(ManagedTrade.from_row(ledger.get_trade(trade_id)))
    _drop_attached_target(broker)
    broker.bid, broker.ask = 3909.5, 3909.7   # past the first target

    outcome = engine.manage_trade(ManagedTrade.from_row(ledger.get_trade(trade_id)))

    assert outcome.get("stop_moved") is True
    assert outcome.get("target_restored") is True, \
        "the dropped target was not reported as restored"
    # And it is really attached, at the final target.
    position = broker.positions()[0]
    levels = {order.id: order for order in broker.orders(history=False)}
    target_order = levels.get(position.take_profit_id)
    assert target_order is not None, "the position points at no target order"
    assert target_order.price == pytest.approx(3900.0)


def test_a_final_target_the_broker_does_not_carry_stops_management(tmp_path):
    """``modify_position`` returning True is not proof the level is attached.

    The position would be unprotected on the upside, so a human has to look.
    """
    class SilentTargetRefuser(FakeBroker):
        def modify_position(self, *, position_id, stop_loss=None, take_profit=None):
            if take_profit is not None:
                # Accept the request and attach nothing.
                self.modify_calls.append({"position_id": position_id,
                                          "stop_loss": stop_loss,
                                          "take_profit": take_profit})
                return True
            return super().modify_position(position_id=position_id,
                                           stop_loss=stop_loss)

    broker = SilentTargetRefuser()
    engine, ledger, _ = engine_for(tmp_path, broker)
    result = evaluated()
    engine.execute(result, envelope())
    trade_id = trade_id_for(result.event_key)
    engine.confirm_fill(ManagedTrade.from_row(ledger.get_trade(trade_id)))
    _drop_attached_target(broker)
    broker.bid, broker.ask = 3909.5, 3909.7   # past the first target

    outcome = engine.manage_trade(ManagedTrade.from_row(ledger.get_trade(trade_id)))

    assert outcome.get("stopped") is True
    assert ManagedTrade.from_row(ledger.get_trade(trade_id)).state == STATE_REVIEW


def test_a_trade_in_review_with_a_live_position_still_occupies_the_allowance(tmp_path):
    """A review trade holding a position must count against the position limit.

    Excluding it let a limit of one be exceeded while a trade was waiting on a
    human: two live positions at the broker, two orders placed.
    """
    broker = FakeBroker()
    engine, ledger, _ = engine_for(tmp_path, broker)
    first = evaluated(mid=100)
    engine.execute(first, envelope(mid=100))
    first_id = trade_id_for(first.event_key)
    engine.confirm_fill(ManagedTrade.from_row(ledger.get_trade(first_id)))

    # Sent to review, position still open at the broker.
    row = ledger.get_trade(first_id)
    row["state"] = STATE_REVIEW
    row["management_stopped"] = True
    ledger.update_trade(row)

    second_text = "XAUUSD BUY NOW 3915\nSl : 3905\nTp : 3920"
    second = evaluated(second_text, mid=101)
    outcome = engine.execute(second, envelope(second_text, mid=101))

    assert outcome.get("executed") is False
    assert outcome.get("guard") is True
    assert len(broker.place_calls) == 1, \
        "a second position was opened while one was waiting on a human"


def test_an_accepted_order_that_never_fills_is_expired_by_the_deployed_loop(tmp_path):
    """A stuck fill must not block the copier forever.

    ``manage`` used to count the pending fill and ``continue``, so the
    fill-window check - which lived only in ``manage_trade`` - was unreachable
    for exactly the state it exists for. The trade stayed ``accepted`` and, being
    in ``ACTIVE_STATES``, counted as an open position, blocking every later
    signal under a limit of one.
    """
    broker = FakeBroker()
    broker.withhold_fill = True          # accepted, but no position ever appears
    engine, ledger, _ = engine_for(tmp_path, broker, fill_timeout_s=0.0)
    result = evaluated()
    engine.execute(result, envelope())
    trade_id = trade_id_for(result.event_key)
    assert ManagedTrade.from_row(ledger.get_trade(trade_id)).state == STATE_ACCEPTED

    summary = engine.manage()

    assert summary["considered"] == 1
    assert summary["stopped"] == 1, "the stuck fill was never expired"
    assert ManagedTrade.from_row(ledger.get_trade(trade_id)).state == STATE_REVIEW
    # And it no longer occupies the one-position allowance.
    assert ledger.list_trades(states=ACTIVE_STATES) == []


def test_the_earlier_ladder_still_partially_closes_through_the_engine(tmp_path):
    """The earlier policy is still executable end to end, not merely buildable."""
    broker = FakeBroker()
    engine, ledger, _ = engine_for(tmp_path, broker, exit_policy="ladder")
    result = evaluated()
    engine.execute(result, envelope())
    trade_id = trade_id_for(result.event_key)
    engine.confirm_fill(ManagedTrade.from_row(ledger.get_trade(trade_id)))

    broker.bid, broker.ask = 3909.5, 3909.7
    outcome = engine.manage_trade(ManagedTrade.from_row(ledger.get_trade(trade_id)))

    assert outcome["partial"] is True
    assert broker.close_calls[0]["qty"] == pytest.approx(0.01)
    # Breakeven is the real fill price, not the published entry.
    assert broker.modify_calls[0]["stop_loss"] == pytest.approx(3915.0)
    trade = ManagedTrade.from_row(ledger.get_trade(trade_id))
    assert trade.state == STATE_PARTIAL
    assert trade.legs_done == 1


def test_a_position_that_no_longer_matches_the_plan_stops_management(tmp_path):
    broker = FakeBroker()
    engine, ledger, _ = engine_for(tmp_path, broker)
    result = evaluated()
    engine.execute(result, envelope())
    trade_id = trade_id_for(result.event_key)
    engine.confirm_fill(ManagedTrade.from_row(ledger.get_trade(trade_id)))

    # Something outside this engine reduced the position. TP1 still passes and
    # moves the stop, but the final close can no longer cover the plan.
    broker._replace(broker.position_book[0], qty=0.001)
    broker.bid, broker.ask = 3909.5, 3909.7
    engine.manage_trade(ManagedTrade.from_row(ledger.get_trade(trade_id)))

    broker.bid, broker.ask = 3899.8, 3900.0
    outcome = engine.manage_trade(ManagedTrade.from_row(ledger.get_trade(trade_id)))

    assert outcome["stopped"] is True
    assert "expects to close" in outcome["reason"]
    assert broker.close_calls == [], "nothing is closed on a plan that does not match"


def test_a_stop_that_would_widen_the_risk_stops_management(tmp_path):
    broker = FakeBroker()
    engine, ledger, _ = engine_for(tmp_path, broker)
    result = evaluated()
    engine.execute(result, envelope())
    trade_id = trade_id_for(result.event_key)
    engine.confirm_fill(ManagedTrade.from_row(ledger.get_trade(trade_id)))

    # Pretend the stop already sits better than TP1 would put it, so the move
    # would widen the risk rather than reduce it.
    trade = ManagedTrade.from_row(ledger.get_trade(trade_id))
    trade.current_stop = 3905.0
    broker.bid, broker.ask = 3909.5, 3909.7

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


def test_a_failed_position_read_does_not_close_an_open_trade(tmp_path):
    """A 429 on /positions must not be read as "the position is gone".

    The demo API does return 429 under load. Treating that as a close marks a
    live position finished, sends the operator a notification saying so, and
    abandons the ladder - while the position is still open at the broker with
    its stop and target attached.
    """
    broker = FakeBroker()
    engine, ledger, _ = engine_for(tmp_path, broker)
    result = evaluated()
    engine.execute(result, envelope())
    trade = ManagedTrade.from_row(ledger.get_trade(trade_id_for(result.event_key)))
    engine.confirm_fill(trade)
    trade = ManagedTrade.from_row(ledger.get_trade(trade.trade_id))
    assert trade.state == STATE_OPEN

    broker.fail_positions = True
    outcome = engine.manage_trade(trade)
    assert outcome.get("error") is True
    assert not outcome.get("closed")

    trade = ManagedTrade.from_row(ledger.get_trade(trade.trade_id))
    assert trade.state == STATE_OPEN, "a failed read closed a live trade"
    assert not any("closed" in str(kind) for kind in engine.notifier.sent), \
        "a failed read told the operator the position had closed"

    # And the pass counts it as an error, not as a close.
    summary = engine.manage()
    assert summary["closed"] == 0
    assert summary["errors"] == 1


def test_a_position_left_behind_by_a_wrong_close_is_reported(tmp_path):
    """The divergence a false close leaves: the ledger says finished, the
    broker still holds the position, and reconciliation cannot see it because
    it only walks active trades."""
    broker = FakeBroker()
    engine, ledger, _ = engine_for(tmp_path, broker)
    result = evaluated()
    engine.execute(result, envelope())
    trade = ManagedTrade.from_row(ledger.get_trade(trade_id_for(result.event_key)))
    engine.confirm_fill(trade)

    # Write the trade off as closed while the position is still there.
    trade = ManagedTrade.from_row(ledger.get_trade(trade.trade_id))
    trade.state = STATE_CLOSED
    engine._save(trade)

    summary = engine.reconcile()
    assert summary["orphaned"] == 1
    orphans = engine.orphaned_positions()
    assert orphans[0]["trade_id"] == trade.trade_id
    assert orphans[0]["state"] == STATE_CLOSED


def test_reconciliation_does_not_write_off_a_trade_it_cannot_read(tmp_path):
    broker = FakeBroker()
    engine, ledger, _ = engine_for(tmp_path, broker)
    result = evaluated()
    engine.execute(result, envelope())
    engine.confirm_fill(ManagedTrade.from_row(
        ledger.get_trade(trade_id_for(result.event_key))))

    broker.fail_positions = True
    summary = engine.reconcile()
    assert summary.get("closed", 0) == 0
    trade = ManagedTrade.from_row(ledger.get_trade(trade_id_for(result.event_key)))
    assert trade.state == STATE_OPEN


def test_a_failed_position_read_does_not_block_a_confirmed_fill(tmp_path):
    """The order feed is separate evidence, so a fill is still recorded."""
    broker = FakeBroker()
    engine, ledger, _ = engine_for(tmp_path, broker)
    result = evaluated()
    engine.execute(result, envelope())

    broker.fail_positions = True
    outcome = engine.confirm_fill(ManagedTrade.from_row(
        ledger.get_trade(trade_id_for(result.event_key))))
    assert outcome["filled"] is True
    trade = ManagedTrade.from_row(ledger.get_trade(trade_id_for(result.event_key)))
    assert trade.state == STATE_OPEN


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
    trade_id = trade_id_for(result.event_key)
    engine.confirm_fill(ManagedTrade.from_row(ledger.get_trade(trade_id)))

    broker.bid, broker.ask = 3909.5, 3909.7
    engine.manage_trade(ManagedTrade.from_row(ledger.get_trade(trade_id)))

    kinds = [text.split("]")[0].strip("[") for text in notifier.sent]
    assert "SIGNAL" in kinds
    assert "ACCEPTED" in kinds
    assert "FILLED" in kinds
    assert "TP1 PASSED" in kinds
    # One event, one message. The stop move is described by the TP1 message
    # rather than announced a second time under a different heading.
    assert kinds.count("TP1 PASSED") == 1
    assert "STOP MOVED" not in kinds

    # TP2 is not an exit, so passing it is not a lifecycle event.
    broker.bid, broker.ask = 3904.8, 3905.0
    engine.manage_trade(ManagedTrade.from_row(ledger.get_trade(trade_id)))
    kinds = [text.split("]")[0].strip("[") for text in notifier.sent]
    assert "TP2 HIT" not in kinds
    assert kinds.count("TP1 PASSED") == 1

    # The final target closes the position, and that is its own event.
    broker.bid, broker.ask = 3899.8, 3900.0
    engine.manage_trade(ManagedTrade.from_row(ledger.get_trade(trade_id)))
    kinds = [text.split("]")[0].strip("[") for text in notifier.sent]
    assert "CLOSED" in kinds


def test_the_correlation_tag_is_stable_and_bounded():
    key = "a" * 64
    assert trade_id_for(key) == trade_id_for(key)
    assert len(trade_id_for(key)) <= 31
    assert trade_id_for("short") != trade_id_for("short2")


def test_a_refused_stop_move_is_retried_before_the_trade_advances(tmp_path):
    """A stop that could not be applied must not be skipped.

    The broker can refuse a modification when the price has moved between the
    check and the request. Advancing past that point would lose the move for
    good, so the leg is not marked done and the next pass tries again.
    """
    broker = FakeBroker()
    engine, ledger, _ = engine_for(tmp_path, broker)
    result = evaluated()
    engine.execute(result, envelope())
    trade_id = trade_id_for(result.event_key)
    engine.confirm_fill(ManagedTrade.from_row(ledger.get_trade(trade_id)))

    broker.fail_modify = True
    broker.bid, broker.ask = 3909.5, 3909.7
    first = engine.manage_trade(ManagedTrade.from_row(ledger.get_trade(trade_id)))

    assert first.get("retry") is True
    trade = ManagedTrade.from_row(ledger.get_trade(trade_id))
    assert trade.legs_done == 0, "the step did not complete, so it is not done"
    assert trade.current_stop == pytest.approx(3925.0), "the stop did not move"
    assert broker.close_calls == [], "nothing was closed"
    assert broker.position_book[0].qty == pytest.approx(FIXED_LOTS)

    # The broker recovers. The next pass applies the stop, and does not jump
    # ahead to the final target.
    broker.fail_modify = False
    second = engine.manage_trade(ManagedTrade.from_row(ledger.get_trade(trade_id)))
    assert second.get("stop_moved") is True
    trade = ManagedTrade.from_row(ledger.get_trade(trade_id))
    assert trade.current_stop == pytest.approx(3910.0)
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

    broker.bid, broker.ask = 3909.5, 3909.7
    engine.manage_trade(ManagedTrade.from_row(ledger.get_trade(trade_id)))

    position = broker.position_book[0]
    levels = engine._attached_levels(position)
    assert levels.known is True
    assert levels.stop == pytest.approx(3910.0), "the live stop moved to TP1"
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
    broker.bid, broker.ask = 3909.5, 3909.7
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


def test_a_fill_beyond_tp1_is_accepted_and_the_stop_moves_at_once(tmp_path):
    """Adverse slippage past TP1 is harmless now that the position is held.

    Under the confirmed policy TP1 is not an exit, so a fill beyond it is a
    better entry rather than an invalid plan. The stop move fires on the first
    observation instead of waiting for a level that has already gone by.
    """
    broker = FakeBroker(bid=3915.0, ask=3915.2)
    engine, ledger, _ = engine_for(tmp_path, broker)
    # The fill moves 3.6 price units. The production slippage limit is 2.0, so
    # this test — which is about the stop move, not slippage — sets its own.
    engine.limits = RiskLimits(max_slippage=10.0)
    # A SELL whose TP1 sits close to the entry, so an adverse fill can pass it
    # while still being inside the slippage limit.
    text = "XAUUSD SELL NOW 3915\nSl : 3925\nTp : 3912\nTp : 3905\nTp : 3900"
    result = evaluated(text)
    # The broker fills the short at 3911.4, already past TP1 (3912).
    broker.bid, broker.ask = 3911.4, 3911.6
    engine.execute(result, envelope(text))
    trade_id = trade_id_for(result.event_key)

    outcome = engine.confirm_fill(ManagedTrade.from_row(ledger.get_trade(trade_id)))
    assert outcome.get("filled") is True

    # The first management pass moves the stop to TP1 and closes nothing.
    broker.bid, broker.ask = 3911.4, 3911.6
    moved = engine.manage_trade(ManagedTrade.from_row(ledger.get_trade(trade_id)))

    assert moved.get("stop_moved") is True
    assert broker.close_calls == []
    trade = ManagedTrade.from_row(ledger.get_trade(trade_id))
    assert trade.current_stop == pytest.approx(3912.0)


def test_a_fill_beyond_the_final_target_stops_before_any_leg(tmp_path):
    """A fill already at the target the trade exists to reach leaves nothing to do."""
    broker = FakeBroker(bid=3915.0, ask=3915.2)
    engine, ledger, _ = engine_for(tmp_path, broker)
    # The fill moves 4.2 price units; this test is about the exhausted ladder,
    # not about slippage, so it sets its own limit.
    engine.limits = RiskLimits(max_slippage=10.0)
    # Two targets, the final one close to the entry, so a fill can pass it
    # while staying inside the slippage limit.
    text = "XAUUSD SELL NOW 3915\nSl : 3925\nTp : 3913\nTp : 3911"
    result = evaluated(text)
    broker.bid, broker.ask = 3910.8, 3911.0
    engine.execute(result, envelope(text))
    trade_id = trade_id_for(result.event_key)

    outcome = engine.confirm_fill(ManagedTrade.from_row(ledger.get_trade(trade_id)))

    assert outcome.get("stopped") is True
    assert "beyond the final target" in outcome["reason"]
    trade = ManagedTrade.from_row(ledger.get_trade(trade_id))
    assert trade.state == STATE_REVIEW
    assert trade.legs_done == 0
    assert broker.close_calls == [], "no leg may run on an exhausted trade"
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
    broker.bid, broker.ask = 3909.5, 3909.7
    outcome = engine.manage_trade(ManagedTrade.from_row(ledger.get_trade(trade_id)))
    for _ in range(engine.max_stop_attempts + 2):
        if outcome.get("stopped"):
            break
        outcome = engine.manage_trade(ManagedTrade.from_row(ledger.get_trade(trade_id)))

    assert outcome.get("stopped") is True
    trade = ManagedTrade.from_row(ledger.get_trade(trade_id))
    assert trade.state == STATE_REVIEW
    assert "still protected" in trade.note


# ---------------------------------------------------------------------------
# the confirmed policy across a restart
# ---------------------------------------------------------------------------


def test_a_stop_move_leg_survives_a_restart(tmp_path):
    """The leg's action is what stops it being read as a full close after a restart."""
    broker = FakeBroker()
    engine, ledger, _ = engine_for(tmp_path, broker)
    result = evaluated()
    engine.execute(result, envelope())
    trade_id = trade_id_for(result.event_key)
    engine.confirm_fill(ManagedTrade.from_row(ledger.get_trade(trade_id)))

    reloaded = ManagedTrade.from_row(ledger.get_trade(trade_id))
    assert reloaded.plan.legs[0].action == "stop_move"
    assert reloaded.plan.legs[0].quantity == 0.0
    assert reloaded.plan.legs[0].closes_quantity is False
    assert reloaded.plan.legs[-1].action == "broker_target"

    # A restarted engine picks the same plan up and moves the stop, not a close.
    restarted = ExecutionEngine(broker=broker, ledger=ledger,
                                notifier=RecordingNotifier(), log=lambda line: None,
                                fixed_lots=FIXED_LOTS,
                                max_trade_risk_usd=MAX_RISK_USD)
    broker.bid, broker.ask = 3909.5, 3909.7
    outcome = restarted.manage_trade(ManagedTrade.from_row(ledger.get_trade(trade_id)))

    assert outcome["stop_moved"] is True
    assert broker.close_calls == []


def test_a_trade_resumes_after_a_restart_and_closes_at_the_final_target(tmp_path):
    broker = FakeBroker()
    engine, ledger, _ = engine_for(tmp_path, broker)
    result = evaluated()
    engine.execute(result, envelope())
    trade_id = trade_id_for(result.event_key)
    engine.confirm_fill(ManagedTrade.from_row(ledger.get_trade(trade_id)))

    broker.bid, broker.ask = 3909.5, 3909.7
    engine.manage_trade(ManagedTrade.from_row(ledger.get_trade(trade_id)))

    # Everything is thrown away except the ledger and the broker.
    restarted = ExecutionEngine(broker=broker, ledger=ledger,
                                notifier=RecordingNotifier(), log=lambda line: None,
                                fixed_lots=FIXED_LOTS,
                                max_trade_risk_usd=MAX_RISK_USD)
    broker.bid, broker.ask = 3899.8, 3900.0
    outcome = restarted.manage_trade(ManagedTrade.from_row(ledger.get_trade(trade_id)))

    assert outcome["closed"] is True
    assert broker.position_book == []
    trade = ManagedTrade.from_row(ledger.get_trade(trade_id))
    assert trade.state == STATE_CLOSED
    assert trade.current_stop == pytest.approx(3910.0)


def test_the_plan_step_is_preserved_from_the_plan_not_the_trade_row(tmp_path):
    """The lot step used to be read from the wrong place and always came back 0.01."""
    broker = FakeBroker()
    engine, ledger, _ = engine_for(tmp_path, broker)
    result = evaluated()
    engine.execute(result, envelope())
    trade_id = trade_id_for(result.event_key)
    engine.confirm_fill(ManagedTrade.from_row(ledger.get_trade(trade_id)))

    reloaded = ManagedTrade.from_row(ledger.get_trade(trade_id))
    assert reloaded.plan.lot_step == pytest.approx(broker.spec().lot_step)
    assert reloaded.plan.min_lot == pytest.approx(broker.spec().min_lot)


def test_manage_counts_a_stop_move_separately_from_a_close(tmp_path):
    broker = FakeBroker()
    engine, ledger, _ = engine_for(tmp_path, broker)
    result = evaluated()
    engine.execute(result, envelope())
    engine.confirm_fill(ManagedTrade.from_row(
        ledger.get_trade(trade_id_for(result.event_key))))

    broker.bid, broker.ask = 3909.5, 3909.7
    summary = engine.manage()

    assert summary["stops_moved"] == 1
    assert summary["partial"] == 0, "nothing was partially closed"
    assert summary["closed"] == 0


# ---------------------------------------------------------------------------
# the engine's own size check
# ---------------------------------------------------------------------------
#
# The pipeline sizes the intent; the engine is what sends the order. These
# tests give the two components *different* configuration on purpose, because
# that is the only way to show the engine is not simply trusting the intent.
# A matching configuration would pass whether or not the engine checked.


def evaluated_fixed(text=THREE_TARGETS, *, lots=0.03, ceiling=50.0, mid=100,
                    quote=(3915.0, 3915.0)):
    """A signal sized by the pipeline in fixed-lot mode."""
    return evaluate_message(
        envelope(text, mid=mid),
        instrument=INSTRUMENT,
        risk=RiskConfig(10_000, 0.005, 2.0, fixed_lots=lots,
                        max_trade_risk_usd=ceiling),
        quote=MarketQuote(quote[0], quote[1], NOW),
    )


def test_the_engine_sends_the_configured_lot_when_it_is_inside_the_ceiling(tmp_path):
    """The happy path, so the refusals below mean something."""
    engine, _ledger, broker = engine_for(tmp_path, fixed_lots=0.03,
                                         max_trade_risk_usd=50.0)
    outcome = engine.execute(evaluated_fixed(), envelope())

    assert outcome["executed"] is True
    assert broker.place_calls[0]["qty"] == pytest.approx(0.03)


def test_the_engine_refuses_a_size_above_the_configured_maximum(tmp_path):
    """The pipeline sized 0.05; the engine's ceiling is 0.03 and refuses.

    A size *below* the configured lot is now normal - that is the pipeline
    fitting the order to the risk budget. A size *above* it is still a bug, and
    the engine is the last line that catches it.
    """
    engine, ledger, broker = engine_for(tmp_path, fixed_lots=0.03,
                                        max_trade_risk_usd=60.0)
    result = evaluated_fixed(lots=0.05, ceiling=60.0)
    outcome = engine.execute(result, envelope())

    assert outcome["executed"] is False
    assert outcome["state"] == STATE_BLOCKED
    assert broker.place_calls == [], "no order may be sent at the wrong size"
    assert "larger than the configured maximum" in outcome["reason"]
    trade = ManagedTrade.from_row(ledger.get_trade(trade_id_for(result.event_key)))
    assert trade.state == STATE_BLOCKED


def test_the_engine_refuses_an_order_over_the_dollar_ceiling(tmp_path):
    """The pipeline allowed $30 against a $50 ceiling; the engine's is $25."""
    engine, _ledger, broker = engine_for(tmp_path, fixed_lots=0.03,
                                         max_trade_risk_usd=25.0)
    outcome = engine.execute(evaluated_fixed(ceiling=50.0), envelope())

    assert outcome["executed"] is False
    assert broker.place_calls == []
    assert "exceeds the" in outcome["reason"]
    assert "$30.00" in outcome["reason"]


def test_the_engine_refuses_a_size_the_broker_cannot_trade(tmp_path):
    """Risk-fraction mode still has to obey the broker's lot rules."""

    class TinyLotBroker(FakeBroker):
        def spec(self) -> BrokerSpec:
            base = super().spec()
            return BrokerSpec(**{**base.__dict__, "min_lot": 1.0})

    engine, _ledger, broker = engine_for(tmp_path, TinyLotBroker())
    outcome = engine.execute(evaluated(), envelope())

    assert outcome["executed"] is False
    assert broker.place_calls == []
    assert "below the broker's minimum" in outcome["reason"]


def test_the_engine_refuses_a_non_finite_contract_size(tmp_path):
    """The NaN bypass, at the layer that actually sends the order."""

    class NanBroker(FakeBroker):
        def spec(self) -> BrokerSpec:
            base = super().spec()
            return BrokerSpec(**{**base.__dict__,
                                 "contract_units_per_lot": float("nan")})

    engine, _ledger, broker = engine_for(tmp_path, NanBroker())
    outcome = engine.execute(evaluated(), envelope())

    assert outcome["executed"] is False
    assert broker.place_calls == []
    assert "not a set of finite numbers" in outcome["reason"]


def test_the_engine_refuses_a_zero_size(tmp_path):
    """A resumed or corrupted intent with no size must not reach the broker."""
    engine, _ledger, broker = engine_for(tmp_path)
    result = evaluated()
    result.order_intent["size_lots"] = 0.0
    outcome = engine.execute(result, envelope())

    assert outcome["executed"] is False
    assert broker.place_calls == []
    # Refused before submission; the reason names the size it was given.
    assert "0.0" in outcome["reason"]


def test_the_engine_applies_the_ceiling_in_risk_fraction_mode_too(tmp_path):
    """No fixed lot configured, but a ceiling is: it still binds."""
    engine, _ledger, broker = engine_for(tmp_path, max_trade_risk_usd=25.0)
    outcome = engine.execute(evaluated(), envelope())

    assert outcome["executed"] is False
    assert broker.place_calls == []
    assert "exceeds the" in outcome["reason"]


# ---------------------------------------------------------------------------
# the engine cannot be built without a size rule
# ---------------------------------------------------------------------------


def _build(tmp_path, **kwargs):
    ledger = SQLiteIntentLedger(tmp_path / "exec.sqlite")
    kwargs.setdefault("fixed_lots", FIXED_LOTS)
    kwargs.setdefault("max_trade_risk_usd", MAX_RISK_USD)
    return ExecutionEngine(broker=FakeBroker(), ledger=ledger,
                           notifier=RecordingNotifier(), log=lambda line: None,
                           **kwargs)


def test_the_engine_refuses_to_exist_without_a_fixed_lot(tmp_path):
    """``None`` used to mean "size from equity" - the rule's opposite."""
    with pytest.raises(ValueError, match="fixed_lots is required"):
        _build(tmp_path, fixed_lots=None)


def test_the_engine_refuses_to_exist_without_a_ceiling(tmp_path):
    with pytest.raises(ValueError, match="max_trade_risk_usd is required"):
        _build(tmp_path, max_trade_risk_usd=None)


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf"), 0.0, -0.03])
def test_the_engine_refuses_a_non_finite_or_non_positive_lot(tmp_path, bad):
    with pytest.raises(ValueError, match="finite positive"):
        _build(tmp_path, fixed_lots=bad)


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), 0.0])
def test_the_engine_refuses_a_non_finite_or_non_positive_ceiling(tmp_path, bad):
    with pytest.raises(ValueError, match="finite positive"):
        _build(tmp_path, max_trade_risk_usd=bad)


def test_the_engine_refuses_a_size_that_is_not_a_number(tmp_path):
    with pytest.raises(ValueError, match="is not a number"):
        _build(tmp_path, fixed_lots="three")


# ---------------------------------------------------------------------------
# a fitted size is normal; only a size above the maximum is a bug
# ---------------------------------------------------------------------------


def test_the_engine_accepts_a_size_fitted_below_the_maximum(tmp_path):
    """0.01 lots over a 16-point stop is $16 of a $30 budget.

    The pipeline fitted the order down because 0.02 lots would have risked $32.
    The engine must send the fitted size, not reject it for not being exactly
    the configured lot.
    """
    text = "XAUUSD SELL NOW 3870\nSl : 3886\nTp : 3860\nTp : 3855"
    result = evaluated_fixed(text, lots=0.02, ceiling=30.0,
                             quote=(3870.0, 3870.2))
    assert result.order_intent["size_lots"] == 0.01

    broker = FakeBroker(bid=3870.0, ask=3870.2)
    engine, _ledger, _ = engine_for(tmp_path, broker, fixed_lots=0.02,
                                    max_trade_risk_usd=30.0)
    outcome = engine.execute(result, envelope(text))

    assert outcome["executed"] is True
    assert broker.place_calls[0]["qty"] == pytest.approx(0.01)


def test_the_engine_refuses_when_the_market_moved_after_the_size_was_fitted(tmp_path):
    """The size fitted a 12-point stop; by submission the stop is 16 away.

    0.02 lots then risks $32 against a $30 ceiling. The size was computed for a
    price that no longer exists, so it is refused rather than silently shrunk -
    the ledger must not claim a size that was never sent.
    """
    text = "XAUUSD SELL NOW 3870\nSl : 3882\nTp : 3860\nTp : 3855"
    result = evaluated_fixed(text, lots=0.02, ceiling=30.0,
                             quote=(3870.0, 3870.2))
    assert result.order_intent["size_lots"] == 0.02

    broker = FakeBroker(bid=3866.0, ask=3866.2)
    engine, _ledger, _ = engine_for(tmp_path, broker, fixed_lots=0.02,
                                    max_trade_risk_usd=30.0)
    outcome = engine.execute(result, envelope(text))

    assert outcome["executed"] is False
    assert broker.place_calls == []
    assert "moved since" in outcome["reason"]


# ---------------------------------------------------------------------------
# a fitted size is normal; only a size above the maximum is a bug
# ---------------------------------------------------------------------------


def test_the_engine_accepts_a_size_fitted_below_the_maximum(tmp_path):
    """0.01 lots over a 16-point stop is $16 of a $30 budget.

    The pipeline fitted the order down because 0.02 lots would have risked $32.
    The engine must send the fitted size, not reject it for not being exactly
    the configured lot.
    """
    text = "XAUUSD SELL NOW 3870\nSl : 3886\nTp : 3860\nTp : 3855"
    result = evaluated_fixed(text, lots=0.02, ceiling=30.0,
                             quote=(3870.0, 3870.2))
    assert result.order_intent["size_lots"] == 0.01

    broker = FakeBroker(bid=3870.0, ask=3870.2)
    engine, _ledger, _ = engine_for(tmp_path, broker, fixed_lots=0.02,
                                    max_trade_risk_usd=30.0)
    outcome = engine.execute(result, envelope(text))

    assert outcome["executed"] is True
    assert broker.place_calls[0]["qty"] == pytest.approx(0.01)


def test_the_engine_refuses_when_the_market_moved_after_the_size_was_fitted(tmp_path):
    """The size fitted a 12-point stop; by submission the stop is 16 away.

    0.02 lots then risks $32 against a $30 ceiling. The size was computed for a
    price that no longer exists, so it is refused rather than silently shrunk -
    the ledger must not claim a size that was never sent.
    """
    text = "XAUUSD SELL NOW 3870\nSl : 3882\nTp : 3860\nTp : 3855"
    result = evaluated_fixed(text, lots=0.02, ceiling=30.0,
                             quote=(3870.0, 3870.2))
    assert result.order_intent["size_lots"] == 0.02

    broker = FakeBroker(bid=3866.0, ask=3866.2)
    engine, _ledger, _ = engine_for(tmp_path, broker, fixed_lots=0.02,
                                    max_trade_risk_usd=30.0)
    outcome = engine.execute(result, envelope(text))

    assert outcome["executed"] is False
    assert broker.place_calls == []
    assert "moved since" in outcome["reason"]
