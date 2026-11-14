"""Read-only Blueberry/TradeLocker account check.

Prints only non-sensitive facts: account id, name, currency, balance, equity,
the XAUUSD instrument specification, open positions and order counts.
Never prints credentials.

Usage:
    export BLUEBERRY_EMAIL=...
    export BLUEBERRY_PASSWORD=...
    export BLUEBERRY_SERVER=BLBRY
    .venv/Scripts/python.exe scripts/check_blueberry_account.py
"""
from __future__ import annotations

import os
import sys

from dotenv import load_dotenv

load_dotenv(".env")

EMAIL = os.getenv("BLUEBERRY_EMAIL") or os.getenv("Login")
PASSWORD = os.getenv("BLUEBERRY_PASSWORD") or os.getenv("Password")
SERVER = os.getenv("BLUEBERRY_SERVER") or os.getenv("Server") or "BLBRY"
PLATFORM = os.getenv("Platform") or "TradeLocker"
BASE_URL = os.getenv("BLUEBERRY_BASE_URL") or "https://app.tradelocker.com"
ACCOUNT_ID = os.getenv("BLUEBERRY_ACCOUNT_ID", "")


def main() -> int:
    if not EMAIL or not PASSWORD:
        print("Set BLUEBERRY_EMAIL and BLUEBERRY_PASSWORD in the environment first.")
        return 2

    from tradelocker import TLAPI

    print(f"Connecting to {BASE_URL} (server {SERVER}) ...")
    tl = TLAPI(
        environment=BASE_URL,
        username=EMAIL,
        password=PASSWORD,
        server=SERVER,
        account_id=int(ACCOUNT_ID) if ACCOUNT_ID else None,
        log_level="error",
    )

    print("\n=== ACCOUNTS ===")
    accounts = tl.get_trade_accounts() or []
    for a in accounts:
        print({k: a.get(k) for k in
               ("id", "accNum", "name", "currency", "balance", "equity",
                "projectedBalance", "marginLevel", "freeMargin") if k in a})

    print("\n=== ACCOUNT STATE ===")
    st = tl.get_account_state() or {}
    for k in ("balance", "equity", "projectedBalance", "currency",
              "freeMargin", "marginLevel", "openPl", "unrealizedPl"):
        if k in st:
            print(f"  {k}: {st[k]}")

    print("\n=== XAUUSD INSTRUMENT SPEC ===")
    try:
        iid = tl.get_instrument_id_from_symbol_name("XAUUSD")
        print(f"  instrument_id: {iid}")
        d = tl.get_instrument_details(iid) or {}
        for k in ("lotSize", "lotStep", "minLot", "maxLot", "tickSize",
                  "symbolStatus", "contractSize", "minVolume", "maxVolume",
                  "volumeStep", "leverage", "marginMode"):
            if k in d:
                print(f"  {k}: {d[k]}")
    except Exception as exc:
        print(f"  could not read instrument details: {type(exc).__name__}")

    print("\n=== OPEN POSITIONS ===")
    try:
        pos = tl.get_all_positions()
        if pos is None or getattr(pos, "empty", True):
            print("  none")
        else:
            for _, r in pos.iterrows():
                print({k: r.get(k) for k in
                       ("id", "side", "qty", "avgPrice", "stopLossId",
                        "takeProfitId", "unrealizedPl") if k in r})
    except Exception as exc:
        print(f"  could not read positions: {type(exc).__name__}")

    print("\n=== ORDERS ===")
    try:
        orders = tl.get_all_orders(history=False)
        print(f"  open/pending orders: {0 if orders is None else len(orders)}")
    except Exception as exc:
        print(f"  could not read orders: {type(exc).__name__}")

    print("\nDone. No credentials were printed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
