"""Run S0 copy-baseline backtest on the labelled set."""
from __future__ import annotations

import json
import sys
from datetime import timezone

import pandas as pd
from dotenv import load_dotenv

sys.path.insert(0, "src")
load_dotenv(".env")

from rade.backtest import BacktestEngine, CostModel
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
    if r.is_recap:
        continue
    if r.stop_loss is None or r.entry_price is None:
        continue
    tps = json.loads(r.take_profits) if r.take_profits else []
    posted_at = r.posted_at
    if posted_at is not None and getattr(posted_at, "tzinfo", None) is None:
        posted_at = pd.Timestamp(posted_at).tz_localize("UTC").to_pydatetime()
    s = Setup(
        message_pk=r.message_pk,
        posted_at=posted_at,
        instrument=r.instrument or "XAUUSD",
        direction=r.direction,
        entry_type=r.entry_type or "market",
        entry_price=r.entry_price,
        stop_loss=r.stop_loss,
        take_profits=tps,
    )
    setups.append(s)

print(f"setups: {len(setups)} (of {len(rows)} labelled)")

def run(label, slippage, move_sl_to_be, slippage_enabled=True):
    cm = CostModel(spread=0.25, slippage=slippage, slippage_enabled=slippage_enabled)
    eng = BacktestEngine(
        bars=bars,
        setups=setups,
        cost_model=cm,
        risk_pct=0.005,
        max_hold_hours=15.0,
        move_sl_to_be=move_sl_to_be,
    )
    res = eng.run()
    m = res.metrics()
    print(f"\n=== {label} ===")
    for k, v in m.items():
        print(f"  {k}: {v}")
    from collections import Counter
    print("  exit types:", dict(Counter(t.exit_type for t in res.trades)))
    return res

r0 = run("NO SLIPPAGE, no SL->BE", 0.0, False, slippage_enabled=False)
r1 = run("BASE: slip=0.25, no SL->BE", 0.25, False)
r2 = run("BASE: slip=0.25, WITH SL->BE", 0.25, True)
r3 = run("STRESS LOW: slip=0.125, no SL->BE", 0.125, False)
r4 = run("STRESS HIGH: slip=0.375, no SL->BE", 0.375, False)
PY