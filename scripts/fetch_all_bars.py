"""Fetch and store XAUUSD bars (D1 + 30m + 1H) from Tradelocker into the DB."""
from __future__ import annotations

import sys
from datetime import datetime, timezone

import pandas as pd
from dotenv import load_dotenv

sys.path.insert(0, "src")
load_dotenv(".env")

from rade.config.settings import get_settings
from rade.ingestion.marketdata.tradelocker import fetch_and_store
from rade.storage.db import make_engine, make_session_factory
from rade.storage.models import Bar, Base, Instrument
from sqlalchemy import select
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
iid = tl.get_instrument_id_from_symbol_name("XAUUSD")

engine = make_engine()
Base.metadata.create_all(engine)
SessionFactory = make_session_factory()

def upsert(resolution, start, end):
    start_ms = int(pd.Timestamp(start, tz="UTC").timestamp() * 1000)
    end_ms = int(pd.Timestamp(end, tz="UTC").timestamp() * 1000)
    df = tl.get_price_history(iid, resolution=resolution, start_timestamp=start_ms, end_timestamp=end_ms)
    if df is None or df.empty:
        print(resolution, "empty")
        return 0
    df = df.rename(columns={"t": "ts", "o": "open", "h": "high", "l": "low", "c": "close", "v": "volume"})
    df["ts"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
    with SessionFactory() as session:
        inst = session.query(Instrument).filter_by(symbol="XAUUSD").first()
        if inst is None:
            inst = Instrument(symbol="XAUUSD", broker="tradelocker", asset_class="metal")
            session.add(inst)
            session.flush()
        inst_id = inst.id
        inserted = 0
        for _, row in df.iterrows():
            ts = row["ts"].to_pydatetime()
            existing = session.query(Bar).filter_by(instrument_id=inst_id, timeframe=resolution, ts=ts).first()
            vals = dict(
                instrument_id=inst_id,
                timeframe=resolution,
                ts=ts,
                open=float(row["open"]),
                high=float(row["high"]),
                low=float(row["low"]),
                close=float(row["close"]),
                volume=float(row["volume"]) if pd.notna(row["volume"]) else None,
            )
            if existing is None:
                session.add(Bar(**vals))
                inserted += 1
            else:
                for k, v in vals.items():
                    setattr(existing, k, v)
        session.commit()
    print(f"{resolution}: fetched {len(df)} bars, inserted {inserted}")
    return len(df)

if __name__ == "__main__":
    upsert("1D", "2016-01-01", "2026-11-03")
    upsert("30m", "2025-04-01", "2026-11-03")
    upsert("1H", "2025-04-01", "2026-11-03")
