"""Dump what the mirror ledger recorded, as evidence, without touching it.

The end-to-end question is "did one real Telegram signal become one demo order,
get managed through its ladder, and close" - and the only trustworthy answer is
the ledger's own record of it: the message id it came from, the broker ids it
was given, and every transition in between.

This script opens the SQLite ledger read-only and writes a JSON file. It never
writes to the ledger, never talks to the broker, and never resubmits anything.
Run it against a ledger that is still being written to; SQLite in read-only mode
takes a consistent snapshot per query, and the exporter tolerates a trade that
gains a transition between two queries.

    python scripts/export_mirror_evidence.py
    python scripts/export_mirror_evidence.py --db data/e2e_mirror.sqlite --out x.json

Exit code is 0 whenever the dump was written, including when the ledger is
empty: "nothing was copied" is a finding, not a failure.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_DB = "data/e2e_mirror.sqlite"


def _read_only(path: Path) -> sqlite3.Connection:
    """Open the ledger without the ability to change it."""
    uri = f"file:{path.as_posix()}?mode=ro"
    con = sqlite3.connect(uri, uri=True, timeout=10.0)
    con.row_factory = sqlite3.Row
    return con


def _rows(con: sqlite3.Connection, sql: str, args: tuple = ()) -> list[dict]:
    try:
        return [dict(row) for row in con.execute(sql, args).fetchall()]
    except sqlite3.Error as exc:
        # A table that does not exist yet is a known absence in an old ledger,
        # not a reason to fail the whole dump.
        return [{"error": f"{type(exc).__name__}: {exc}"}]


def collect(path: Path) -> dict:
    con = _read_only(path)
    try:
        trades = _rows(con, "select * from mirror_trades order by rowid")
        for trade in trades:
            trade_id = trade.get("trade_id")
            if trade_id:
                trade["transitions"] = _rows(
                    con,
                    "select * from mirror_transitions where trade_id = ? "
                    "order by rowid",
                    (trade_id,),
                )
        return {
            "ledger": str(path),
            "read_at": datetime.now(timezone.utc).isoformat(),
            "counts": {
                "trades": len(trades),
                "transitions": sum(len(t.get("transitions") or [])
                                   for t in trades),
            },
            "risk_state": (_rows(con, "select * from mirror_risk_state")
                           or [None])[0],
            "control": (_rows(con, "select * from mirror_control") or [None])[0],
            "trades": trades,
            "events": _rows(con, "select * from mirror_events order by rowid"),
            "cursor": _rows(con, "select * from mirror_cursor"),
        }
    finally:
        con.close()


def summarise(dump: dict) -> str:
    """A few lines a human can read without opening the JSON."""
    lines = [f"ledger: {dump['ledger']}",
             f"trades: {dump['counts']['trades']}, "
             f"transitions: {dump['counts']['transitions']}"]
    control = dump.get("control") or {}
    # The stored column is execution_paused; the adapter names it paused.
    paused = control.get("paused", control.get("execution_paused"))
    lines.append(f"paused: {paused} "
                 f"({control.get('reason') or 'no reason recorded'})")
    for trade in dump["trades"]:
        lines.append(
            f"- {trade.get('trade_id')} {trade.get('side')} "
            f"{trade.get('size')} @ {trade.get('entry')} "
            f"state={trade.get('state')} "
            f"order={trade.get('broker_order_id')} "
            f"position={trade.get('broker_position_id')} "
            f"msg={trade.get('message_id')} "
            f"({len(trade.get('transitions') or [])} transitions)"
        )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=os.getenv("MIRROR_SQLITE_PATH", DEFAULT_DB))
    parser.add_argument("--out", default="")
    args = parser.parse_args()

    path = Path(args.db)
    if not path.exists():
        print(f"no ledger at {path}; nothing has been recorded there")
        return 0

    dump = collect(path)
    out = Path(args.out) if args.out else Path(
        f"docs/evidence/mirror-ledger-"
        f"{datetime.now(timezone.utc).strftime('%Y-%m-%dT%H-%M-%SZ')}.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(dump, indent=2, default=str), encoding="utf-8")
    print(summarise(dump))
    print(f"\nwritten: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
