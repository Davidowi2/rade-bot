"""Validate the execution path against the TradeLocker DEMO account.

Two modes, and the read-only one runs first on purpose:

    --probe    resolve the specification and read the account. No writes.
    --run      place one market order, then let the manager take the ladder.

The script refuses any environment other than ``demo``: the gateway it uses
checks both the configured environment name and the resolved host, so a
misconfiguration cannot point it at a live account.

What this proves and what it does not is written into the evidence file it
produces. It proves that an order is accepted, that a position appears with a
broker-side stop and target, that a partial close reduces it, that a stop move
is applied and confirmed by the broker, and that a restart reconciles instead
of resubmitting. It proves nothing about profitability, and it is not a
backtest.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from rade.execution.engine import ExecutionEngine, ManagedTrade, trade_id_for
from rade.execution.gateway import tradelocker_gateway_from_env
from rade.observer.mirror_pipeline import (
    InstrumentSpec,
    MarketQuote,
    MessageEnvelope,
    RiskConfig,
    SQLiteIntentLedger,
    evaluate_message,
)
from rade.observer.notify import RecordingNotifier

EVIDENCE_DIR = Path("docs/evidence")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _stamp() -> str:
    return _now().strftime("%Y-%m-%dT%H-%M-%SZ")


def signal_text(side: str, price: float, stop: float, targets: list[float]) -> str:
    lines = [f"XAUUSD {side.upper()} NOW {price:.2f}", f"Sl : {stop:.2f}"]
    lines.extend(f"Tp : {t:.2f}" for t in targets)
    return "\n".join(lines)


def drift(gateway, *, samples: int = 5, gap_s: float = 2.0) -> tuple[str, float]:
    """The direction the price is currently moving, and by how much.

    This is not a strategy and it predicts nothing. The ladder can only be
    observed if the price travels through its targets, and the demo feed here
    moves several points a minute in both directions; taking the side the price
    is already moving towards simply biases the entry in the direction the
    ladder needs, so it is reached inside one validation window instead of
    waiting for a reversal. The evidence file says so, and nothing here is
    claimed to be profitable.
    """
    first = gateway.quote()
    time.sleep(gap_s)
    last = first
    for _ in range(max(1, samples - 1)):
        last = gateway.quote()
        time.sleep(gap_s)
    moved = ((last.bid + last.ask) / 2.0) - ((first.bid + first.ask) / 2.0)
    return ("buy" if moved >= 0 else "sell"), moved


def build_engine(ledger_path: str, *, confirmed_units: float | None,
                 log=print):
    gateway = tradelocker_gateway_from_env(confirmed_contract_units=confirmed_units,
                                           log=log)
    ledger = SQLiteIntentLedger(ledger_path)
    engine = ExecutionEngine(broker=gateway, ledger=ledger,
                             notifier=RecordingNotifier(), log=log)
    return gateway, ledger, engine


def probe(gateway, ledger, engine) -> dict:
    """Read the account and report what the broker says. No writes."""
    spec = gateway.spec()
    quote = gateway.quote()
    positions = gateway.positions()
    orders = gateway.orders(history=True)
    return {
        "mode": "probe",
        "read_only": True,
        "spec": spec.to_dict(),
        "quote": {"bid": quote.bid, "ask": quote.ask,
                  "quoted_at": quote.quoted_at.isoformat()},
        "positions": [p.__dict__ for p in positions],
        "orders": [o.__dict__ for o in orders],
        "writes": "none",
    }


def run(gateway, ledger, engine, *, timeout_s: float, poll_s: float,
        target_offsets: list[float], stop_distance: float, log=print) -> dict:
    """Place one trade and manage it until it closes or the window ends."""
    events: list[dict] = []

    def note(stage: str, detail: dict | None = None) -> None:
        events.append({"at": _now().isoformat(), "stage": stage,
                       "detail": detail or {}})
        log(f"[{stage}] {json.dumps(detail or {}, default=str)[:300]}")

    spec = gateway.spec()
    note("spec", spec.to_dict())
    if not spec.verified:
        return {"mode": "run", "aborted": spec.unconfirmed_reason, "events": events}

    equity = gateway.equity()
    note("equity", {"equity": equity,
                    "source": "the broker's projectedBalance"})
    if equity <= 0:
        return {"mode": "run", "aborted": "the broker reported no usable equity",
                "events": events}

    side, moved = drift(gateway)
    # A flat tape says nothing about direction, so wait for a measurement that
    # actually moved before committing to a side.
    attempts = 1
    while abs(moved) < 0.5 and attempts < 3:
        side, moved = drift(gateway)
        attempts += 1
    note("drift", {"side": side, "moved_points": round(moved, 2),
                   "measurements": attempts,
                   "purpose": "choose the side the price is already travelling, "
                              "so the ladder is reachable in one window; this is "
                              "not a strategy"})

    quote = gateway.quote()
    # The demo feed occasionally publishes a crossed book (bid above ask) for a
    # moment. Intake refuses it, correctly, so the harness waits for a sane one
    # instead of reporting a blocked run that says nothing about the ladder.
    crossed = 0
    while quote.ask <= quote.bid and crossed < 6:
        crossed += 1
        note("quote_crossed", {"bid": quote.bid, "ask": quote.ask,
                               "attempt": crossed,
                               "note": "the demo feed published bid >= ask; "
                                       "intake would block this, so it is not used"})
        time.sleep(3)
        quote = gateway.quote()
    note("quote", {"bid": quote.bid, "ask": quote.ask,
                   "crossed_retries": crossed})
    # Enter at the price this order will actually meet, and build the ladder
    # from there.
    entry = round(quote.ask if side == "buy" else quote.bid, 2)
    sign = 1.0 if side == "buy" else -1.0
    stop = round(entry - sign * stop_distance, 2)
    targets = [round(entry + sign * offset, 2) for offset in target_offsets]
    text = signal_text(side, entry, stop, targets)
    note("signal", {"text": text})

    # A synthetic Telegram envelope: the real parser and the real sizing rule
    # run, so what is executed is what a channel message would have produced.
    message = MessageEnvelope(
        channel="@demo-validation",
        message_id=int(time.time()),
        revision=0,
        posted_at=_now(),
        received_at=_now(),
        text=text,
        source="demo_validation",
    )
    result = evaluate_message(
        message,
        instrument=InstrumentSpec(
            spec.symbol, spec.contract_units_per_lot, spec.lot_step,
            spec.min_lot, spec.max_lot, units_confirmed=spec.verified),
        risk=RiskConfig(equity, 0.005, 60.0),
        quote=MarketQuote(quote.bid, quote.ask, quote.quoted_at),
    )
    note("validated", {"status": result.status, "reason": result.reason,
                       "intent": result.order_intent})
    if result.status != "paper_intent":
        return {"mode": "run", "aborted": f"validation produced {result.status}",
                "reason": result.reason, "events": events}

    trade_id = trade_id_for(result.event_key)
    outcome = engine.execute(result, message)
    note("submitted", outcome)
    if not outcome.get("executed"):
        return {"mode": "run", "aborted": "the order was not accepted",
                "outcome": outcome, "events": events}

    trade = ManagedTrade.from_row(ledger.get_trade(trade_id))
    fill = engine.confirm_fill(trade)
    note("fill", fill)

    deadline = time.time() + timeout_s
    passes = 0
    watched: list[dict] = []
    while time.time() < deadline:
        trade = ManagedTrade.from_row(ledger.get_trade(trade_id))
        if trade is None or trade.state in {"closed", "review"}:
            break
        try:
            seen = gateway.quote()
            watched.append({"at": _now().isoformat(), "bid": seen.bid,
                            "ask": seen.ask, "legs_done": trade.legs_done})
        except Exception:
            pass
        outcome = engine.manage_trade(trade)
        passes += 1
        if outcome.get("partial") or outcome.get("closed") or outcome.get("stopped"):
            note("manage", outcome)
        elif outcome.get("retry") or outcome.get("error"):
            note("deferred", outcome)
        time.sleep(max(1.0, poll_s))
    note("watch", {"passes": passes, "quotes": watched[-12:]})

    trade = ManagedTrade.from_row(ledger.get_trade(trade_id))
    transitions = ledger.transitions(trade_id)
    note("final", {"state": trade.state if trade else None,
                   "legs_done": trade.legs_done if trade else None,
                   "note": trade.note if trade else None})

    # A trade still open at the end of the window is closed deliberately, so
    # the demo account is not left holding a position overnight. This is
    # recorded rather than hidden.
    closed_by_script = False
    if trade is not None and trade.state not in {"closed"}:
        position = engine._position_for(trade_id)
        if position is not None:
            engine.broker.close_quantity(position_id=position.id, qty=0)
            engine._transition(trade, "cleanup", "closed_by_script",
                               "the validation window ended; the position was "
                               "closed deliberately")
            trade.state = "closed"
            trade.management_stopped = True
            engine._save(trade)
            closed_by_script = True
            note("cleanup", {"closed_by_script": True})

    return {
        "mode": "run",
        "trade_id": trade_id,
        "closed_by_script": closed_by_script,
        "events": events,
        "transitions": transitions,
        "final_state": trade.state if trade else None,
    }


def run_reconcile(ledger_path: str, *, confirmed_units: float | None,
                  log=print) -> dict:
    """Place one trade, then prove a restart adopts it instead of resubmitting.

    The restart is simulated by building a *new* gateway (a new login and a new
    session) and a *new* engine over the *same* durable ledger. That is exactly
    what a process restart leaves behind: a fresh client, an empty memory, and
    a database that remembers.
    """
    events: list[dict] = []

    def note(stage: str, detail: dict | None = None) -> None:
        events.append({"at": _now().isoformat(), "stage": stage,
                       "detail": detail or {}})
        log(f"[{stage}] {json.dumps(detail or {}, default=str)[:300]}")

    gateway, ledger, engine = build_engine(ledger_path, confirmed_units=confirmed_units,
                                           log=log)
    spec = gateway.spec()
    if not spec.verified:
        return {"aborted": spec.unconfirmed_reason, "events": events}

    quote = gateway.quote()
    equity = gateway.equity()
    side, moved = drift(gateway)
    note("drift", {"side": side, "moved_points": round(moved, 2)})
    quote = gateway.quote()
    entry = round(quote.ask if side == "buy" else quote.bid, 2)
    sign = 1.0 if side == "buy" else -1.0
    # The first target is far enough that the fill cannot land beyond it, and
    # the last is far enough that the broker does not close the position before
    # the reconciliation has been observed.
    text = signal_text(side, entry, round(entry - sign * 10.0, 2),
                       [round(entry + sign * 3.0, 2),
                        round(entry + sign * 6.0, 2),
                        round(entry + sign * 20.0, 2)])
    message = MessageEnvelope(channel="@demo-validation", message_id=int(time.time()),
                              revision=0, posted_at=_now(), received_at=_now(),
                              text=text, source="demo_reconcile")
    result = evaluate_message(
        message,
        instrument=InstrumentSpec(spec.symbol, spec.contract_units_per_lot,
                                  spec.lot_step, spec.min_lot, spec.max_lot,
                                  units_confirmed=spec.verified),
        risk=RiskConfig(equity, 0.005, 60.0),
        quote=MarketQuote(quote.bid, quote.ask, quote.quoted_at),
    )
    note("validated", {"status": result.status, "size": (result.order_intent or {}).get("size_lots")})
    if result.status != "paper_intent":
        return {"aborted": result.reason, "events": events}

    trade_id = trade_id_for(result.event_key)
    outcome = engine.execute(result, message)
    note("submitted", outcome)
    if not outcome.get("executed"):
        return {"aborted": "not accepted", "outcome": outcome, "events": events}

    trade = ManagedTrade.from_row(ledger.get_trade(trade_id))
    note("fill", engine.confirm_fill(trade))

    orders_before = len(gateway.orders(history=True))
    positions_before = len(gateway.positions())
    note("before_restart", {"orders": orders_before, "positions": positions_before})

    # --- the restart: new login, new engine, same durable ledger ----------
    gateway2, ledger2, engine2 = build_engine(ledger_path,
                                              confirmed_units=confirmed_units,
                                              log=log)
    summary = engine2.reconcile()
    orders_after = len(gateway2.orders(history=True))
    trade_after = ManagedTrade.from_row(ledger2.get_trade(trade_id))
    note("after_restart", {
        "reconcile": summary,
        "orders": orders_after,
        "order_delta": orders_after - orders_before,
        "state": trade_after.state if trade_after else None,
        "position_id": trade_after.broker_position_id if trade_after else None,
        "legs_done": trade_after.legs_done if trade_after else None,
    })

    # --- clean up: the demo account is not left holding a position --------
    position = engine2._position_for(trade_id)
    closed = False
    if position is not None:
        closed = engine2.broker.close_quantity(position_id=position.id, qty=0)
        engine2._transition(trade_after, "cleanup", "closed_by_script",
                            "the reconciliation check ended; the position was "
                            "closed deliberately")
        trade_after.state = "closed"
        trade_after.management_stopped = True
        engine2._save(trade_after)
    note("cleanup", {"closed": closed})

    return {
        "trade_id": trade_id,
        "orders_before_restart": orders_before,
        "orders_after_restart": orders_after,
        "resubmitted": orders_after > orders_before,
        "reconcile_summary": summary,
        "final_state": trade_after.state if trade_after else None,
        "events": events,
        "transitions": ledger2.transitions(trade_id),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--probe", action="store_true",
                        help="read-only: resolve the specification, write nothing")
    parser.add_argument("--run", action="store_true",
                        help="place one demo trade and manage it")
    parser.add_argument("--reconcile", action="store_true",
                        help="place one demo trade, then prove a restart adopts "
                             "it instead of resubmitting")
    parser.add_argument("--ledger", default="data/demo_execution.sqlite")
    parser.add_argument("--timeout", type=float, default=900.0,
                        help="how long to manage the trade, in seconds")
    parser.add_argument("--poll", type=float, default=5.0)
    parser.add_argument("--stop-distance", type=float, default=10.0)
    parser.add_argument("--target-offsets", type=float, nargs="+",
                        default=[3.0, 6.0, 20.0],
                        help="distance from entry to each target, in price units. "
                             "Spacing matters more than size. The first target must "
                             "clear the fill slippage, because a fill at or beyond "
                             "it makes the published ladder impossible to walk and "
                             "management stops at the fill; the last target must be "
                             "far enough that the broker's own attached take profit "
                             "cannot fire before the manager has walked the earlier "
                             "legs, which would prove nothing")
    parser.add_argument("--confirmed-units", type=float, default=100.0,
                        help="the contract size a human confirmed against the broker")
    parser.add_argument("--out", default=None)
    args = parser.parse_args(argv)

    if not args.probe and not args.run and not args.reconcile:
        parser.error("choose --probe, --run or --reconcile")

    confirmed = args.confirmed_units if args.confirmed_units > 0 else None

    if args.reconcile:
        evidence = run_reconcile(args.ledger, confirmed_units=confirmed)
        evidence["environment"] = "demo"
        evidence["mode"] = "reconcile"
        evidence["finished_at"] = _now().isoformat()
        out = Path(args.out) if args.out else (
            EVIDENCE_DIR / f"demo-reconcile-{_stamp()}.json")
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(evidence, indent=2, default=str), encoding="utf-8")
        print(f"evidence: {out}")
        return 0

    gateway, ledger, engine = build_engine(args.ledger, confirmed_units=confirmed)

    if args.probe:
        evidence = probe(gateway, ledger, engine)
    else:
        evidence = run(gateway, ledger, engine, timeout_s=args.timeout,
                       poll_s=args.poll, target_offsets=args.target_offsets,
                       stop_distance=args.stop_distance)

    evidence["environment"] = "demo"
    evidence["finished_at"] = _now().isoformat()
    out = Path(args.out) if args.out else (
        EVIDENCE_DIR / f"demo-execution-{_stamp()}.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(evidence, indent=2, default=str), encoding="utf-8")
    print(f"evidence: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
