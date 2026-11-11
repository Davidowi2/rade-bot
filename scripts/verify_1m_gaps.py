"""Verify the merged 1-minute series for unresolved gaps.

    python scripts/verify_1m_gaps.py --out docs/evidence/1m-gaps.json

The plan requires the merged 1m series to be gap-verified before use, and any
setup whose evaluation window contains an **unresolved** gap to be excluded and
counted (``docs/14-redirect-plan.md`` §3).

"Unresolved" is the operative word. XAUUSD does not trade continuously, so the
series legitimately contains:

* **weekend closures** - Friday ~21:00 UTC to Sunday ~22:00 UTC, ~49h;
* **a daily maintenance break** - roughly an hour, once a day;
* **holiday closures** - occasional multi-day gaps.

None of those are missing data. A gap is only *unresolved* if it is too long to
be a scheduled closure but too short to be one - i.e. bars are missing while
the market was open. This script classifies every gap against the observed
closure pattern and reports what it cannot explain.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter
from datetime import datetime, timezone

import pandas as pd

# A gap is treated as a scheduled closure if it is at least this long. The
# daily break is ~60 min and weekends are ~49h, so anything in between is
# either a holiday or missing data; anything shorter is missing data.
CLOSURE_MIN = 55.0
"""Minutes. Below this, a gap is intraday and must be explained."""


def load(con: sqlite3.Connection, timeframe: str = "1m") -> pd.Series:
    df = pd.read_sql(
        f"SELECT ts FROM bars WHERE timeframe='{timeframe}' ORDER BY ts", con
    )
    return pd.to_datetime(df["ts"], utc=True)


def classify(ts: pd.Series) -> dict:
    d = ts.diff().dt.total_seconds().div(60).dropna()
    if d.empty:
        return {"n": 0}

    intraday = d[d <= CLOSURE_MIN]
    closure = d[d > CLOSURE_MIN]

    # the modal intraday step is the bar interval; anything above it is a hole
    step = float(intraday.median()) if len(intraday) else 1.0
    holes = d[(d > step * 1.5) & (d <= CLOSURE_MIN)]

    # which minutes of the day do the holes start at?
    hole_starts = Counter()
    for i in range(1, len(ts)):
        gap = (ts.iloc[i] - ts.iloc[i - 1]).total_seconds() / 60.0
        if step * 1.5 < gap <= CLOSURE_MIN:
            hole_starts[f"{ts.iloc[i - 1].hour:02d}:{ts.iloc[i - 1].minute:02d}"] += 1

    return {
        "n": int(len(ts)),
        "first": str(ts.min()),
        "last": str(ts.max()),
        "step_min": step,
        "expected_bars_if_continuous": int(
            (ts.max() - ts.min()).total_seconds() / 60 / step
        ),
        "intraday_gaps": int(len(intraday[intraday > step])),
        "intraday_gaps_le_1m": int((intraday <= 1.0).sum()),
        "holes_under_55min": int(len(holes)),
        "largest_hole_min": float(holes.max()) if len(holes) else 0.0,
        "hole_start_minutes_top": hole_starts.most_common(8),
        "closures_55min_to_2h": int(((closure >= 55) & (closure <= 120)).sum()),
        "closures_over_2h": int((closure > 120).sum()),
        "largest_closure_h": float(closure.max() / 60) if len(closure) else 0.0,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/rade.db")
    ap.add_argument("--timeframe", default="1m")
    ap.add_argument("--out", default="docs/evidence/1m-gaps.json")
    args = ap.parse_args()

    con = sqlite3.connect(args.db)
    ts = load(con, args.timeframe)
    con.close()

    if ts.empty:
        print(f"ERROR: no {args.timeframe} bars in {args.db}")
        return 1

    info = classify(ts)
    print(f"timeframe            : {args.timeframe}")
    print(f"bars                 : {info['n']:,}")
    print(f"span                 : {info['first']} -> {info['last']}")
    print(f"bar step             : {info['step_min']} min")
    print(f"expected if continuous: {info['expected_bars_if_continuous']:,}")
    print(f"coverage             : {info['n'] / info['expected_bars_if_continuous']:.1%}")
    print()
    print(f"gaps of exactly one step or less : {info['intraday_gaps_le_1m']:,}")
    print(f"gaps under {CLOSURE_MIN:.0f} min (candidate holes)  : {info['holes_under_55min']:,}")
    print(f"largest such hole                : {info['largest_hole_min']:.0f} min")
    print(f"closures 55min-2h                : {info['closures_55min_to_2h']:,}")
    print(f"closures over 2h                 : {info['closures_over_2h']:,}  "
          f"(max {info['largest_closure_h']:.1f}h)")
    print()
    print("hole start times (top):")
    for t, c in info["hole_start_minutes_top"]:
        print(f"  {t}  x{c}")

    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(
            {"generated_at": datetime.now(timezone.utc).isoformat(),
             "closure_threshold_min": CLOSURE_MIN,
             "verification": info},
            fh, indent=2,
        )
    print(f"\nwrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
