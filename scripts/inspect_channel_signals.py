"""List the channel's recent messages and say which ones the intake would act on.

Read-only: it opens the Telegram session, reads, and exits. Nothing is sent to
a broker and nothing is written to the ledger.

This exists because "test a real signal" needs a real signal, and the only way
to know which recent message is one is to look. It also reports how many
messages sit after the chosen one, so a run can be pointed at the last
executable signal and not at a burst of them.

    python scripts/inspect_channel_signals.py --limit 40
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dotenv import load_dotenv  # noqa: E402

load_dotenv()

from rade.ingestion.telegram.client import make_client  # noqa: E402
from rade.observer.mirror_pipeline import (  # noqa: E402
    MessageEnvelope,
    evaluate_message,
    count_entry_lines,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=40)
    parser.add_argument("--channel", default=os.getenv("TELEGRAM_CHANNEL", ""))
    parser.add_argument("--out", default=None,
                        help="write the messages to this JSON file")
    args = parser.parse_args(argv)

    channel = (args.channel or "").strip()
    if not channel:
        print("TELEGRAM_CHANNEL is not set")
        return 2

    client = make_client()
    rows: list[dict] = []
    with client:
        for message in client.iter_messages(channel, limit=args.limit):
            text = message.message or ""
            rows.append({
                "id": message.id,
                "date": message.date.astimezone(timezone.utc).isoformat(),
                "revision": 0,
                "entry_lines": count_entry_lines(text),
                "text": text,
            })
    rows.sort(key=lambda r: r["id"])

    print(f"channel {channel}: {len(rows)} messages, "
          f"ids {rows[0]['id']}..{rows[-1]['id']}" if rows else "no messages")
    for row in rows:
        flag = "SIGNAL" if row["entry_lines"] == 1 else (
            f"{row['entry_lines']} entries" if row["entry_lines"] else "")
        first = row["text"].strip().splitlines()[0][:70] if row["text"].strip() else ""
        print(f"  {row['id']:>7} {row['date'][:19]} {flag:>10}  {first}")

    # Which of them are executable market calls, per the real grammar.
    #
    # The quote is set to the signal's own published entry, so this answers
    # "does the grammar accept this message" and nothing about whether the
    # market is anywhere near it. A live run uses the real quote, and the
    # slippage guard decides then.
    from rade.observer.mirror_pipeline import InstrumentSpec, MarketQuote, RiskConfig

    instrument = InstrumentSpec("XAUUSD", 100.0, 0.01, 0.01, 50.0,
                                units_confirmed=True)
    executable = []
    for row in rows:
        if row["entry_lines"] != 1:
            continue
        envelope = MessageEnvelope(
            channel=channel, message_id=row["id"], revision=0,
            posted_at=datetime.fromisoformat(row["date"]),
            received_at=datetime.now(timezone.utc), text=row["text"],
            source="inspect")
        first_pass = evaluate_message(
            envelope, instrument=instrument,
            risk=RiskConfig(10_000.0, 0.005, 86_400.0), quote=None)
        signal = first_pass.signal or {}
        row["status"] = first_pass.status
        row["reason"] = first_pass.reason
        row["entry_type"] = signal.get("entry_type")
        # Without a quote the pipeline returns the parser's own dict, which
        # uses the parser's key names rather than the enriched signal's.
        row["side"] = signal.get("direction") or signal.get("side")
        row["entry_price"] = signal.get("entry_price") or signal.get("published_entry")
        if signal.get("entry_price"):
            price = float(signal["entry_price"])
            quoted = evaluate_message(
                envelope, instrument=instrument,
                risk=RiskConfig(10_000.0, 0.005, 86_400.0),
                quote=MarketQuote(price, price, datetime.now(timezone.utc)))
            row["parse_status"] = quoted.status
            row["parse_reason"] = quoted.reason
            if (quoted.status == "paper_intent"
                    and signal.get("entry_type") == "market"):
                executable.append(row)
        row["text_sha256"] = __import__("hashlib").sha256(
            row["text"].encode("utf-8")).hexdigest()

    print("\nexecutable NOW/market signals (grammar only, priced at their own entry):")
    for row in executable:
        print(f"  {row['id']}  {row['side']}  "
              f"{row['text'].strip().splitlines()[0]}")
    if not executable:
        print("  none in this window")

    # Identical text under different ids is the same idea posted twice.
    seen: dict[str, list[int]] = {}
    for row in executable:
        seen.setdefault(row["text_sha256"], []).append(row["id"])
    repeats = {h: ids for h, ids in seen.items() if len(ids) > 1}
    if repeats:
        print("\nthe same call posted more than once (copied once, not twice):")
        for ids in repeats.values():
            print(f"  messages {ids}")

    if args.out:
        Path(args.out).write_text(json.dumps(rows, indent=2), encoding="utf-8")
        print(f"\nwrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
