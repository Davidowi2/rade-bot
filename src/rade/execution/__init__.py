"""Demo-only TradeLocker execution path.

The modules are layered so each one can be tested without the next:

* :mod:`rade.execution.broker` — the contract and its data types. No I/O.
* :mod:`rade.execution.ladder` — the exit policy. Pure arithmetic. No I/O.
* :mod:`rade.execution.gateway` — the only module that talks to the broker.
* :mod:`rade.execution.engine` — submission, management and reconciliation,
  written against the contract so a test double can drive it.

There is no live-account account path. The gateway refuses anything that is
not the demo environment, and the engine refuses to size a trade against a
broker specification that a human has not confirmed.
"""
from rade.execution.broker import (
    Broker,
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
from rade.execution.ladder import (
    ExitLeg,
    ExitPlan,
    LadderError,
    build_exit_plan,
    round_down_to_step,
    stop_move_is_risk_reducing,
    target_reached,
)

__all__ = [
    "ACTIVE_STATES",
    "Broker",
    "BrokerError",
    "BrokerOrder",
    "BrokerPosition",
    "BrokerQuote",
    "BrokerRefused",
    "BrokerSpec",
    "ExecutionEngine",
    "ExitLeg",
    "ExitPlan",
    "LadderError",
    "ManagedTrade",
    "OrderAck",
    "STATE_ACCEPTED",
    "STATE_BLOCKED",
    "STATE_CLOSED",
    "STATE_NOT_COPIED",
    "STATE_OPEN",
    "STATE_PARTIAL",
    "STATE_REJECTED",
    "STATE_REVIEW",
    "STATE_SUBMITTED",
    "STATE_VALIDATED",
    "build_exit_plan",
    "round_down_to_step",
    "stop_move_is_risk_reducing",
    "target_reached",
    "trade_id_for",
]
