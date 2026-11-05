"""CLI: backfill Telegram channel history."""
import argparse, sys
sys.path.insert(0, "src")
from rade.ingestion.telegram.backfill import run

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--since", type=int, default=0)
    ap.add_argument("--channel", type=str, default=None)
    args = ap.parse_args()
    print(run(channel=args.channel, limit=args.limit, since_id=args.since))
