"""How often does the source channel actually post?

The reception pass criterion is a *p95* over delivery latencies, and a
percentile needs a sample. Three 90-second probes returned zero messages, which
is not evidence that reception is broken - it is evidence that the window was
too short for this channel. This script replaces that guess with the measured
arrival rate, so a probe window can be sized before it is run rather than after
it comes back empty.

Reads only the local database. No network, no Telegram client.

Usage
-----
    python scripts/channel_activity.py
    python scripts/channel_activity.py --days 30 --at-hour 12 --window-minutes 240
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import statistics
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

SAMPLES_FOR_P95 = 20
"""Below ~20 samples a p95 is the second-largest value, not a percentile."""


def load_posted_at(db: Path, channel: str, since: datetime | None) -> list[datetime]:
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        rows = con.execute(
            "select posted_at from raw_telegram_messages "
            "where channel = ? order by posted_at",
            (channel,),
        ).fetchall()
    finally:
        con.close()
    out = []
    for (value,) in rows:
        try:
            ts = datetime.fromisoformat(str(value))
        except ValueError:
            continue
        if since is None or ts >= since:
            out.append(ts)
    return out


def summarise(ts: list[datetime], channel: str, at_hour: int | None,
              window_minutes: float) -> dict:
    if not ts:
        return {"channel": channel, "n": 0, "verdict": "no history for this channel"}

    span_days = max((ts[-1] - ts[0]).total_seconds() / 86400.0, 1e-9)
    per_day = len(ts) / span_days

    by_hour = Counter(t.hour for t in ts)
    # messages per calendar day that land inside this hour, then per hour
    hour_rate = {h: c / span_days for h, c in by_hour.items()}

    gaps = [
        (b - a).total_seconds() / 60.0
        for a, b in zip(ts, ts[1:])
        if 0 <= (b - a).total_seconds() / 60.0 < 720
    ]

    result = {
        "channel": channel,
        "n": len(ts),
        "first": ts[0].isoformat(),
        "last": ts[-1].isoformat(),
        "span_days": round(span_days, 1),
        "messages_per_day": round(per_day, 2),
        "messages_per_hour": round(per_day / 24.0, 3),
        "gap_minutes_median": round(statistics.median(gaps), 1) if gaps else None,
        "gap_minutes_mean": round(statistics.fmean(gaps), 1) if gaps else None,
        "by_hour_utc": {h: round(r, 3) for h, r in sorted(hour_rate.items())},
    }

    if at_hour is None:
        busiest = max(hour_rate.items(), key=lambda kv: kv[1]) if hour_rate else None
        at_hour = busiest[0] if busiest else 0
        result["at_hour_assumed"] = at_hour

    rate_h = hour_rate.get(at_hour, 0.0)
    expected = rate_h * window_minutes / 60.0
    hours_for_20 = SAMPLES_FOR_P95 / rate_h if rate_h > 0 else None

    result.update({
        "at_hour_utc": at_hour,
        "rate_at_hour_per_hour": round(rate_h, 3),
        "window_minutes": window_minutes,
        "expected_messages_in_window": round(expected, 2),
        "hours_listening_for_20_samples": round(hours_for_20, 1) if hours_for_20 else None,
        "verdict": _verdict(expected, hours_for_20, at_hour, rate_h),
    })
    return result


def _verdict(expected: float, hours_for_20: float | None, hour: int, rate: float) -> str:
    if rate <= 0:
        return (f"INFEASIBLE at {hour:02d}:00 UTC - the channel has never posted "
                f"in this hour; pick another window")
    if expected < 1:
        return (f"UNDERPOWERED - expect {expected:.2f} message(s); a window this "
                f"short will usually return nothing. {hours_for_20:.1f}h of "
                f"listening is needed for a p95 over {SAMPLES_FOR_P95} samples")
    if expected < SAMPLES_FOR_P95:
        return (f"WEAK - expect {expected:.1f} message(s) in this window; enough to "
                f"prove the pipe works, not enough for a p95. "
                f"{hours_for_20:.1f}h gives {SAMPLES_FOR_P95}")
    return (f"ADEQUATE - expect {expected:.1f} message(s), enough for a p95 over "
            f"{SAMPLES_FOR_P95}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(ROOT / "data" / "rade.db"))
    ap.add_argument("--channel", default="@XAUUSD_FOREX4")
    ap.add_argument("--days", type=int, default=None,
                    help="restrict to the last N days of history")
    ap.add_argument("--at-hour", type=int, default=None,
                    help="UTC hour to plan the probe in (default: the busiest)")
    ap.add_argument("--window-minutes", type=float, default=240.0)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    since = None
    if args.days:
        # the database stores naive UTC; compare like with like
        since = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=args.days)
    ts = load_posted_at(Path(args.db), args.channel, since)
    result = summarise(ts, args.channel, args.at_hour, args.window_minutes)

    if args.json:
        print(json.dumps(result, indent=2))
        return 0

    print(f"channel {result['channel']}: {result['n']} message(s) "
          f"{result.get('first', '?')[:10]} -> {result.get('last', '?')[:10]}")
    if result["n"] == 0:
        print(result["verdict"])
        return 1
    print(f"  span {result['span_days']}d  {result['messages_per_day']}/day  "
          f"{result['messages_per_hour']}/hour")
    print(f"  gap median {result['gap_minutes_median']}min  "
          f"mean {result['gap_minutes_mean']}min")
    print("  messages per hour, by UTC hour:")
    peak = max(result["by_hour_utc"].values())
    for h, r in result["by_hour_utc"].items():
        bar = "#" * int(round(40 * r / peak)) if peak else ""
        print(f"    {h:02d}  {r:6.3f}  {bar}")
    print(f"\n  planning a {result['window_minutes']:.0f}min probe at "
          f"{result['at_hour_utc']:02d}:00 UTC")
    print(f"  expected messages: {result['expected_messages_in_window']}")
    print(f"  verdict: {result['verdict']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
