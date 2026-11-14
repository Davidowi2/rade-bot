"""Measure spread and short-term slippage on the destination feed.

Read-only. Samples the destination quote and recent 1-minute bars, then prints
distributions. Never prints credentials.
"""
from __future__ import annotations

import os
import statistics
import sys
import time
from datetime import timedelta

from dotenv import load_dotenv

load_dotenv(".env")

EMAIL = os.getenv("Login")
PASSWORD = os.getenv("Password")
SERVER = os.getenv("Server", "BLBRY")
BASE_URL = os.getenv("BLUEBERRY_BASE_URL", "https://demo.tradelocker.com")
ACCOUNT_ID = os.getenv("BLUEBERRY_ACCOUNT_ID", "")


def main() -> int:
    if not EMAIL or not PASSWORD:
        print("Set the destination credentials in the environment.")
        return 2

    from tradelocker import TLAPI

    tl = TLAPI(environment=BASE_URL, username=EMAIL, password=PASSWORD,
               server=SERVER,
               account_id=int(ACCOUNT_ID) if ACCOUNT_ID else None,
               log_level="error")
    iid = tl.get_instrument_id_from_symbol_name("XAUUSD")
    print(f"instrument {iid} on {BASE_URL} server {SERVER}")

    # --- spread sampling ---
    spreads, prices = [], []
    print("sampling the quote for 60 seconds ...")
    for _ in range(30):
        try:
            bid = float(tl.get_latest_bid_price(iid))
            ask = float(tl.get_latest_asking_price(iid))
            spreads.append(ask - bid)
            prices.append((ask + bid) / 2.0)
        except Exception as exc:
            print(f"  quote read failed: {type(exc).__name__}")
        time.sleep(2.0)

    if spreads:
        spreads_sorted = sorted(spreads)
        def pct(p):
            idx = min(len(spreads_sorted) - 1, int(p / 100.0 * len(spreads_sorted)))
            return spreads_sorted[idx]
        print("\n=== SPREAD (ask - bid, price units) ===")
        print(f"  samples: {len(spreads)}")
        print(f"  min {min(spreads):.3f}  p50 {pct(50):.3f}  p90 {pct(90):.3f}  "
              f"p95 {pct(95):.3f}  p99 {pct(99):.3f}  max {max(spreads):.3f}")
        print(f"  mean {statistics.mean(spreads):.3f}")

    # --- 1-minute bar range (slippage proxy) ---
    try:
        df = tl.get_price_history(iid, resolution="1m", lookback_period="1D")
    except Exception as exc:
        print(f"could not read bars: {type(exc).__name__}")
        return 0

    if df is None or df.empty:
        print("no bars returned")
        return 0

    cols = {c.lower(): c for c in df.columns}
    high = df[cols.get("h", "h")].astype(float)
    low = df[cols.get("l", "l")].astype(float)
    close = df[cols.get("c", "c")].astype(float)
    rng = (high - low).dropna()
    # 15-second move approximated from the 1-minute range (sqrt of time)
    sec15 = rng * (15.0 / 60.0) ** 0.5
    for name, series in (("1-minute range", rng), ("~15-second move", sec15)):
        s = sorted(series.tolist())
        def pct(p):
            return s[min(len(s) - 1, int(p / 100.0 * len(s)))]
        print(f"\n=== {name.upper()} (price units) ===")
        print(f"  n {len(s)}  p50 {pct(50):.3f}  p90 {pct(90):.3f}  "
              f"p95 {pct(95):.3f}  p99 {pct(99):.3f}  max {s[-1]:.3f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
