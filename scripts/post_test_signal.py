"""Post a fake NOW signal, priced at the live demo quote.

Test signals have to be priced where the market actually is, or every check
refuses them for a reason that has nothing to do with what is being tested. This
composes one from the current bid/ask and posts it to the test channel.

It reads a quote from the TradeLocker **demo** account. It never places, amends
or closes anything: the only broker call in this file is ``quote()``.

    python scripts/post_test_signal.py --side sell
    python scripts/post_test_signal.py --side buy --tp 1,2,3 --stop 6
    python scripts/post_test_signal.py --text "malformed signal"   # as-is

Prints the message id so the run can be followed in the ledger.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys

from dotenv import load_dotenv

load_dotenv(".env")

from rade.execution.gateway import tradelocker_gateway_from_env  # noqa: E402
from rade.ingestion.telegram.client import make_client  # noqa: E402

DEFAULT_TEST_CHANNEL = "@rade_mirror_test_20261113"
#: A separate session file, because a Telegram session file can only be open in
#: one process and the listener already holds the main one.
POST_SESSION = "data/post_session"


def compose(side: str, entry: float, *, stop: float, targets: list[float]) -> str:
    """A signal whose stop and targets sit on the correct sides of the entry.

    For a SELL the stop is above and the targets below; for a BUY it is the
    other way round. Getting this wrong produces a message the pipeline
    correctly refuses, which is a fine malformed case but a useless valid one.
    """
    stop_sign = 1 if side == "sell" else -1
    target_sign = -1 if side == "sell" else 1
    lines = [f"XAUUSD {side.upper()} NOW {entry:.2f}",
             f"Sl : {entry + stop_sign * stop:.2f}"]
    lines.extend(f"Tp : {entry + target_sign * t:.2f}" for t in targets)
    return "\n".join(lines)


async def send(text: str) -> int:
    channel = os.getenv("MIRROR_TEST_CHANNEL", DEFAULT_TEST_CHANNEL).strip()
    live = (os.getenv("TELEGRAM_CHANNEL") or "").strip().lower()
    if live and channel.lower() == live:
        raise SystemExit(
            f"refusing to write to {channel}: that is TELEGRAM_CHANNEL, the "
            "trader's channel")
    client = make_client(POST_SESSION)
    await client.connect()
    try:
        entity = await client.get_entity(channel)
        message = await client.send_message(entity, text)
        print(f"posted id={message.id} at {message.date.isoformat()}")
        return int(message.id)
    finally:
        await client.disconnect()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--side", choices=["buy", "sell"], default="sell")
    parser.add_argument("--tp", default="6,11,16",
                        help="target distances from entry, in price units")
    parser.add_argument("--stop", type=float, default=9.0)
    parser.add_argument("--text", default=None,
                        help="post this text verbatim instead of composing one")
    parser.add_argument("--no-quote", action="store_true",
                        help="compose from --entry instead of the live quote")
    parser.add_argument("--entry", type=float, default=0.0)
    args = parser.parse_args()

    if args.text is not None:
        text = args.text
        print(text)
        asyncio.run(send(text))
        return 0

    if args.no_quote:
        entry = args.entry
    else:
        gateway = tradelocker_gateway_from_env(
            confirmed_contract_units=float(
                os.getenv("MIRROR_CONTRACT_UNITS_PER_LOT", "100") or 100),
            log=lambda line: None)
        quote = gateway.quote()
        # A SELL is priced at the bid and a BUY at the ask: that is where the
        # market would actually fill, so the slippage check is not tripped by
        # the spread alone.
        entry = float(quote.bid if args.side == "sell" else quote.ask)
        print(f"quote bid={quote.bid} ask={quote.ask}")

    targets = [float(t) for t in args.tp.split(",") if t.strip()]
    text = compose(args.side, round(entry, 2), stop=args.stop, targets=targets)
    print(text)
    asyncio.run(send(text))
    return 0


if __name__ == "__main__":
    sys.exit(main())
