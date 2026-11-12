"""Read-only quote providers.

A quote is the only broker interaction the paper mirror has. There is no order
object in this module and no write method is referenced, so a bug here cannot
place, amend or close anything.

The provider refuses a non-demo TradeLocker environment: paper sizing against a
live account would be a live-money decision made by accident.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable

from rade.observer.mirror_pipeline import MarketQuote


def tradelocker_quote_provider(symbol: str = "XAUUSD") -> Callable[[], MarketQuote]:
    """Build a callable returning the current demo bid/ask."""
    from tradelocker import TLAPI

    from rade.config.settings import get_settings

    settings = get_settings()
    if settings.tradelocker_env.lower() != "demo":
        raise RuntimeError("refusing a non-demo TradeLocker environment for paper quotes")

    client = TLAPI(
        environment=settings.tradelocker_base_url,
        username=settings.tradelocker_email,
        password=settings.tradelocker_password,
        server=settings.tradelocker_server,
        account_id=settings.tradelocker_account_id,
        log_level="error",
    )
    instrument_id = client.get_instrument_id_from_symbol_name(symbol)

    def quote() -> MarketQuote:
        bid = float(client.get_latest_bid_price(instrument_id))
        ask = float(client.get_latest_asking_price(instrument_id))
        return MarketQuote(bid, ask, datetime.now(timezone.utc))

    return quote
