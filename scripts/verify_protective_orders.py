"""Verify the protective-order lifecycle on the destination account.

The exit policy rests on two assumptions about TradeLocker's
``PATCH /trade/positions/{id}``:

  1. patching only the stop leaves the take profit attached;
  2. patching only the take profit leaves the stop attached.

If either is false, the engine's "move the stop, then ensure the target"
sequence would silently remove the other protection - a position left with a
stop and no target, or worse, with neither.

Read-mostly: places one 0.01 lot order, patches it twice, reads the live levels
after each patch, and closes it in a ``finally``. Never prints credentials.

    python scripts/verify_protective_orders.py
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from rade.execution.gateway import tradelocker_gateway_from_env  # noqa: E402

TRUTHY = {"1", "true", "yes"}


def _confirmed_units() -> float | None:
    """The human-confirmed contract size, read the way the service reads it.

    ``tradelocker_gateway_from_env`` takes this as an argument rather than
    reading the environment itself, so the same two variables the deployment
    uses are read here.
    """
    raw = str(os.getenv("MIRROR_CONTRACT_UNITS_PER_LOT", "")).strip()
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def live_levels(gateway, position_id: int) -> dict | None:
    """The stop and target the broker is actually carrying for a position.

    The live levels are the open orders the position points at, not the
    ``stopLoss``/``takeProfit`` fields on the filled entry order - those never
    change after submission and would confirm a move that never happened.
    """
    positions = [p for p in gateway.positions() if p.id == position_id]
    if not positions:
        return None
    position = positions[0]
    # history=False: the open-orders feed. history=True returns the filled
    # entry order, whose stopLoss/takeProfit are frozen at submission.
    orders = {order.id: order for order in gateway.orders(history=False)}
    stop = orders.get(position.stop_loss_id) if position.stop_loss_id else None
    target = orders.get(position.take_profit_id) if position.take_profit_id else None
    return {
        "stop": stop.stop_price if stop else None,
        "stop_status": stop.status if stop else None,
        "target": target.price if target else None,
        "target_status": target.status if target else None,
    }


def main() -> int:
    if os.getenv("MIRROR_CONTRACT_UNITS_CONFIRMED", "").strip().lower() not in TRUTHY:
        print("MIRROR_CONTRACT_UNITS_CONFIRMED is not set: the specification "
              "cannot be verified, so nothing is sent.\n"
              "The deployment sets it; this script needs it because "
              "tradelocker_gateway_from_env takes the value as an argument.")
        return 2
    gateway = tradelocker_gateway_from_env(
        confirmed_contract_units=_confirmed_units(), log=lambda *_: None)
    spec = gateway.spec()
    if not spec.verified:
        print(f"the instrument specification is not verified: "
              f"{spec.unconfirmed_reason}")
        return 2

    quote = gateway.quote()
    entry = round(float(quote.ask), 2)
    stop = round(entry - 20.0, 2)
    target = round(entry + 20.0, 2)
    tag = f"RADE-verify-{int(time.time())}"
    print(f"instrument {spec.symbol} {spec.instrument_id} on "
          f"{spec.environment} account {spec.account_id}")
    print(f"quote bid={quote.bid} ask={quote.ask}")
    print(f"placing 0.01 lot buy, SL {stop}, TP {target}, tag {tag}")

    ack = gateway.place_market(qty=0.01, side="buy", stop_loss=stop,
                               take_profit=target, strategy_id=tag)
    print(f"order accepted={ack.accepted} id={ack.order_id} detail={ack.detail}")
    if not ack.accepted:
        return 1

    position_id = None
    try:
        for _ in range(20):
            matches = [p.id for p in gateway.positions()
                       if p.strategy_id == tag]
            if matches:
                position_id = matches[0]
                break
            time.sleep(2)
        if position_id is None:
            print("no position appeared")
            return 1
        print(f"position {position_id}")

        before = live_levels(gateway, position_id)
        print(f"  as placed      : {before}")
        assert before and before["stop"] == stop, "the stop was not attached"
        assert before and before["target"] == target, "the target was not attached"

        # 1. Patch only the stop.
        new_stop = round(entry - 10.0, 2)
        gateway.modify_position(position_id=position_id, stop_loss=new_stop)
        time.sleep(3)
        after_stop = live_levels(gateway, position_id)
        print(f"  stop patched   : {after_stop}")
        assert after_stop and after_stop["stop"] == new_stop, \
            "the stop did not move"
        assert after_stop and after_stop["target"] == target, \
            "PATCHING THE STOP REMOVED THE TAKE PROFIT"

        # 2. Patch only the target.
        new_target = round(entry + 15.0, 2)
        gateway.modify_position(position_id=position_id, take_profit=new_target)
        time.sleep(3)
        after_target = live_levels(gateway, position_id)
        print(f"  target patched : {after_target}")
        assert after_target and after_target["target"] == new_target, \
            "the target did not move"
        assert after_target and after_target["stop"] == new_stop, \
            "PATCHING THE TARGET REMOVED THE STOP"

        print("\nOK: each patch is a partial update; the other level survives.")
        return 0
    finally:
        if position_id is not None:
            try:
                gateway.close_quantity(position_id=position_id, qty=0)
                print(f"closed position {position_id}")
            except Exception as exc:
                print(f"could not close position {position_id}: "
                      f"{type(exc).__name__}: {exc}")


if __name__ == "__main__":
    sys.exit(main())
