"""Compare hold-to-final exit policies: stop to TP1 vs stop to breakeven after TP1."""
from __future__ import annotations

import json
import sys
from datetime import timedelta

import pandas as pd

sys.path.insert(0, "src")

from rade.storage.db import make_session_factory
from rade.storage.models import Bar

sf = make_session_factory()
with sf() as s:
    bars = pd.DataFrame([
        {"ts": b.ts, "high": b.high, "low": b.low, "close": b.close}
        for b in s.query(Bar).filter_by(timeframe="30m").all()
    ])
bars["ts"] = pd.to_datetime(bars["ts"], utc=True)
bars = bars.sort_values("ts").reset_index(drop=True)
ts_list = bars["ts"].tolist()

ls = pd.read_csv("data/labeled_setups.csv")
ls["fill_ts"] = pd.to_datetime(ls["fill_ts"], utc=True, errors="coerce")
f = ls[(ls["fill_status"] == "filled") & (~ls["is_recap"])].copy()
f = f[f["fill_ts"].notna() & f["stop_loss"].notna()].reset_index(drop=True)
print(f"Setups: {len(f)}")


def norm_ladder(direction, entry, tps):
    on_side = [t for t in tps if (t > entry if direction == "buy" else t < entry)]
    seen, uniq = set(), []
    for t in on_side:
        if t not in seen:
            seen.add(t)
            uniq.append(t)
    return sorted(uniq, key=(lambda t: t - entry) if direction == "buy" else (lambda t: entry - t))


def simulate(row, policy):
    direction = row["direction"]
    entry = float(row["entry_price"])
    sl0 = float(row["stop_loss"])
    tps = norm_ladder(direction, entry, json.loads(row["take_profits"]))
    if not tps:
        return None
    final_tp = tps[-1]
    tp1 = tps[0]
    buy = direction == "buy"
    sl_dist = abs(entry - sl0)
    if sl_dist <= 0:
        return None

    # find fill bar index
    idx = None
    for i, ts in enumerate(ts_list):
        if ts >= row["fill_ts"]:
            idx = i
            break
    if idx is None:
        return None

    stop = sl0
    tp1_hit = False
    pnl = None
    exit_reason = None
    max_bars = 30  # 15h

    for j in range(idx, min(idx + max_bars, len(bars))):
        low = float(bars.loc[j, "low"])
        high = float(bars.loc[j, "high"])

        if not tp1_hit:
            # check TP1 and SL in the same bar; SL wins ties (pessimistic)
            sl_hit = (low <= stop) if buy else (high >= stop)
            tp1_reached = (high >= tp1) if buy else (low <= tp1)
            if sl_hit:
                pnl = (entry - stop) if buy else (stop - entry)
                exit_reason = "sl_initial"
                break
            if tp1_reached:
                tp1_hit = True
                stop = tp1 if policy == "tp1" else entry
        else:
            # after TP1: check final target and the moved stop
            sl_hit = (low <= stop) if buy else (high >= stop)
            tp_final = (high >= final_tp) if buy else (low <= final_tp)
            if sl_hit:
                pnl = (entry - stop) if buy else (stop - entry)
                exit_reason = "stop_after_tp1"
                break
            if tp_final:
                pnl = (final_tp - entry) if buy else (entry - final_tp)
                exit_reason = "final_tp"
                break

    if pnl is None:
        # timeout at 15h: mark to market at last close
        j = min(idx + max_bars - 1, len(bars) - 1)
        close = float(bars.loc[j, "close"])
        pnl = (close - entry) if buy else (entry - close)
        exit_reason = "timeout"

    return pnl / sl_dist, exit_reason


for policy in ("tp1", "breakeven"):
    rs, reasons = [], {}
    for _, row in f.iterrows():
        r = simulate(row, policy)
        if r is None:
            continue
        rs.append(r[0])
        reasons[r[1]] = reasons.get(r[1], 0) + 1
    rs = pd.Series(rs)
    print(f"\n=== Policy: stop to {'TP1' if policy == 'tp1' else 'BREAKEVEN'} after TP1 ===")
    print(f"  n={len(rs)}  mean R={rs.mean():.4f}  median R={rs.median():.4f}")
    print(f"  win rate={(rs > 0).mean():.3f}  total R={rs.sum():.1f}")
    print(f"  exits: {reasons}")
