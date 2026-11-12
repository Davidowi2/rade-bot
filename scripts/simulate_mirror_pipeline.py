"""Run the paper mirror against a real archived Telegram message, then against
the failure modes that must not silently become orders.

    python scripts/simulate_mirror_pipeline.py --message-id 18048

What this does NOT prove: live push delivery. Every message here comes from the
local research database (``data/rade.db``), which was populated by the fetch
path. No Telegram connection is opened and no broker call is made.

Exit code 0 when every scenario matches its expectation, 1 otherwise.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from rade.observer.mirror_pipeline import (  # noqa: E402
    InstrumentSpec,
    MarketQuote,
    MessageEnvelope,
    RiskConfig,
    SQLiteIntentLedger,
    evaluate_message,
    parse_message_signal,
    process_once,
    text_hash,
)

INSTRUMENT = InstrumentSpec("XAUUSD", 100.0, 0.01, 0.01, 50.0)
RISK = RiskConfig(equity=10_000.0, risk_fraction=0.005, quote_max_age_seconds=2.0)


def load_message(db: str, message_id: int) -> tuple[str, int, datetime, str]:
    con = sqlite3.connect(db)
    row = con.execute(
        "SELECT channel, message_id, posted_at, text FROM raw_telegram_messages WHERE message_id=?",
        (message_id,),
    ).fetchone()
    con.close()
    if row is None:
        raise SystemExit(f"message {message_id} is not present in {db}")
    channel, mid, posted_at, text = row
    posted = datetime.fromisoformat(posted_at)
    if posted.tzinfo is None:
        posted = posted.replace(tzinfo=timezone.utc)
    return channel, int(mid), posted, text or ""


def envelope(channel, mid, posted, text, *, revision=0, received_at=None,
             source="stored_telegram_history"):
    return MessageEnvelope(
        channel=channel, message_id=mid, revision=revision, posted_at=posted,
        received_at=received_at or datetime.now(timezone.utc), text=text, source=source,
    )


def fmt(price: float) -> str:
    """Render a price the way the channel writes it (3925, not 3925.0).

    Without this the "tampered" and "edited" bodies were byte-identical to the
    original, so those two scenarios passed without testing anything.
    """
    return str(int(price)) if float(price).is_integer() else str(price)


def quote_now(bid=3915.0, ask=3915.0):
    return MarketQuote(bid, ask, datetime.now(timezone.utc))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--message-id", type=int, default=18048,
                    help="Telegram message id in the local database")
    ap.add_argument("--db", default="data/rade.db")
    ap.add_argument("--json-out", default=None,
                    help="write the report here as well as printing it")
    args = ap.parse_args()

    channel, mid, posted, text = load_message(args.db, args.message_id)
    parsed = parse_message_signal(text)
    if parsed is None:
        raise SystemExit(f"message {mid} is not a parseable signal: {text[:120]!r}")

    scenarios: list[dict] = []

    def record(name, expected, result, extra=None):
        ok = result.status == expected
        row = {
            "scenario": name,
            "expected": expected,
            "actual": result.status,
            "verdict": "PASS" if ok else "FAIL",
            "reason": result.reason,
        }
        if result.order_intent is not None:
            row["order_state"] = result.order_intent.get("order_state")
            row["filled"] = result.order_intent.get("filled")
            row["size_lots"] = result.order_intent.get("size_lots")
        if extra:
            row.update(extra)
        scenarios.append(row)
        return row

    with tempfile.TemporaryDirectory() as tmp:
        ledger_path = Path(tmp) / "simulation.sqlite"

        # 1. the real message, priced from a quote taken at evaluation time
        ledger = SQLiteIntentLedger(ledger_path)
        first = process_once(envelope(channel, mid, posted, text), ledger=ledger,
                             instrument=INSTRUMENT, risk=RISK, quote=quote_now())
        record("real archived signal", "paper_intent", first)

        # 2. the same message delivered twice
        second = process_once(envelope(channel, mid, posted, text), ledger=ledger,
                              instrument=INSTRUMENT, risk=RISK, quote=quote_now())
        record("duplicate delivery", "duplicate", second)

        # 3. a restart: a new ledger object on the same database file
        restarted = process_once(envelope(channel, mid, posted, text),
                                 ledger=SQLiteIntentLedger(ledger_path),
                                 instrument=INSTRUMENT, risk=RISK, quote=quote_now())
        record("restart replay", "duplicate", restarted)

        # 4. the same id and revision with a different body
        tampered_text = text.replace(fmt(parsed["stop_loss"]), fmt(parsed["stop_loss"] + 5), 1)
        if tampered_text == text:
            raise SystemExit("scenario fixture is broken: the tampered body is unchanged")
        tampered = process_once(envelope(channel, mid, posted, tampered_text), ledger=ledger,
                                instrument=INSTRUMENT, risk=RISK, quote=quote_now())
        record("same revision, changed body", "conflict", tampered)

        # 5. an edit is stored as a new revision and never auto-applied
        edited_text = text.replace(fmt(parsed["take_profits"][-1]),
                                   fmt(parsed["take_profits"][-1] - 5), 1)
        if edited_text == text:
            raise SystemExit("scenario fixture is broken: the edited body is unchanged")
        edited = process_once(envelope(channel, mid, posted, edited_text, revision=1),
                              ledger=ledger, instrument=INSTRUMENT, risk=RISK,
                              quote=quote_now())
        record("edited signal", "manual_review", edited)

        # 6. malformed and non-signal traffic
        malformed = evaluate_message(envelope(channel, 99001, posted, "XAUUSD SELL NOW"),
                                     instrument=INSTRUMENT, risk=RISK, quote=quote_now())
        record("malformed call", "ignored", malformed)

        recap = evaluate_message(
            envelope(channel, 99002, posted, "XAUUSD BUY closed at TP2, +120 pips"),
            instrument=INSTRUMENT, risk=RISK)
        record("recap post", "ignored", recap)

        news = evaluate_message(envelope(channel, 99003, posted, "Gold rallies on CPI data"),
                                instrument=INSTRUMENT, risk=RISK)
        record("news post", "ignored", news)

        # 7. missing and stale quotes
        no_quote = evaluate_message(envelope(channel, 99004, posted, text),
                                    instrument=INSTRUMENT, risk=RISK, quote=None)
        record("missing quote", "blocked", no_quote)

        stale = evaluate_message(
            envelope(channel, 99005, posted, text), instrument=INSTRUMENT, risk=RISK,
            quote=MarketQuote(3915, 3915, datetime.now(timezone.utc) - timedelta(seconds=30)))
        record("stale quote", "blocked", stale)

        # 8. invalid account inputs
        bad_spec = evaluate_message(
            envelope(channel, 99006, posted, text),
            instrument=InstrumentSpec("XAUUSD", 0, 0.01, 0.01, 50), risk=RISK,
            quote=quote_now())
        record("missing contract spec", "blocked", bad_spec)

        over_cap = evaluate_message(
            envelope(channel, 99007, posted, text), instrument=INSTRUMENT,
            risk=RiskConfig(10_000, 0.02, 2.0), quote=quote_now())
        record("risk above the 0.5% cap", "blocked", over_cap)

        no_equity = evaluate_message(
            envelope(channel, 99008, posted, text), instrument=INSTRUMENT,
            risk=RiskConfig(0, 0.005, 2.0), quote=quote_now())
        record("missing equity", "blocked", no_equity)

        tiny = evaluate_message(
            envelope(channel, 99009, posted, text), instrument=INSTRUMENT,
            risk=RiskConfig(100, 0.005, 2.0), quote=quote_now())
        record("minimum lot exceeds risk budget", "skipped_minimum_lot", tiny)

        # 9. a limit call is never a fill
        limit_text = text.replace("NOW", "LIMIT", 1)
        limit = evaluate_message(envelope(channel, 99010, posted, limit_text),
                                 instrument=INSTRUMENT, risk=RISK, quote=None)
        row = record("limit call", "paper_intent", limit)
        if row["order_state"] != "pending_limit" or row["filled"] is not False:
            row["verdict"] = "FAIL"
            row["reason"] = "a limit call was not represented as pending/unfilled"

        # 10. a market call is never a fill either
        row = scenarios[0]
        if row.get("order_state") != "would_fill_market" or row.get("filled") is not False:
            row["verdict"] = "FAIL"
            row["reason"] = "a market call was not represented as unfilled"

    failed = [s for s in scenarios if s["verdict"] != "PASS"]
    report = {
        "mode": "simulation",
        "provenance": {
            "message_source": f"stored Telegram history, message {mid} in {args.db}",
            "channel": channel,
            "posted_at": posted.isoformat(),
            "text_sha256": text_hash(text),
            "quote": "synthetic, stamped at evaluation time; not a live broker quote",
            "equity": "synthetic 10,000 for sizing only",
            "contract_spec": "TradeLocker XAUUSD read-only response; still unconfirmed by a demo fill",
            "proves": "parser/validator/ledger behaviour",
            "does_not_prove": "live Telegram push delivery, broker execution, exit policy",
        },
        "scenarios": scenarios,
        "summary": {"total": len(scenarios), "passed": len(scenarios) - len(failed),
                    "failed": len(failed)},
    }
    print(json.dumps(report, indent=2))
    if args.json_out:
        Path(args.json_out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json_out).write_text(json.dumps(report, indent=2), encoding="utf-8")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
