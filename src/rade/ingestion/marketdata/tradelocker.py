"""Tradelocker market-data fetcher (D1 bars for XAUUSD)."""
from __future__ import annotations

import os
from datetime import datetime, timezone

import pandas as pd
from dotenv import load_dotenv
from sqlalchemy.orm import Session

from rade.config.settings import get_settings
from rade.storage.db import make_engine, make_session_factory
from rade.storage.models import Base, Bar, Instrument

load_dotenv(".env")


def _ms_to_dt(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc)


def fetch_and_store(symbol: str = "XAUUSD", resolution: str = "1D", lookback: str = "365D") -> dict:
    from tradelocker import TLAPI

    s = get_settings()
    tl = TLAPI(
        environment=s.tradelocker_base_url,
        username=s.tradelocker_email,
        password=s.tradelocker_password,
        server=s.tradelocker_server,
        account_id=s.tradelocker_account_id,
        log_level="warning",
    )
    iid = tl.get_instrument_id_from_symbol_name(symbol)

    df = tl.get_price_history(iid, resolution=resolution, lookback_period=lookback)
    if df is None or df.empty:
        return {"bars": 0}

    # normalise column names
    cols = {c.lower(): c for c in df.columns}
    # expected: t, o, h, l, c, v
    tcol = cols.get("t", "t")
    ocol = cols.get("o", "o")
    hcol = cols.get("h", "h")
    lcol = cols.get("l", "l")
    ccol = cols.get("c", "c")
    vcol = cols.get("v", "v")

    engine = make_engine()
    Base.metadata.create_all(engine)
    SessionFactory = make_session_factory()

    with SessionFactory() as session:
        # upsert instrument
        inst = session.query(Instrument).filter_by(symbol=symbol).first()
        if inst is None:
            inst = Instrument(symbol=symbol, broker="tradelocker", asset_class="metal")
            session.add(inst)
            session.flush()
        inst_id = inst.id

        inserted = 0
        for _, row in df.iterrows():
            ts = _ms_to_dt(int(row[tcol]))
            existing = (
                session.query(Bar)
                .filter_by(instrument_id=inst_id, timeframe=resolution, ts=ts)
                .first()
            )
            vals = dict(
                instrument_id=inst_id,
                timeframe=resolution,
                ts=ts,
                open=float(row[ocol]),
                high=float(row[hcol]),
                low=float(row[lcol]),
                close=float(row[ccol]),
                volume=float(row[vcol]) if vcol in df.columns and pd.notna(row[vcol]) else None,
            )
            if existing is None:
                session.add(Bar(**vals))
                inserted += 1
            else:
                for k, v in vals.items():
                    setattr(existing, k, v)
        session.commit()

    return {"bars": len(df), "inserted": inserted, "instrument_id": inst_id}
