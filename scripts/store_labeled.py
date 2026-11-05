"""Populate labeled_setups table from the CSV/DB."""
from __future__ import annotations

import json
import sys

import pandas as pd
from dotenv import load_dotenv

sys.path.insert(0, "src")
load_dotenv(".env")

from rade.storage.db import make_engine, make_session_factory
from rade.storage.models import Base, LabeledSetup

engine = make_engine()
Base.metadata.create_all(engine)
sf = make_session_factory()

df = pd.read_csv("data/labeled_setups.csv")

with sf() as session:
    # clear existing
    session.query(LabeledSetup).delete()
    session.commit()
    for _, r in df.iterrows():
        tps = r["take_profits"]
        if isinstance(tps, str):
            try:
                tps = json.loads(tps.replace("'", '"'))
            except Exception:
                tps = []
        row = LabeledSetup(
            message_pk=int(r["message_pk"]),
            posted_at=pd.to_datetime(r["posted_at"], utc=True).to_pydatetime() if pd.notna(r["posted_at"]) else None,
            instrument=r["instrument"],
            direction=r["direction"],
            entry_type=r["entry_type"],
            entry_price=float(r["entry_price"]) if pd.notna(r["entry_price"]) else None,
            stop_loss=float(r["stop_loss"]) if pd.notna(r["stop_loss"]) else None,
            take_profits=json.dumps(tps) if tps else None,
            is_recap=int(r["is_recap"]) if pd.notna(r["is_recap"]) else 0,
            fill_status=r["fill_status"],
            fill_price=float(r["fill_price"]) if pd.notna(r["fill_price"]) else None,
            fill_ts=pd.to_datetime(r["fill_ts"], utc=True).to_pydatetime() if pd.notna(r["fill_ts"]) else None,
            exit_type=r["exit_type"],
            exit_price=float(r["exit_price"]) if pd.notna(r["exit_price"]) else None,
            exit_ts=pd.to_datetime(r["exit_ts"], utc=True).to_pydatetime() if pd.notna(r["exit_ts"]) else None,
            R=float(r["R"]) if pd.notna(r["R"]) else None,
            hours_held=float(r["hours_held"]) if pd.notna(r["hours_held"]) else None,
            ambiguity=int(r["ambiguity"]) if pd.notna(r["ambiguity"]) else 0,
            ambiguity_optimistic=int(r["ambiguity_optimistic"]) if pd.notna(r["ambiguity_optimistic"]) else 0,
            notes=r["notes"] if pd.notna(r["notes"]) else None,
        )
        session.add(row)
    session.commit()
    print("inserted", session.query(LabeledSetup).count())
