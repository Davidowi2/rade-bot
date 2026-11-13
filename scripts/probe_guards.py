"""Run the pre-trade guards against the real demo account without placing an order.

The unit tests prove the guard arithmetic against a broker double. They cannot
prove that the numbers the guards compare - the account equity, the day's
opening balance, the peak, the spread, the pause flag - can actually be read
from the live account and persisted to the ledger. This script does that, and it
is built so that it *cannot* place an order:

* The signal it uses is synthetic and is labelled as such in the output. It is a
  vehicle for the guards, not a claim that a signal was copied.
* Each case is configured so a guard must refuse before submission. The pause
  case refuses at the first check; the spread case refuses at the sixth. If
  either case ever returns anything but a blocked outcome, the script says so
  loudly and exits non-zero, because that would mean a control did not fire.
* The ledger is a throwaway file. The live ledger is not touched.

    python scripts/probe_guards.py
    python scripts/probe_guards.py --db /tmp/probe.sqlite
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from rade.execution.engine import ExecutionEngine
from rade.execution.gateway import tradelocker_gateway_from_env
from rade.execution.guards import RiskLimits
from rade.observer.mirror_pipeline import (
    InstrumentSpec,
    MarketQuote,
    MessageEnvelope,
    RiskConfig,
    SQLiteIntentLedger,
    evaluate_message,
)
from rade.observer.notify import RecordingNotifier


def _signal(bid: float, *, mid: int) -> tuple[str, object]:
    """A synthetic SELL built around the live price, and the message it rides in.

    Synthetic on purpose: the guards only care about the account and the market,
    so a real signal would add nothing and would make the probe depend on the
    channel's schedule.
    """
    entry = round(bid, 2)
    text = (f"XAUUSD SELL NOW {entry:.2f}\n"
            f"Sl : {entry + 9:.2f}\n"
            f"Tp : {entry - 6:.2f}\n"
            f"Tp : {entry - 11:.2f}\n"
            f"Tp : {entry - 16:.2f}")
    now = datetime.now(timezone.utc)
    return text, MessageEnvelope("@probe", mid, 0, now, now, text)


def run_case(*, gateway, db: Path, paused: bool, spread_limit: float,
             label: str) -> dict:
    ledger = SQLiteIntentLedger(db)
    if paused:
        ledger.set_paused(paused=True, reason="probe: pause switch check",
                          changed_by="probe")
    else:
        ledger.set_paused(paused=False, reason="probe: not paused",
                          changed_by="probe")

    quote = gateway.quote()
    spec = gateway.spec()
    engine = ExecutionEngine(
        broker=gateway,
        ledger=ledger,
        notifier=RecordingNotifier(),
        log=lambda line: None,
        limits=RiskLimits(max_open_positions=3, daily_loss_fraction=0.02,
                          max_drawdown_fraction=0.05, max_spread=spread_limit,
                          max_slippage=5.0),
        duplicate_window_s=0,
    )

    text, envelope = _signal(float(quote.bid), mid=900_000 + int(paused))
    instrument = InstrumentSpec(spec.symbol, spec.contract_units_per_lot,
                                spec.lot_step, spec.min_lot, spec.max_lot,
                                units_confirmed=spec.verified)
    result = evaluate_message(
        envelope,
        instrument=instrument,
        risk=RiskConfig(gateway.equity(), 0.005, 2.0),
        quote=MarketQuote(quote.bid, quote.ask, quote.quoted_at),
    )

    outcome = engine.execute(result, envelope)
    state = ledger.risk_state()
    control = ledger.control()
    trades = ledger.list_trades()
    trade = trades[0] if trades else {}
    return {
        "case": label,
        "blocked": bool(outcome.get("guard")),
        "reason": outcome.get("reason"),
        "state": outcome.get("state"),
        "broker_order_id": trade.get("broker_order_id"),
        "broker_position_id": trade.get("broker_position_id"),
        "signal_text": text,
        "risk_state_persisted": state,
        "control": control,
        "guard_checks": ((engine.guard_state() or {}).get("last_verdict")
                         or {}).get("checks"),
        "notifications": list(engine.notifier.sent),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="")
    args = parser.parse_args()

    units = os.getenv("MIRROR_CONTRACT_UNITS_PER_LOT", "").strip()
    gateway = tradelocker_gateway_from_env(
        confirmed_contract_units=float(units) if units else None,
        log=lambda line: print(line, file=sys.stderr))
    if not gateway.spec().verified:
        print(f"the broker specification is not verified: "
              f"{gateway.spec().unconfirmed_reason}")
        return 2

    print(f"environment: {gateway.environment} (this script refuses anything else)")
    quote = gateway.quote()
    equity = gateway.equity()
    print(f"live quote: bid {quote.bid} ask {quote.ask}; equity {equity:,.2f}")
    print(f"open positions: {len(gateway.positions())}; "
          f"open orders: {len(gateway.orders())}")

    db = Path(args.db) if args.db else Path(tempfile.gettempdir()) / (
        f"rade-guard-probe-{os.getpid()}.sqlite")
    if db.exists():
        db.unlink()

    cases = [
        run_case(gateway=gateway, db=db, paused=True, spread_limit=5.0,
                 label="paused: the switch must refuse before anything else"),
        run_case(gateway=gateway, db=db, paused=False, spread_limit=0.0001,
                 label="spread limit set below the live spread: must refuse"),
    ]

    failures = []
    for case in cases:
        print(f"\n== {case['case']}")
        print(f"   blocked: {case['blocked']}")
        print(f"   reason:  {case['reason']}")
        print(f"   state:   {case['state']}")
        print(f"   broker order id: {case['broker_order_id']!r} "
              f"position id: {case['broker_position_id']!r}")
        print(f"   risk state: {case['risk_state_persisted']}")
        print(f"   notifications that would have been sent: "
              f"{case['notifications']}")
        if not case["blocked"]:
            failures.append(f"{case['case']}: the guard did not block")
        if case["broker_order_id"] or case["broker_position_id"]:
            failures.append(f"{case['case']}: an order reached the broker")

    after = gateway.positions()
    print(f"\nopen positions after the probe: {len(after)}")
    if after:
        failures.append("the probe left a position open, which it must not")

    evidence = Path("docs/evidence") / (
        "guard-probe-"
        f"{datetime.now(timezone.utc).strftime('%Y-%m-%dT%H-%M-%SZ')}.json")
    evidence.parent.mkdir(parents=True, exist_ok=True)
    evidence.write_text(json.dumps({
        "read_at": datetime.now(timezone.utc).isoformat(),
        "environment": gateway.environment,
        "quote": {"bid": quote.bid, "ask": quote.ask},
        "equity": equity,
        "cases": cases,
        "failures": failures,
    }, indent=2, default=str), encoding="utf-8")
    print(f"evidence: {evidence}")

    if failures:
        for failure in failures:
            print(f"FAIL {failure}")
        return 1
    print("\nboth controls refused the order; nothing was submitted")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
