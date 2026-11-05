"""Populate features table from data/features.csv (long format)."""
from __future__ import annotations

import sys

import pandas as pd
from dotenv import load_dotenv

sys.path.insert(0, "src")
load_dotenv(".env")

from rade.storage.db import make_engine, make_session_factory
from rade.storage.models import Base, Feature

engine = make_engine()
Base.metadata.create_all(engine)
sf = make_session_factory()

df = pd.read_csv("data/features.csv")
id_cols = {"message_pk", "posted_at"}
feature_cols = [c for c in df.columns if c not in id_cols]

with sf() as session:
    session.query(Feature).delete()
    session.commit()
    n = 0
    for _, r in df.iterrows():
        ts = pd.to_datetime(r["posted_at"], utc=True).to_pydatetime()
        mpk = int(r["message_pk"]) if pd.notna(r["message_pk"]) else None
        for fc in feature_cols:
            val = r[fc]
            session.add(
                Feature(
                    instrument="XAUUSD",
                    ts=ts,
                    message_pk=mpk,
                    feature_name=fc,
                    value=float(val) if pd.notna(val) else None,
                )
            )
            n += 1
    session.commit()
    print("inserted feature rows:", n)
    print("distinct features:", len(feature_cols))
