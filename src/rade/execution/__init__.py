"""Demo-only TradeLocker execution path.

The modules are layered so each one can be tested without the next:

* :mod:`rade.execution.broker` — the contract and its data types. No I/O.
* :mod:`rade.execution.ladder` — the exit policy. Pure arithmetic. No I/O.
* :mod:`rade.execution.sizing` — the fixed-lot rule. Pure arithmetic. No I/O.
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
from rade.execution.ladder import (
    ACTION_BROKER_TARGET,
    ACTION_CLOSE,
    ACTION_STOP_MOVE,
    POLICY_HOLD_TO_FINAL,
    POLICY_LADDER,
    ExitLeg,
    ExitPlan,
    LadderError,
    build_exit_plan,
    round_down_to_step,
    stop_move_is_risk_reducing,
    target_reached,
)
from rade.execution.sizing import LotSpec, check_fixed_lot, describe_fixed_lot

#: Names that live in :mod:`rade.execution.engine`.
#:
#: The engine is imported on demand rather than here. ``engine`` imports
#: :mod:`rade.observer.mirror_pipeline` for the pipeline's result types, and
#: ``mirror_pipeline`` imports :mod:`rade.execution.sizing`. Importing the
#: engine eagerly therefore closes a cycle: ``import rade.observer.mirror_pipeline``
#: would run this file, which would import ``engine``, which would try to read
#: names from a ``mirror_pipeline`` that has only executed as far as its own
#: ``sizing`` import. That fails with ``ImportError: cannot import name
#: 'STATUS_MANUAL_REVIEW' from partially initialized module``.
#:
#: Deferring the engine import means ``mirror_pipeline`` can be the first
#: module imported in a process, which is what the tests and the service do.
_ENGINE_EXPORTS = frozenset(
    {
        "ACTIVE_STATES",
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
        "ExecutionEngine",
        "ManagedTrade",
        "trade_id_for",
    }
)


def __getattr__(name: str) -> object:
    """Resolve the engine re-exports on first use (PEP 562)."""
    if name in _ENGINE_EXPORTS:
        from rade.execution import engine as _engine

        value = getattr(_engine, name)
        globals()[name] = value
        return value
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted(set(__all__))

__all__ = [
    "ACTIVE_STATES",
    "ACTION_BROKER_TARGET",
    "ACTION_CLOSE",
    "ACTION_STOP_MOVE",
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
    "LotSpec",
    "ManagedTrade",
    "OrderAck",
    "POLICY_HOLD_TO_FINAL",
    "POLICY_LADDER",
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
    "check_fixed_lot",
    "describe_fixed_lot",
    "round_down_to_step",
    "stop_move_is_risk_reducing",
    "target_reached",
    "trade_id_for",
]
