"""Reconciled S0 report: counts, setup-level aggregation, posted_at split, 39h purge."""
from __future__ import annotations

import json
import sys
from collections import Counter, defaultdict

import numpy as np
import pandas as pd
from dotenv import load_dotenv

sys.path.insert(0, "src")
load_dotenv(".env")

from rade.backtest import BacktestEngine, CostModel
from rade.backtest.walkforward import chronological_split
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

# --- classify setups ---
total_labelled = len(rows)
recap_count = sum(1 for r in rows if r.is_recap)
invalid_count = sum(1 for r in rows if r.stop_loss is None or r.entry_price is None)
eligible = [r for r in rows if not r.is_recap and r.stop_loss is not None and r.entry_price is not None]

print("=" * 60)
print("S0 COUNT RECONCILIATION")
print("=" * 60)
print(f"Total labelled setups:     {total_labelled}")
print(f"  recaps (excluded):       {recap_count}")
print(f"  invalid (no SL):         {invalid_count}")
print(f"  eligible (replayed):     {len(eligible)}")

# posted_at map
posted_at_map = {}
for r in rows:
    pa = r.posted_at
    if pa is not None and getattr(pa, "tzinfo", None) is None:
        pa = pd.Timestamp(pa).tz_localize("UTC").to_pydatetime()
    posted_at_map[r.message_pk] = pa

setups = []
for r in eligible:
    tps = json.loads(r.take_profits) if r.take_profits else []
    posted_at = posted_at_map[r.message_pk]
    setups.append(Setup(message_pk=r.message_pk, posted_at=posted_at,
                        instrument=r.instrument or "XAUUSD", direction=r.direction,
                        entry_type=r.entry_type or "market", entry_price=r.entry_price,
                        stop_loss=r.stop_loss, take_profits=tps))

cm = CostModel(spread=0.25, slippage=0.25, slippage_enabled=True)
eng = BacktestEngine(bars=bars, setups=setups, cost_model=cm, risk_pct=0.005,
                     max_hold_hours=15.0, move_sl_to_be=False)
res = eng.run()

# --- aggregate by setup ---
groups = defaultdict(list)
for t in res.trades:
    groups[t.setup_id].append(t)

agg = []
unfilled_setups = [s for s in setups if s.message_pk not in groups]
for sid, legs in groups.items():
    legs_sorted = sorted(legs, key=lambda t: t.exit_ts)
    total_pnl = sum(l.pnl for l in legs)
    risk = legs[0].risk_dollars if legs else 0.0
    total_R = total_pnl / risk if risk > 0 else 0.0
    exit_ts = legs_sorted[-1].exit_ts
    entry_ts = legs[0].entry_ts
    hours = (pd.Timestamp(exit_ts) - pd.Timestamp(entry_ts)).total_seconds() / 3600.0
    agg.append({
        "setup_id": sid,
        "side": legs[0].side,
        "entry_ts": entry_ts,
        "posted_at": posted_at_map.get(sid),
        "exit_ts": exit_ts,
        "exit_type": legs_sorted[-1].exit_type,
        "n_legs": len(legs),
        "total_pnl": total_pnl,
        "total_R": total_R,
        "hours_held": hours,
        "risk_dollars": risk,
    })

print(f"\nFilled setups (>=1 leg): {len(agg)}")
print(f"Unfilled setups:        {len(unfilled_setups)}")
print(f"Total legs:             {len(res.trades)}")
print(f"Reconciled: {len(agg)} filled + {len(unfilled_setups)} unfilled = {len(agg)+len(unfilled_setups)} (eligible {len(eligible)})")

# --- split by posted_at ---
split = chronological_split(agg, ts_attr="posted_at", train_frac=0.6, val_frac=0.2, purge_hours=39.0)
print("\n" + "=" * 60)
print("WALK-FORWARD SPLIT (by posted_at, purge=39h)")
print("=" * 60)

def period_metrics(items, label):
    if not items:
        print(f"  {label}: no data"); return
    pnls = [x["total_pnl"] for x in items]
    Rs = [x["total_R"] for x in items]
    wins = [r for r in Rs if r > 0]
    gross_p = sum(p for p in pnls if p > 0)
    gross_l = abs(sum(p for p in pnls if p < 0))
    pf = gross_p / gross_l if gross_l > 0 else float("inf")
    print(f"  {label}: n={len(items)} win%={100*len(wins)/len(items):.1f} PF={pf:.3f} "
          f"exp_R={sum(Rs)/len(Rs):.4f} total_R={sum(Rs):.1f} "
          f"avg_hold={sum(x['hours_held'] for x in items)/len(items):.2f}h")
    if items:
        print(f"    posted_at range: {min(x['posted_at'] for x in items)} .. {max(x['posted_at'] for x in items)}")

purged = len(agg) - sum(len(split[k]) for k in ("train","val","test"))
for name in ("train", "val", "test"):
    period_metrics(split[name], name)
print(f"\n  purged (39h): {purged} setups removed near boundaries")
print(f"  total in split: {sum(len(split[k]) for k in ('train','val','test'))}")

print("\n=== Full sample (setup-level) ===")
period_metrics(agg, "all")
from collections import Counter
print("Final exit types:", dict(Counter(x["exit_type"] for x in agg)))

# --- save report ---
report_df = pd.DataFrame(agg)
report_df.to_csv("data/s0_report.csv", index=False)
print("\nSaved data/s0_report.csv")

# Save split boundaries
import json as _json
boundaries = {}
for name in ("train", "val", "test"):
    items = split[name]
    boundaries[name] = {
        "n": len(items),
        "posted_at_min": str(min(x["posted_at"] for x in items)) if items else None,
        "posted_at_max": str(max(x["posted_at"] for x in items)) if items else None,
        "win_pct": round(100*sum(1 for x in items if x["total_R"]>0)/len(items), 1) if items else 0,
        "exp_R": round(sum(x["total_R"] for x in items)/len(items), 4) if items else 0,
        "total_R": round(sum(x["total_R"] for x in items), 1) if items else 0,
    }
boundaries["purged"] = purged
boundaries["unfilled"] = len(unfilled_setups)
with open("data/s0_split.json", "w") as f:
    _json.dump(boundaries, f, indent=2)
print("Saved data/s0_split.json")
