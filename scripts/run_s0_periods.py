"""Setup-level per-period breakdown of S0 (base case)."""
from __future__ import annotations

import json
import sys
from collections import Counter

import pandas as pd
from dotenv import load_dotenv

sys.path.insert(0, "src")
load_dotenv(".env")

from rade.backtest import BacktestEngine, CostModel
from rade.backtest.walkforward import aggregate_by_setup, chronological_split
from rade.labelling.models import Setup
from rade.storage.db import make_session_factory
from rade.storage.models import Bar, LabeledSetup

sf = make_session_factory()
with sf() as session:
    bars = pd.DataFrame(
        [{"ts": b.ts, "open": b.open, "high": b.high, "low": b.low, "close": b.close, "volume": b.volume}
         for b in session.query(Bar).filter_by(timeframe="30m").all()]
    )
    rows = session.query(LabeledSetup).all()

bars["ts"] = pd.to_datetime(bars["ts"], utc=True)
bars = bars.sort_values("ts").reset_index(drop=True)

setups = []
for r in rows:
    if r.is_recap or r.stop_loss is None or r.entry_price is None:
        continue
    tps = json.loads(r.take_profits) if r.take_profits else []
    posted_at = r.posted_at
    if posted_at is not None and getattr(posted_at, "tzinfo", None) is None:
        posted_at = pd.Timestamp(posted_at).tz_localize("UTC").to_pydatetime()
    setups.append(Setup(message_pk=r.message_pk, posted_at=posted_at, instrument=r.instrument or "XAUUSD",
                        direction=r.direction, entry_type=r.entry_type or "market",
                        entry_price=r.entry_price, stop_loss=r.stop_loss, take_profits=tps))

cm = CostModel(spread=0.25, slippage=0.25, slippage_enabled=True)
eng = BacktestEngine(bars=bars, setups=setups, cost_model=cm, risk_pct=0.005,
                     max_hold_hours=15.0, move_sl_to_be=False)
res = eng.run()

# Aggregate partial exits by setup
agg = aggregate_by_setup(res.trades)
print(f"Setups traded: {len(agg)} (from {len(setups)} setups, {len(res.trades)} legs)")

def period_metrics(items, label):
    if not items:
        print(f"  {label}: no data"); return
    pnls = [x["total_pnl"] for x in items]
    Rs = [x["total_R"] for x in items]
    wins = [r for r in Rs if r > 0]
    import numpy as np
    gross_p = sum(p for p in pnls if p > 0)
    gross_l = abs(sum(p for p in pnls if p < 0))
    pf = gross_p / gross_l if gross_l > 0 else float("inf")
    print(f"  {label}: n={len(items)} win%={100*len(wins)/len(items):.1f} PF={pf:.3f} "
          f"exp_R={sum(Rs)/len(Rs):.4f} total_R={sum(Rs):.1f} "
          f"avg_hold={sum(x['hours_held'] for x in items)/len(items):.2f}h")

split = chronological_split(agg, ts_attr="entry_ts", train_frac=0.6, val_frac=0.2, purge_hours=39.0)
print("\n=== Walk-forward split (purge=39h) ===")
for name in ("train", "val", "test"):
    items = split[name]
    period_metrics(items, name)
    if items:
        print(f"    date range: {min(x['entry_ts'] for x in items)} .. {max(x['entry_ts'] for x in items)}")

# Full sample
print("\n=== Full sample (setup-level) ===")
period_metrics(agg, "all")

# Exit type distribution (final exit)
from collections import Counter
print("Final exit types:", dict(Counter(x["exit_type"] for x in agg)))
