"""A throttled broker edge must not become a refused signal.

On 2026-11-17 at 13:11 a BUY call was validated, sized, and then died on
``TLAPIException: Request failed Received response: '<!doctype html>...Access
denied'``. The broker's gateway had refused the *response*, and the bot treated
that as the broker refusing the *trade*.

These tests hold the line in both directions: a refusal from the edge is
repeated, and a refusal from the broker is not. The dangerous half is the
retry on an order, so most of this file is about not sending one twice.
"""
from __future__ import annotations

import pandas as pd
import pytest

from rade.execution.broker import BrokerError
from rade.execution.gateway import (
    READ_ATTEMPTS,
    TradeLockerGateway,
    is_transient,
)

BLOCK_PAGE = ("TLAPIException: Request failed Received response: '<!doctype html>"
              "<html class=\"no-js\"><head><title>Access denied</title>")


def frame(rows: list[dict]) -> pd.DataFrame:
    return pd.DataFrame(rows)


ORDER_COLUMNS = {
    "id": 77, "tradableInstrumentId": 1808, "side": "buy", "qty": 0.02,
    "status": "Filled", "type": "market", "filledQty": 0.02, "avgPrice": 3854.0,
    "price": 0.0, "positionId": 55, "stopLoss": 3845.0, "takeProfit": 3870.0,
    "stopPrice": 0.0, "strategyId": "RADE-abc", "createdDate": 1_700_000_000_000,
}


class Client:
    """A client whose edge can be made to fail a fixed number of times."""

    account_id = 2509174
    acc_num = 2

    def __init__(self):
        self.details = {
            "lotSize": 100, "lotStep": 0.01, "minLot": 0.01, "maxLot": 50,
            "tickSize": [{"tickSize": 0.01}], "symbolStatus": "FULLY_OPEN",
        }
        self.accounts = [{"id": 2509174, "accNum": 2, "currency": "USD"}]
        self.created: list[dict] = []
        self.position_failures = 0
        self.position_calls = 0
        self.create_failures = 0
        self.order_rows: list[dict] = []
        self.orders_unreadable = False
        self.create_error = Exception(BLOCK_PAGE)

    # -- reads ------------------------------------------------------------
    def get_instrument_id_from_symbol_name(self, symbol):
        return 1808

    def get_instrument_details(self, instrument_id):
        return self.details

    def get_trade_route_id(self, instrument_id):
        return "1869"

    def get_trade_accounts(self):
        return self.accounts

    def get_latest_bid_price(self, instrument_id):
        return 3915.0

    def get_latest_asking_price(self, instrument_id):
        return 3915.2

    def get_account_state(self):
        return {"projectedBalance": 10_000.0}

    def get_all_positions(self):
        self.position_calls += 1
        if self.position_failures > 0:
            self.position_failures -= 1
            raise Exception(BLOCK_PAGE)
        return frame([])

    def get_all_orders(self, history=False):
        if self.orders_unreadable:
            raise Exception(BLOCK_PAGE)
        return frame(self.order_rows)

    # -- writes -----------------------------------------------------------
    def create_order(self, **kwargs):
        self.created.append(kwargs)
        if self.create_failures > 0:
            self.create_failures -= 1
            raise self.create_error
        return 1234

    def modify_position(self, position_id, params):
        return True

    def close_position(self, position_id=0, close_quantity=0):
        return True


def gateway(client: Client) -> TradeLockerGateway:
    return TradeLockerGateway(client, environment="demo",
                              base_url="https://demo.tradelocker.com",
                              confirmed_contract_units=100.0, log=lambda line: None)


@pytest.fixture(autouse=True)
def no_sleeping(monkeypatch):
    """The backoff is real; the waiting is not."""
    monkeypatch.setattr("rade.execution.gateway.time.sleep", lambda _s: None)


# ---------------------------------------------------------------------------
# what counts as the edge rather than the broker
# ---------------------------------------------------------------------------


def test_an_html_block_page_is_transient():
    assert is_transient(Exception(BLOCK_PAGE))
    assert is_transient("HTTPError: 429 Client Error: Too Many Requests")
    assert is_transient("502 Bad Gateway")


def test_a_broker_rejection_is_not_transient():
    # "the broker said no" is an answer. Repeating it would just ask twice.
    assert not is_transient(Exception("Rejected: insufficient margin"))
    assert not is_transient(Exception("invalid side 'sideways'"))
    assert not is_transient(Exception("symbol is CLOSE_ONLY"))


# ---------------------------------------------------------------------------
# reads
# ---------------------------------------------------------------------------


def test_a_throttled_read_is_repeated_and_still_answers():
    client = Client()
    client.position_failures = 2
    assert gateway(client).positions() == []
    assert client.position_calls == 3


def test_a_read_that_never_answers_raises_and_says_so():
    client = Client()
    client.position_failures = 99
    with pytest.raises(BrokerError) as caught:
        gateway(client).positions()
    assert client.position_calls == READ_ATTEMPTS
    assert "Access denied" in str(caught.value) or "doctype" in str(caught.value)


def test_equity_survives_a_throttled_edge():
    """Equity is what the sizing and the breakers read; it must not give up."""
    client = Client()
    calls = {"n": 0}
    original = client.get_account_state

    def flaky():
        calls["n"] += 1
        if calls["n"] == 1:
            raise Exception(BLOCK_PAGE)
        return original()

    client.get_account_state = flaky
    assert gateway(client).equity() == pytest.approx(10_000.0)
    assert calls["n"] == 2


# ---------------------------------------------------------------------------
# the order: never twice
# ---------------------------------------------------------------------------


def test_an_order_is_resent_once_when_the_broker_has_nothing():
    client = Client()
    client.create_failures = 1
    ack = gateway(client).place_market(qty=0.02, side="buy", stop_loss=3845.0,
                                       take_profit=3870.0, strategy_id="RADE-abc")
    assert ack.accepted is True
    assert ack.order_id == 1234
    assert len(client.created) == 2


def test_an_order_that_already_landed_is_not_sent_again():
    """The whole point: a refused response is not a refused order."""
    client = Client()
    client.create_failures = 1
    client.order_rows = [dict(ORDER_COLUMNS)]
    ack = gateway(client).place_market(qty=0.02, side="buy", stop_loss=3845.0,
                                       take_profit=3870.0, strategy_id="RADE-abc")
    assert ack.accepted is True
    assert ack.order_id == 77
    assert len(client.created) == 1, "the order was sent twice"


def test_an_order_is_not_resent_when_the_broker_cannot_be_read():
    """An unreadable feed is not evidence that nothing landed."""
    client = Client()
    client.create_failures = 1
    client.orders_unreadable = True
    client.position_failures = 99
    ack = gateway(client).place_market(qty=0.02, side="buy", stop_loss=3845.0,
                                       take_profit=3870.0, strategy_id="RADE-abc")
    assert ack.accepted is False
    assert len(client.created) == 1
    assert "not being resent" in ack.detail


def test_a_broker_rejection_is_returned_without_a_retry():
    client = Client()
    client.create_failures = 1
    client.create_error = Exception("Rejected: insufficient margin")
    ack = gateway(client).place_market(qty=0.02, side="buy", stop_loss=3845.0,
                                       take_profit=3870.0, strategy_id="RADE-abc")
    assert ack.accepted is False
    assert len(client.created) == 1
    assert "insufficient margin" in ack.detail
