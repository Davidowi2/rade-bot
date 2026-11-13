"""Exercise every intake refusal against the demo account and record the ledger row.

The milestone asks for a ledger status for each failure case and proof that the
blocked cases create no order. Both are checked here:

* Every case runs through the same functions the live listener calls -
  :func:`process_once` for intake, then :meth:`ExecutionEngine.execute` for the
  two guard cases - and the status is read back from the ledger, not from the
  return value. A refusal that is decided but not persisted is not a refusal.
* The broker is wrapped so that ``place_market``, ``modify_position`` and
  ``close_quantity`` raise. If a guard ever fails to fire, the case raises and
  the script fails; it cannot quietly submit. The account is also counted
  before and after, so the claim "no order" is measured, not asserted.

Quotes are controlled per case, because a live demo feed will not produce a
crossed book or a sixty-second-old tick on request. The quote is an input to the
pipeline; substituting it changes only the input, not the code under test. The
signals are synthetic and labelled as such.

    python scripts/probe_intake_cases.py
    python scripts/probe_intake_cases.py --db /tmp/intake.sqlite --keep
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from rade.execution.engine import ExecutionEngine
from rade.execution.gateway import tradelocker_gateway_from_env
from rade.execution.guards import RiskLimits
from rade.observer.mirror_pipeline import (
    STATUS_BLOCKED,
    STATUS_MANUAL_REVIEW,
    STATUS_STALE,
    InstrumentSpec,
    MarketQuote,
    MessageEnvelope,
    RiskConfig,
    SQLiteIntentLedger,
    process_once,
)
from rade.observer.notify import RecordingNotifier

#: Distinct ids so one ledger can hold every case without collisions.
_BASE_ID = 990_000


class NoWriteGateway:
    """A broker that can be read but not written.

    Delegates every read to the real demo gateway and refuses the three calls
    that change the account. Used only for the guard cases, where the expected
    outcome is a refusal before submission.
    """

    def __init__(self, inner, log=print):
        self._inner = inner
        self._log = log

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def place_market(self, **_kwargs):
        self._log("  !! a guard did not fire: place_market was reached")
        raise AssertionError("the guard did not fire; submission was attempted")

    def modify_position(self, **_kwargs):
        self._log("  !! a guard did not fire: modify_position was reached")
        raise AssertionError("the guard did not fire; a stop move was attempted")

    def close_quantity(self, **_kwargs):
        self._log("  !! a guard did not fire: close_quantity was reached")
        raise AssertionError("the guard did not fire; a close was attempted")


def _text(entry: float, *, side: str = "buy", stop: float | None = None,
          targets: tuple[float, ...] | None = None) -> str:
    """A signal in the trader's format, priced off a given entry."""
    if stop is None:
        stop = entry - 9 if side == "buy" else entry + 9
    if targets is None:
        step = 1 if side == "buy" else -1
        targets = (entry + step, entry + 2 * step, entry + 3 * step)
    lines = [f"XAUUSD {side.upper()} NOW {entry:.2f}", f"Sl : {stop:.2f}"]
    lines += [f"Tp : {t:.2f}" for t in targets]
    return "\n".join(lines)


def _envelope(text: str, mid: int, *, age_s: float = 0.0,
              revision: int = 0) -> MessageEnvelope:
    now = datetime.now(timezone.utc)
    return MessageEnvelope("@probe", _BASE_ID + mid, revision,
                           now - timedelta(seconds=age_s), now, text)


def _instrument(gateway) -> InstrumentSpec:
    spec = gateway.spec()
    return InstrumentSpec(spec.symbol, spec.contract_units_per_lot, spec.lot_step,
                          spec.min_lot, spec.max_lot, units_confirmed=spec.verified)


def _risk(equity: float, *, quote_age: float = 5.0) -> RiskConfig:
    """Sizing config. The equity is read once, at startup, and passed in.

    Reading it per case would add a broker round-trip to every case for a number
    the probe does not vary.
    """
    return RiskConfig(equity, 0.0003, quote_age, signal_max_age_seconds=120.0)


def _read_back(db: Path) -> dict:
    """The executed-trade row, for the cases that reach the engine."""
    ledger = SQLiteIntentLedger(db)
    trades = ledger.list_trades()
    trade = trades[-1] if trades else {}
    return {
        "state": trade.get("state"),
        "broker_order_id": trade.get("broker_order_id"),
        "broker_position_id": trade.get("broker_position_id"),
        "note": trade.get("note"),
        "size": trade.get("size"),
    }


def _read_event(db: Path, event_key: str) -> dict:
    """The intake row, for the cases the engine never sees.

    An intake refusal is stored in ``mirror_events`` - it is a record of a
    message, not of a trade. Reading ``mirror_trades`` for these cases finds
    nothing, which is correct and is why the two readers are separate.
    """
    # ``with sqlite3.connect(...)`` is a transaction context, not a closer: the
    # handle stays open and Windows then refuses to delete the file. Close it
    # explicitly.
    con = sqlite3.connect(db)
    try:
        con.row_factory = sqlite3.Row
        row = con.execute(
            "SELECT status, reason, intent_json, signal_json FROM mirror_events "
            "WHERE event_key=?", (event_key,)).fetchone()
    finally:
        con.close()
    if row is None:
        return {"state": None, "reason": None, "has_intent": False}
    intent = json.loads(row["intent_json"]) if row["intent_json"] else None
    return {
        "state": row["status"],
        "reason": row["reason"],
        "has_intent": intent is not None,
        "size": (intent or {}).get("size"),
    }


def _run_intake(name: str, *, db: Path, gateway, envelope, quote, risk,
                expected: str, expect_in_reason: str = "") -> dict:
    """One intake case, read back from the ledger."""
    result = process_once(envelope, ledger=SQLiteIntentLedger(db),
                          instrument=_instrument(gateway), risk=risk, quote=quote)
    row = _read_event(db, result.event_key)
    problems = []
    if row["state"] != expected:
        problems.append(f"ledger state is {row['state']!r}, expected {expected!r}")
    if result.status != expected:
        problems.append(f"returned status is {result.status!r}, expected {expected!r}")
    if expect_in_reason and expect_in_reason not in (result.reason or ""):
        problems.append(f"reason does not mention {expect_in_reason!r}: {result.reason!r}")
    if row["has_intent"]:
        problems.append("a refused case still recorded an order intent")
    return {
        "case": name, "expected": expected, "status": result.status,
        "reason": result.reason, "ledger": row,
        "signal_text": envelope.text, "quote": None if quote is None else {
            "bid": quote.bid, "ask": quote.ask,
            "quoted_at": quote.quoted_at.isoformat(),
            "age_s": round((envelope.received_at - quote.quoted_at).total_seconds(), 1),
        },
        "problems": problems,
    }


def _run_guard(name: str, *, db: Path, gateway, envelope, quote, risk,
               limits: RiskLimits, expected: str = STATUS_BLOCKED) -> dict:
    """One pre-trade guard case: intake passes, the engine must refuse."""
    ledger = SQLiteIntentLedger(db)
    notifier = RecordingNotifier()
    engine = ExecutionEngine(broker=NoWriteGateway(gateway), ledger=ledger,
                             notifier=notifier, log=lambda line: None,
                             limits=limits, duplicate_window_s=0)
    result = process_once(envelope, ledger=ledger, instrument=_instrument(gateway),
                          risk=risk, quote=quote)
    if result.status != "paper_intent":
        return {"case": name, "expected": expected, "status": result.status,
                "reason": f"intake refused before the guard: {result.reason}",
                "ledger": _read_back(db), "problems": ["intake refused; guard untested"]}
    outcome = engine.execute(result, envelope)
    row = _read_back(db)
    checks = ((engine.guard_state() or {}).get("last_verdict") or {}).get("checks")
    problems = []
    if not outcome.get("guard"):
        problems.append("the guard did not refuse")
    if row["state"] != expected:
        problems.append(f"ledger state is {row['state']!r}, expected {expected!r}")
    if row["broker_order_id"] or row["broker_position_id"]:
        problems.append("the refused case still holds a broker id")
    return {
        "case": name, "expected": expected, "status": outcome.get("state"),
        "reason": outcome.get("reason"), "ledger": row, "checks": checks,
        "notifications": list(notifier.sent),
        "signal_text": envelope.text, "problems": problems,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="")
    parser.add_argument("--keep", action="store_true",
                        help="keep the throwaway ledger for inspection")
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
    equity = gateway.equity()
    quote = gateway.quote()
    print(f"live quote: bid {quote.bid} ask {quote.ask}")
    before_positions = len(gateway.positions())
    before_orders = len(gateway.orders())
    print(f"before: {before_positions} positions, {before_orders} open orders\n")

    base_db = Path(args.db) if args.db else Path(tempfile.gettempdir()) / (
        f"rade-intake-cases-{os.getpid()}.sqlite")
    if base_db.exists():
        base_db.unlink()

    def fresh(tag: str) -> Path:
        path = base_db.with_name(f"{base_db.stem}-{tag}{base_db.suffix}")
        if path.exists():
            path.unlink()
        return path

    # A quote ages. One fetched at startup is already past a five-second budget
    # by the time the later cases run, which would make every one of them a
    # stale-quote case. Each case therefore fetches its own, and only the case
    # that is *about* quote age overrides the timestamp.
    def live_quote() -> MarketQuote:
        fresh = gateway.quote()
        return MarketQuote(fresh.bid, fresh.ask, fresh.quoted_at)

    # The pause switch is set on its own ledger *before* the case runs, because
    # the case list is evaluated top to bottom and a switch thrown afterwards
    # would only be read by a later run.
    paused_db = fresh("paused")
    SQLiteIntentLedger(paused_db).set_paused(
        paused=True, reason="probe: pause switch", changed_by="probe")

    _ids = iter(range(1, 100))

    def intake(name: str, tag: str, text_of, *, expected: str,
               quote_of=None, risk_age: float = 5.0, age_s: float = 0.0,
               revision: int = 0, expect_in_reason: str = "") -> dict:
        """One quote per case: a second fetch is a second chance to age out."""
        q = live_quote()
        return _run_intake(
            name, db=fresh(tag), gateway=gateway,
            envelope=_envelope(text_of(q), next(_ids), age_s=age_s, revision=revision),
            quote=quote_of(q) if quote_of else q,
            risk=_risk(equity, quote_age=risk_age), expected=expected,
            expect_in_reason=expect_in_reason)

    def guard(name: str, tag: str, text_of, *, limits, db: Path | None = None,
              expected: str = STATUS_BLOCKED) -> dict:
        q = live_quote()
        return _run_guard(
            name, db=db or fresh(tag), gateway=gateway,
            envelope=_envelope(text_of(q), next(_ids)), quote=q,
            risk=_risk(equity), limits=limits, expected=expected)

    _limits = lambda **kw: RiskLimits(  # noqa: E731 - a table, not a function
        max_open_positions=3, daily_loss_fraction=0.02, max_drawdown_fraction=0.05,
        max_spread=kw.get("max_spread", 5.0), max_slippage=kw.get("max_slippage", 5.0))

    cases = [
        # --- intake: the instruction itself -------------------------------
        intake("delayed signal (recovered 10 min after it was posted)", "delayed",
               lambda q: _text(q.bid), age_s=600, expected=STATUS_STALE,
               expect_in_reason="posted"),
        intake("stale quote (60 s old, 5 s budget)", "stalequote",
               lambda q: _text(q.bid), expected=STATUS_BLOCKED,
               quote_of=lambda q: MarketQuote(q.bid, q.ask,
                                              datetime.now(timezone.utc) - timedelta(seconds=60)),
               risk_age=5.0, expect_in_reason="stale"),
        intake("crossed book (ask below bid)", "crossed",
               lambda q: _text(q.bid), expected=STATUS_BLOCKED,
               quote_of=lambda q: MarketQuote(q.bid + 20, q.ask - 20,
                                              datetime.now(timezone.utc)),
               expect_in_reason="invalid"),
        intake("quote timestamp from the future", "future",
               lambda q: _text(q.bid), expected=STATUS_BLOCKED,
               quote_of=lambda q: MarketQuote(q.bid, q.ask,
                                              datetime.now(timezone.utc) + timedelta(minutes=10)),
               expect_in_reason="future"),
        intake("market already through the published stop", "through",
               lambda q: _text(q.bid + 30), expected=STATUS_BLOCKED,
               expect_in_reason="crossed"),
        intake("edited signal (revision 1)", "edited",
               lambda q: _text(q.bid), revision=1, expected=STATUS_MANUAL_REVIEW,
               expect_in_reason="edited"),
        intake("malformed signal (no stop)", "malformed",
               lambda q: f"XAUUSD BUY NOW {q.bid:.2f}\nTp : {q.bid + 2:.2f}",
               expected=STATUS_MANUAL_REVIEW),
        # --- pre-trade guards: the account and the market ------------------
        guard("excessive spread (limit set below the live spread)", "spread",
              lambda q: _text(q.bid), limits=_limits(max_spread=0.0001)),
        guard("excessive slippage (entry 3.00 from the market, limit 0.01)", "slippage",
              lambda q: _text(q.ask + 3.0), limits=_limits(max_slippage=0.01)),
        guard("pause enabled (switch set before the signal)", "paused",
              lambda q: _text(q.bid), limits=_limits(), db=paused_db),
    ]

    failures: list[str] = []
    for case in cases:
        print(f"== {case['case']}")
        print(f"   expected: {case['expected']}")
        print(f"   status:   {case['status']}")
        print(f"   reason:   {case['reason']}")
        print(f"   ledger:   state={case['ledger'].get('state')!r} "
              f"order={case['ledger'].get('broker_order_id')!r} "
              f"position={case['ledger'].get('broker_position_id')!r} "
              f"intent={case['ledger'].get('has_intent', 'n/a')}")
        if case.get("checks"):
            for check in case["checks"]:
                print(f"   check:    {check}")
        for problem in case["problems"]:
            print(f"   PROBLEM:  {problem}")
            failures.append(f"{case['case']}: {problem}")
        print()

    after_positions = len(gateway.positions())
    after_orders = len(gateway.orders())
    print(f"after: {after_positions} positions, {after_orders} open orders "
          f"(before: {before_positions}, {before_orders})")
    if after_positions != before_positions or after_orders != before_orders:
        failures.append("the account changed during the probe")

    evidence = Path("docs/evidence") / (
        "intake-cases-"
        f"{datetime.now(timezone.utc).strftime('%Y-%m-%dT%H-%M-%SZ')}.json")
    evidence.parent.mkdir(parents=True, exist_ok=True)
    evidence.write_text(json.dumps({
        "read_at": datetime.now(timezone.utc).isoformat(),
        "environment": gateway.environment,
        "live_quote": {"bid": quote.bid, "ask": quote.ask},
        "account_before": {"positions": before_positions, "open_orders": before_orders},
        "account_after": {"positions": after_positions, "open_orders": after_orders},
        "cases": cases,
        "failures": failures,
    }, indent=2, default=str), encoding="utf-8")
    print(f"evidence: {evidence}")

    if not args.keep:
        kept = []
        for path in base_db.parent.glob(f"{base_db.stem}*{base_db.suffix}"):
            try:
                path.unlink(missing_ok=True)
            except PermissionError:
                # A ledger handle still open somewhere in this process. Not a
                # finding; say where the file is rather than failing the run.
                kept.append(str(path))
        if kept:
            print(f"note: {len(kept)} throwaway ledger(s) could not be removed: "
                  f"{', '.join(kept)}")

    if failures:
        for failure in failures:
            print(f"FAIL {failure}")
        return 1
    print(f"\nall {len(cases)} cases refused with the expected ledger status; "
          "no order was submitted")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
