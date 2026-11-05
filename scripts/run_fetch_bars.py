"""CLI: fetch D1 bars from Tradelocker."""
import argparse, sys
sys.path.insert(0, "src")
from rade.ingestion.marketdata.tradelocker import fetch_and_store

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="XAUUSD")
    ap.add_argument("--resolution", default="1D")
    ap.add_argument("--lookback", default="365D")
    args = ap.parse_args()
    print(fetch_and_store(args.symbol, args.resolution, args.lookback))
