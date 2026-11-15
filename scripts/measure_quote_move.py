"""Measure the quote's short-term move, which is the poll-delay slippage.

Read-only. Samples the destination mid price once a second for a few minutes and
reports the distribution of the move over a 15-second window (the catch-up
poll interval). Never prints credentials.

    .venv/Scripts/python.exe scripts/measure_quote_move.py [seconds]
"""
from __future__ import annotations

import os
import statistics
import sys
import time

from dotenv import load_dotenv

load_dotenv(".env")

EMAIL = os.getenv("Login")
PASSWORD = os.getenv("Password")
SERVER = os.getenv("Server", "BLBRY")
BASE_URL = os.getenv("BLUEBERRY_BASE_URL", "https://demo.tradelocker.com")
ACCOUNT_ID = os.getenv("BLUEBERRY_ACCOUNT_ID", "")


def pct(sorted_values: list[float], p: float) -> float:
    if not sorted_values:
        return 0.0
    return sorted_values[min(len(sorted_values) - 1, int(p / 100.0 * len(sorted_values)))]


def main() -> int:
    seconds = int(sys.argv[1]) if len(sys.argv) > 1 else 180
    if not EMAIL or not PASSWORD:
        print("Set the destination credentials in the environment.")
        return 2

    from tradelocker import TLAPI

    tl = TLAPI(environment=BASE_URL, username=EMAIL, password=PASSWORD,
               server=SERVER,
               account_id=int(ACCOUNT_ID) if ACCOUNT_ID else None,
               log_level="error")
    iid = tl.get_instrument_id_from_symbol_name("XAUUSD")

    mids: list[tuple[float, float]] = []
    print(f"sampling the mid price every second for {seconds}s ...")
    started = time.time()
    while time.time() - started < seconds:
        try:
            bid = float(tl.get_latest_bid_price(iid))
            ask = float(tl.get_latest_asking_price(iid))
            mids.append((time.time(), (ask + bid) / 2.0))
        except Exception as exc:
            print(f"  quote read failed: {type(exc).__name__}")
        time.sleep(1.0)

    if len(mids) < 20:
        print(f"too few samples ({len(mids)})")
        return 0

    # Move over a 15-second window, matching MIRROR_CATCHUP_EVERY_S.
    window_s = 15.0
    moves: list[float] = []
    for i, (t0, p0) in enumerate(mids):
        # The quote API takes a few seconds per call, so the samples are not
        # one second apart. Match the sample closest to t0 + window rather than
        # requiring an exact one-second grid.
        future = [p for (t, p) in mids if abs((t - t0) - window_s) <= 3.0]
        if future:
            moves.append(abs(future[0] - p0))
    moves.sort()

    print(f"\nsamples {len(mids)} | 15s windows {len(moves)}")
    if moves:
        print(f"  p50 {pct(moves, 50):.3f}  p90 {pct(moves, 90):.3f}  "
              f"p95 {pct(moves, 95):.3f}  p99 {pct(moves, 99):.3f}  "
              f"max {moves[-1]:.3f}  mean {statistics.mean(moves):.3f}")
        print(f"  at 0.03 lots: p50 ${pct(moves, 50) * 3:.2f}  "
              f"p95 ${pct(moves, 95) * 3:.2f}  max ${moves[-1] * 3:.2f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
