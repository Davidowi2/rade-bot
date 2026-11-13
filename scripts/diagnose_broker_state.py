"""Find out where the broker keeps an attached stop, and whether a move is visible.

This is a diagnostic, not part of the execution path. It places the smallest
possible demo order, changes its stop, and prints what each endpoint reports
before and after, so the engine's verification reads a field that actually
changes rather than one that merely looks plausible.

The engine needs an answer to one question: after ``modify_position`` returns
``True``, which endpoint reports the new stop? A verification that reads a
stale field will either block a good trade or, worse, confirm a move that never
happened.

Demo only: it goes through the same gateway, which refuses anything else.

    python scripts/diagnose_broker_state.py
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone

from rade.execution.gateway import tradelocker_gateway_from_env

FIELDS = ("id", "type", "status", "qty", "price", "stopPrice", "stopLoss",
          "stopLossType", "takeProfit", "takeProfitType", "isOpen",
          "positionId", "strategyId")


def _frame(client, endpoint: str, config_key: str, row_key: str) -> list[dict]:
    """Read a raw endpoint and zip its rows against the advertised columns.

    The SDK returns a DataFrame whose column set comes from the account
    config, so reading the raw payload is the only way to see every field the
    broker is willing to publish.
    """
    base = client.get_base_url()
    config = client.get_config() or {}
    columns = [col.get("id") for col in (config.get(config_key) or {}).get("columns", [])]
    payload = client._request("get", f"{base}/trade/accounts/{client.account_id}/{endpoint}")
    rows = (payload.get("d") or {}).get(row_key) or []
    return [dict(zip(columns, row)) for row in rows]


def open_orders(client) -> list[dict]:
    return _frame(client, "orders", "ordersConfig", "orders")


def history_orders(client) -> list[dict]:
    return _frame(client, "ordersHistory", "ordersHistoryConfig", "ordersHistory")


def positions(client) -> list[dict]:
    return _frame(client, "positions", "positionsConfig", "positions")


def show(label: str, rows: list[dict], keep) -> None:
    rows = [row for row in rows if keep(row)]
    print(f"--- {label}: {len(rows)} row(s) ---")
    for row in rows:
        print("  " + json.dumps({key: row.get(key) for key in FIELDS
                                 if key in row}))


def main() -> int:
    gateway = tradelocker_gateway_from_env(confirmed_contract_units=100.0)
    client = gateway.client
    spec = gateway.spec()
    if not spec.verified:
        print(f"specification not verified: {spec.unconfirmed_reason}")
        return 1

    config = client.get_config() or {}
    print("config keys carrying an order/position column set: "
          f"{[key for key in config if 'olumn' in key or key.endswith('Config')]}")

    quote = gateway.quote()
    entry = round(quote.ask, 2)
    stop = round(entry - 20.0, 2)
    target = round(entry + 20.0, 2)
    tag = f"RADE-diag-{int(datetime.now(timezone.utc).timestamp())}"
    print(f"quote bid={quote.bid} ask={quote.ask}")
    print(f"placing 0.01 lot buy, SL {stop}, TP {target}, tag {tag}")

    ack = gateway.place_market(qty=0.01, side="buy", stop_loss=stop,
                               take_profit=target, strategy_id=tag)
    print(f"order: accepted={ack.accepted} id={ack.order_id} detail={ack.detail}")
    if not ack.accepted:
        return 1

    position = None
    for _ in range(20):
        found = [p for p in positions(client) if str(p.get("strategyId")) == tag]
        if found:
            position = found[0]
            break
        time.sleep(2)
    if position is None:
        print("no position appeared")
        return 1

    position_id = str(position["id"])
    print(f"position: {json.dumps(position)}")

    def snapshot(label: str) -> None:
        by_position = lambda row: str(row.get("positionId")) == position_id  # noqa: E731
        show(f"{label} — open orders", open_orders(client), by_position)
        show(f"{label} — history orders", history_orders(client), by_position)
        current = [p for p in positions(client) if str(p.get("id")) == position_id]
        if current:
            print("  position: " + json.dumps(current[0]))

    snapshot("before the move")

    new_stop = round(entry - 10.0, 2)
    print(f"modify_position(stop_loss={new_stop}) -> "
          f"{gateway.modify_position(position_id=int(position_id), stop_loss=new_stop)}")
    time.sleep(3)
    snapshot("after the move")

    print(f"closing position {position_id}: "
          f"{gateway.close_quantity(position_id=int(position_id), qty=0)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
