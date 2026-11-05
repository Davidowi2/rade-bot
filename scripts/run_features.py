"""Run Phase 2B feature engineering on real data."""
from __future__ import annotations

import sys

import pandas as pd
from sqlalchemy import select

sys.path.insert(0, "src")
from rade.features import run_feature_pipeline
from rade.labelling.models import Setup
from rade.labelling.setup_extractor import extract_all
from rade.storage.db import make_session_factory
from rade.storage.models import Bar, RawTelegramMessage

sf = make_session_factory()
with sf() as session:
    raw_rows = [(m.id, m.posted_at, m.text) for m in session.query(RawTelegramMessage).all() if m.text]
    d1_df = pd.DataFrame(
        [{"ts": b.ts, "open": b.open, "high": b.high, "low": b.low, "close": b.close, "volume": b.volume}
         for b in session.query(Bar).filter_by(timeframe="1D").all()]
    )
    h1_df = pd.DataFrame(
        [{"ts": b.ts, "open": b.open, "high": b.high, "low": b.low, "close": b.close, "volume": b.volume}
         for b in session.query(Bar).filter_by(timeframe="1H").all()]
    )

for d in (d1_df, h1_df):
    d["ts"] = pd.to_datetime(d["ts"], utc=True)

setups = extract_all(raw_rows)
feat_df = run_feature_pipeline(setups, d1_df, h1_df)

print("feature rows:", len(feat_df))
print("columns:", list(feat_df.columns))
print("non-null d1_close:", feat_df["d1_close"].notna().sum())
print("non-null h1_atr:", feat_df["h1_atr14_pct"].notna().sum())
print(feat_df.head(3).T)

feat_df.to_csv("data/features.csv", index=False)
print("saved data/features.csv")
