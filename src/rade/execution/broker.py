"""The broker contract for the demo execution path.

This module defines what the execution engine is allowed to ask a broker, and
what it gets back. It contains no broker-specific code: the TradeLocker
implementation lives in :mod:`rade.execution.gateway`, and the engine in
:mod:`rade.execution.engine` is written against this contract alone.

That separation is deliberate. The spec requires mocks and simulations *before*
the demo account is touched, and a contract is the only way a mock can be
honest - if the engine talked to a concrete SDK client, a test double would
have to imitate an SDK, and a test double that imitates an SDK proves only
that the imitation works.

Two rules are encoded in the types rather than left to comments:

* **A broker spec is either verified or it carries a reason.** There is no
  "partially resolved" state. :class:`BrokerSpec` has a ``verified`` flag and
  an ``unconfirmed_reason``; the engine refuses to size anything when
  ``verified`` is false.
* **A write returns an acknowledgement, not a boolean.** A broker order is
  accepted, rejected, or unknown - and "unknown" is not the same as "no".
  :class:`OrderAck` carries the broker's order id when there is one.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Protocol, runtime_checkable


class BrokerError(RuntimeError):
    """A broker call failed.

    The message never contains a credential. The TradeLocker gateway scrubs
    before it gets here.
    """


class BrokerRefused(BrokerError):
    """The gateway refused to act at all (wrong environment, missing config)."""


@dataclass(frozen=True)
class BrokerSpec:
    """Everything the broker reports about the instrument and the account.

    ``verified`` is the whole point of this type. A spec assembled from
    partially-resolved values looks exactly like a complete one unless
    something marks it, so the mark is a field and the engine reads it before
    it computes a size.
    """

    environment: str
    base_url: str
    account_id: int
    account_number: int
    symbol: str
    instrument_id: int
    trade_route_id: str
    #: The broker's own contract size: ``lotSize`` in the instrument details.
    contract_units_per_lot: float
    lot_step: float
    min_lot: float
    max_lot: float
    tick_size: float
    symbol_status: str
    #: The value a human confirmed against the broker's specification.
    confirmed_contract_units: float | None = None
    verified: bool = False
    unconfirmed_reason: str | None = None

    def to_dict(self) -> dict:
        return {
            "environment": self.environment,
            "base_url": self.base_url,
            "account_id": self.account_id,
            "account_number": self.account_number,
            "symbol": self.symbol,
            "instrument_id": self.instrument_id,
            "trade_route_id": self.trade_route_id,
            "contract_units_per_lot": self.contract_units_per_lot,
            "lot_step": self.lot_step,
            "min_lot": self.min_lot,
            "max_lot": self.max_lot,
            "tick_size": self.tick_size,
            "symbol_status": self.symbol_status,
            "confirmed_contract_units": self.confirmed_contract_units,
            "verified": self.verified,
            "unconfirmed_reason": self.unconfirmed_reason,
        }


@dataclass(frozen=True)
class BrokerQuote:
    bid: float
    ask: float
    quoted_at: datetime

    def __post_init__(self) -> None:
        if self.quoted_at.tzinfo is None:
            object.__setattr__(self, "quoted_at", self.quoted_at.replace(tzinfo=timezone.utc))


@dataclass(frozen=True)
class OrderAck:
    """The broker's answer to an order request."""

    accepted: bool
    order_id: int | None = None
    detail: str = ""
    #: The full request that was sent, for the ledger. Never a credential.
    request: dict = field(default_factory=dict)


@dataclass(frozen=True)
class BrokerOrder:
    id: int
    instrument_id: int
    side: str
    qty: float
    status: str
    type: str = ""
    filled_qty: float = 0.0
    avg_price: float = 0.0
    price: float = 0.0
    position_id: int | None = None
    stop_loss: float | None = None
    take_profit: float | None = None
    #: The trigger price of a ``stop`` order. This, not ``stop_loss``, is
    #: where the broker publishes the live stop of an open position.
    stop_price: float | None = None
    strategy_id: str | None = None
    created_at: datetime | None = None

    @property
    def is_filled(self) -> bool:
        return self.status.lower() == "filled"

    @property
    def is_open(self) -> bool:
        return bool(self.status) and self.status.lower() not in {
            "filled", "cancelled", "canceled", "refused", "unplaced", "removed", "expired",
        }


@dataclass(frozen=True)
class BrokerPosition:
    id: int
    instrument_id: int
    side: str
    qty: float
    avg_price: float
    stop_loss_id: int | None = None
    take_profit_id: int | None = None
    strategy_id: str | None = None
    unrealized_pl: float = 0.0


@runtime_checkable
class Broker(Protocol):
    """The write and read surface the execution engine uses."""

    def spec(self) -> BrokerSpec:
        """Resolve and verify the instrument/account specification."""
        ...

    def quote(self) -> BrokerQuote:
        """The current bid/ask for the instrument."""
        ...

    def place_market(self, *, qty: float, side: str, stop_loss: float,
                     take_profit: float, strategy_id: str) -> OrderAck:
        """Place a market order with a broker-side stop loss and take profit."""
        ...

    def modify_position(self, *, position_id: int, stop_loss: float | None = None,
                        take_profit: float | None = None) -> bool:
        """Change the attached stop loss and/or take profit of an open position."""
        ...

    def close_quantity(self, *, position_id: int, qty: float) -> bool:
        """Reduce a position by ``qty``. ``qty=0`` closes it entirely."""
        ...

    def positions(self) -> list[BrokerPosition]:
        """Open positions."""
        ...

    def orders(self, *, history: bool = False) -> list[BrokerOrder]:
        """Orders, optionally including filled and cancelled history."""
        ...
