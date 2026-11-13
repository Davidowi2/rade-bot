"""Listen to Telegram and record paper intents. No order endpoint is called.

This process can place no order: there is no broker object in it. A NOW signal
uses a read-only quote; if the quote cannot be fetched the message is recorded
as blocked rather than priced from a guess.

It refuses to start while another Telegram listener is already running against
the same session, because two readers of one Telethon session corrupt each
other's update state.

Examples:
    # stored history only, no Telegram connection
    python scripts/telegram_paper_listener.py --dry-run --message-id 18048 \
        --contract-units-per-lot 100

    # live, 15 minutes, read-only quotes. The contract size must be confirmed
    # against the broker's specification, and the first activation starts from
    # the newest message so the channel's history is not imported.
    python scripts/telegram_paper_listener.py --seconds 900 --equity 10000 \
        --contract-units-per-lot 100 --confirm-contract-units --start-at latest
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from rade.observer.listener import (  # noqa: E402
    MirrorListener,
    MirrorState,
    Runtime,
    install_signal_handlers,
)
from rade.observer.mirror_pipeline import (  # noqa: E402
    InstrumentSpec,
    MarketQuote,
    MessageEnvelope,
    RiskConfig,
    parse_message_signal,
    process_once,
)
from rade.observer.notify import notifier_from_env  # noqa: E402
from rade.observer.quotes import tradelocker_quote_provider  # noqa: E402
from rade.observer.store import StorageError, make_ledger  # noqa: E402

SOAK_HEARTBEAT = ROOT / "docs" / "evidence" / "telegram-soak.heartbeat.json"


def soak_is_active(max_age_s: float = 180.0) -> dict | None:
    """Return the soak heartbeat when another listener is plausibly running."""
    if not SOAK_HEARTBEAT.exists():
        return None
    age = time.time() - SOAK_HEARTBEAT.stat().st_mtime
    if age > max_age_s:
        return None
    try:
        data = json.loads(SOAK_HEARTBEAT.read_text(encoding="utf-8"))
    except ValueError:
        return {"phase": "unknown", "age_s": age}
    if data.get("phase") not in {"listening", "connecting"}:
        return None
    data["age_s"] = round(age, 1)
    return data


def run_dry(args, instrument: InstrumentSpec, risk: RiskConfig, ledger) -> int:
    """Replay one stored message. No Telegram, no broker, no notification."""
    import sqlite3

    con = sqlite3.connect(args.db)
    row = con.execute(
        "SELECT channel, message_id, posted_at, text FROM raw_telegram_messages WHERE message_id=?",
        (args.message_id,),
    ).fetchone()
    con.close()
    if row is None:
        print(f"message {args.message_id} is not in {args.db}")
        return 2

    channel, message_id, posted_at, text = row
    posted = datetime.fromisoformat(posted_at)
    if posted.tzinfo is None:
        posted = posted.replace(tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)
    envelope = MessageEnvelope(channel, int(message_id), 0, posted, now, text or "",
                               source="stored_telegram_history")

    quote = None
    parsed = parse_message_signal(envelope.text)
    if parsed and parsed.get("entry_type") == "market":
        entry = parsed.get("entry_price")
        # A synthetic quote at the published entry, labelled as synthetic. It is
        # not a live price and must not be read as one.
        quote = MarketQuote(float(entry), float(entry), now) if entry else None

    result = process_once(envelope, ledger=ledger, instrument=instrument,
                          risk=risk, quote=quote)
    print(json.dumps({
        "mode": "dry_run",
        "source": "stored Telegram history (not a live push)",
        "quote_source": "synthetic, at the published entry",
        **result.to_dict(),
    }, indent=2))
    return 0


async def run_live(args, instrument: InstrumentSpec, risk: RiskConfig, ledger) -> int:
    from rade.ingestion.telegram.client import make_client
    from rade.config.settings import get_settings

    settings = get_settings()
    channel = args.channel or settings.telegram_channel
    if not channel:
        print("TELEGRAM_CHANNEL is not configured")
        return 2

    notifier = None if args.no_notify else notifier_from_env()
    quote_provider = None
    if args.quote_provider == "tradelocker":
        quote_provider = tradelocker_quote_provider()

    listener = MirrorListener(
        channel=channel,
        ledger=ledger,
        instrument=instrument,
        risk=risk,
        notifier=notifier,
        quote_provider=quote_provider,
        state=MirrorState(args.state),
    )
    runtime = Runtime(
        client=make_client(),
        listener=listener,
        channel=channel,
        reconnect_every=args.reconnect_every,
        heartbeat_path=Path(args.heartbeat) if args.heartbeat else None,
        start_at=args.start_at,
        allow_history_import=args.allow_history_import,
    )
    install_signal_handlers(runtime)

    if notifier is None:
        print("notifications: disabled (TELEGRAM_BOT_TOKEN / TELEGRAM_NOTIFY_CHAT_ID not set)")
    print(f"PAPER ONLY: {channel}; quotes={args.quote_provider}; "
          f"reconnect every {args.reconnect_every:.0f}s; no broker write path")
    summary = await runtime.run(seconds=args.seconds)
    print(json.dumps(summary, indent=2))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true",
                    help="replay one stored message; never connects to Telegram")
    ap.add_argument("--message-id", type=int, default=18048)
    ap.add_argument("--db", default="data/rade.db")
    ap.add_argument("--seconds", type=float, default=None,
                    help="stop after N seconds (omit to run until SIGTERM)")
    ap.add_argument("--reconnect-every", type=float, default=900.0,
                    help="force a reconnect + gap fill this often (0 disables)")
    ap.add_argument("--equity", type=float, default=None,
                    help="paper account equity; required for live mode, never guessed")
    ap.add_argument("--contract-units-per-lot", type=float, default=None,
                    help="XAUUSD contract units per lot from the broker; never guessed")
    ap.add_argument("--confirm-contract-units", action="store_true",
                    help="assert that the contract size has been checked against "
                         "the broker's own specification (required for live mode)")
    ap.add_argument("--risk-pct", type=float, default=0.005,
                    help="per-trade risk fraction; the cap is 0.5%%")
    ap.add_argument("--quote-max-age", type=float, default=2.0)
    ap.add_argument("--quote-provider", choices=["none", "tradelocker"], default="none")
    ap.add_argument("--start-at", default="latest",
                    help="where the first activation starts: 'latest' (import nothing "
                         "that is already in the channel), a message id, or 'history'")
    ap.add_argument("--allow-history-import", action="store_true",
                    help="required by --start-at history: import the whole channel, "
                         "old calls included")
    ap.add_argument("--ledger", default=None,
                    help="SQLite ledger path when Supabase is not configured")
    ap.add_argument("--state", default="data/mirror_state.json")
    ap.add_argument("--heartbeat", default="data/mirror_listener.heartbeat.json")
    ap.add_argument("--no-notify", action="store_true")
    ap.add_argument("--allow-concurrent", action="store_true",
                    help="override the guard against a second listener on the same session")
    args = ap.parse_args()

    if args.ledger:
        os.environ["MIRROR_SQLITE_PATH"] = args.ledger
    try:
        ledger = make_ledger()
    except StorageError as exc:
        print(str(exc), file=sys.stderr)
        return 3

    if args.dry_run:
        if args.contract_units_per_lot is None:
            print("--contract-units-per-lot is required (the size is never guessed)")
            return 2
        instrument = InstrumentSpec("XAUUSD", args.contract_units_per_lot, 0.01, 0.01, 50.0,
                                    units_confirmed=args.confirm_contract_units)
        risk = RiskConfig(args.equity or 10_000.0, args.risk_pct, args.quote_max_age)
        return run_dry(args, instrument, risk, ledger)

    # --- live guards -----------------------------------------------------
    active = soak_is_active()
    if active and not args.allow_concurrent:
        print("refusing to start: another Telegram listener is active on this session")
        print(json.dumps(active, indent=2))
        print("Two readers of one Telethon session corrupt each other's update state.")
        print("Stop it, or pass --allow-concurrent if you are certain.")
        return 3
    if args.equity is None:
        print("--equity is required for live mode (the size is never guessed)")
        return 2
    if args.contract_units_per_lot is None:
        print("--contract-units-per-lot is required for live mode (never guessed)")
        return 2
    if not args.confirm_contract_units:
        print("--confirm-contract-units is required for live mode: the contract size "
              "must have been checked against the broker's own specification")
        return 2

    instrument = InstrumentSpec("XAUUSD", args.contract_units_per_lot, 0.01, 0.01, 50.0,
                               units_confirmed=True)
    risk = RiskConfig(args.equity, args.risk_pct, args.quote_max_age)
    return asyncio.run(run_live(args, instrument, risk, ledger))


if __name__ == "__main__":
    raise SystemExit(main())
