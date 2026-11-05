"""Run Phase 2A labelling on real data and report metrics."""
from __future__ import annotations

import sys
from datetime import datetime

import pandas as pd
from sqlalchemy import select

sys.path.insert(0, "src")
from rade.labelling import run_pipeline
from rade.storage.db import make_session_factory
from rade.storage.models import Bar, RawTelegramMessage

sf = make_session_factory()
with sf() as session:
    raw_rows = [(m.id, m.posted_at, m.text) for m in session.query(RawTelegramMessage).all() if m.text]
    bars_df = pd.DataFrame(
        [
            {
                "ts": b.ts,
                "open": b.open,
                "high": b.high,
                "low": b.low,
                "close": b.close,
                "volume": b.volume,
            }
            for b in session.query(Bar).filter_by(timeframe="30m").all()
        ]
    )

bars_df["ts"] = pd.to_datetime(bars_df["ts"], utc=True)
bars_df = bars_df.sort_values("ts").reset_index(drop=True)

df = run_pipeline(raw_rows, bars_df)

# Metrics
total = len(df)
resolved = df[df["exit_type"].isin(["tp", "sl", "timeout"])]
tp = df[df["exit_type"] == "tp"]
ambig = df[df["ambiguity"] == True]
recap = df[df["is_recap"] == True]
unfilled = df[df["exit_type"] == "unfilled"]
invalid = df[df["exit_type"] == "invalid"]

print(f"total setups:        {total}")
print(f"recaps:              {len(recap)} ({100*len(recap)/total:.1f}%)")
print(f"invalid (no SL):     {len(invalid)}")
print(f"resolved:            {len(resolved)} ({100*len(resolved)/total:.1f}%)")
print(f"  tp:                {len(tp)} ({100*len(tp)/len(resolved):.1f}%)")
print(f"  sl:                {len(df[df['exit_type']=='sl'])}")
print(f"  timeout:           {len(df[df['exit_type']=='timeout'])}")
print(f"unfilled:            {len(unfilled)}")
print(f"ambiguity:           {len(ambig)} ({100*len(ambig)/len(resolved):.1f}% of resolved)")
print(f"R median:            {resolved['R'].median():.3f}")
print(f"R mean:              {resolved['R'].mean():.3f}")
print(f"hours_held median:   {resolved['hours_held'].median():.2f}")
print()
print("Direction mix:")
print(df["direction"].value_counts().to_string())
print()
print("Entry type mix:")
print(df["entry_type"].value_counts().to_string())

# Save to CSV for inspection
out = "data/labeled_setups.csv"
df.to_csv(out, index=False)
print(f"\nSaved {len(df)} rows to {out}")
