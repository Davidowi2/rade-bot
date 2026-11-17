"""What does the first-target gate actually cost, and what does it save?

The gate refuses a signal when the market has already passed the first target
by the time the message is read. The argument for it is that the move the
signal existed for has happened, so copying it means risking the full stop to
reach what is left. The argument against it is the trade it refused this
afternoon: gold was already through TP1 and still ran to the final target.

Both are claims about the same population, so they can be settled by counting.
This walks every parseable signal in the archive, takes the market where it
actually was when the message was posted, and follows the published stop and
final target through the 1-minute bars until one of them is hit.

    python scripts/measure_tp1_gate.py --db data/rade.db

Output is in R: one R is the distance from the entry to the published stop, so
-1R is a full stop-out and the numbers do not depend on lot size.

Method and its limits are printed with the result. Read them before quoting it.
"""
from __future__ import annotations

import argparse
import bisect
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from rade.observer.mirror_pipeline import parse_message_signal  # noqa: E402

#: How long a copied trade is given to resolve. A prop account does not hold a
#: day-trade through a weekend, and a target that takes three days to reach is
#: not the trade the channel described.
MAX_HOLD_MINUTES = 24 * 60


def load_bars(db: Path) -> tuple[list[int], list[tuple[float, float, float, float]]]:
    """1-minute bars, as a sorted timestamp list and parallel OHLC tuples."""
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    rows = con.execute(
        "SELECT ts, open, high, low, close FROM bars "
        "WHERE timeframe='1m' AND instrument_id=1 ORDER BY ts"
    ).fetchall()
    con.close()
    stamps: list[int] = []
    bars: list[tuple[float, float, float, float]] = []
    for ts, o, h, l, c in rows:
        stamps.append(int(datetime.fromisoformat(ts).replace(tzinfo=timezone.utc)
                          .timestamp() // 60))
        bars.append((float(o), float(h), float(l), float(c)))
    return stamps, bars


def load_signals(db: Path) -> list[tuple[int, datetime, dict]]:
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    rows = con.execute(
        "SELECT message_id, posted_at, text FROM raw_telegram_messages "
        "ORDER BY message_id"
    ).fetchall()
    con.close()
    out = []
    for message_id, posted_at, text in rows:
        parsed = parse_message_signal(text or "")
        if not parsed or not parsed.get("entry_price") or not parsed.get("stop_loss"):
            continue
        if not parsed.get("take_profits"):
            continue
        posted = datetime.fromisoformat(posted_at)
        if posted.tzinfo is None:
            posted = posted.replace(tzinfo=timezone.utc)
        out.append((int(message_id), posted, parsed))
    return out


def resolve(entry: float, stop: float, final: float, side: str,
            bars, start: int, limit: int = MAX_HOLD_MINUTES) -> tuple[float, str]:
    """Follow one trade to its stop, its final target, or the holding limit.

    When a single bar contains both the stop and the target, the stop is
    assumed to have been hit first. That is the pessimistic reading and it is
    the only one that cannot flatter the result.
    """
    risk = abs(entry - stop)
    if risk <= 0:
        return 0.0, "no risk"
    for index in range(start, min(start + limit, len(bars))):
        _o, high, low, close = bars[index]
        if side == "sell":
            if high >= stop:
                return -1.0, "stop"
            if low <= final:
                return (entry - final) / risk, "target"
        else:
            if low <= stop:
                return -1.0, "stop"
            if high >= final:
                return (final - entry) / risk, "target"
    last = bars[min(start + limit, len(bars)) - 1][3]
    return ((entry - last) if side == "sell" else (last - entry)) / risk, "timeout"


def summarise(name: str, results: list[tuple[float, str]]) -> dict:
    if not results:
        return {"group": name, "signals": 0}
    rs = [r for r, _ in results]
    wins = sum(1 for r in rs if r > 0)
    return {
        "group": name,
        "signals": len(rs),
        "win_rate": round(wins / len(rs), 3),
        "mean_r": round(sum(rs) / len(rs), 3),
        "total_r": round(sum(rs), 1),
        "stops": sum(1 for _, why in results if why == "stop"),
        "targets": sum(1 for _, why in results if why == "target"),
        "timeouts": sum(1 for _, why in results if why == "timeout"),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default="data/rade.db")
    ap.add_argument("--json-out", default=None)
    args = ap.parse_args()

    db = Path(args.db)
    if not db.exists():
        raise SystemExit(f"{db} not found; the archive lives in the main checkout")

    stamps, bars = load_bars(db)
    if not stamps:
        raise SystemExit("no 1-minute bars in this database")
    first, last = stamps[0], stamps[-1]

    past_first: list[tuple[float, str]] = []
    before_first: list[tuple[float, str]] = []
    drift: list[float] = []
    skipped = {"before the bar archive": 0, "no bar at that minute": 0,
               "stop already crossed": 0, "targets on the wrong side": 0}

    for message_id, posted, parsed in load_signals(db):
        minute = int(posted.timestamp() // 60)
        if minute < first or minute > last:
            skipped["before the bar archive"] += 1
            continue
        index = bisect.bisect_left(stamps, minute)
        if index >= len(stamps) or stamps[index] - minute > 5:
            skipped["no bar at that minute"] += 1
            continue

        entry = bars[index][3]                       # the close of that minute
        side = parsed["direction"]
        stop = float(parsed["stop_loss"])
        targets = [float(t) for t in parsed["take_profits"]]
        first_target = max(targets) if side == "sell" else min(targets)
        final = min(targets) if side == "sell" else max(targets)

        # How far the market already is from the price the channel published,
        # measured in the trade's favour. This is the number that explains the
        # gate: if it is routinely large, the channel posts after the move.
        drift.append((float(parsed["entry_price"]) - entry) if side == "sell"
                     else (entry - float(parsed["entry_price"])))

        if (side == "sell" and stop <= entry) or (side == "buy" and stop >= entry):
            skipped["stop already crossed"] += 1
            continue
        if (side == "sell" and final >= entry) or (side == "buy" and final <= entry):
            # The whole trade is behind the market; there is nothing to copy.
            skipped["targets on the wrong side"] += 1
            continue

        outcome = resolve(entry, stop, final, side, bars, index)
        passed = entry <= first_target if side == "sell" else entry >= first_target
        (past_first if passed else before_first).append(outcome)

    drift.sort()

    def percentile(p: float) -> float:
        return round(drift[int(p * (len(drift) - 1))], 2) if drift else 0.0

    report = {
        "source": str(db),
        "bar_coverage": {
            "from": datetime.fromtimestamp(first * 60, timezone.utc).isoformat(),
            "to": datetime.fromtimestamp(last * 60, timezone.utc).isoformat(),
        },
        "posting_lag": {
            "signals": len(drift),
            "median_points_past_entry": percentile(0.5),
            "p75_points_past_entry": percentile(0.75),
            "p90_points_past_entry": percentile(0.90),
            "share_already_past_entry": round(
                sum(1 for d in drift if d > 0) / len(drift), 3) if drift else 0.0,
            "share_past_by_5_or_more": round(
                sum(1 for d in drift if d >= 5) / len(drift), 3) if drift else 0.0,
        },
        "groups": [summarise("market already past TP1 (the gate refuses these)",
                             past_first),
                   summarise("market before TP1 (the gate allows these)",
                             before_first)],
        "skipped": skipped,
        "method": {
            "entry": "close of the 1-minute bar containing the post time",
            "risk": "distance from that entry to the published stop (1R)",
            "exit": f"stop, final target, or {MAX_HOLD_MINUTES // 60}h, whichever "
                    "comes first; a bar containing both is scored as a stop",
            "does_not_model": "spread, commission, slippage, or the bot's own "
                              "resize to the risk budget",
        },
    }
    print(json.dumps(report, indent=2))
    if args.json_out:
        out = Path(args.json_out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
