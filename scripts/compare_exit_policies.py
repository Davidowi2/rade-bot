"""Compare hold-to-final exit policies: stop to TP1 vs stop to breakeven after TP1.

The simulation is a pure function of ``(setup, policy, bars)`` so it can be
tested: a stopped-out long must score a loss, not a win. That sign error was
made once and reversed the A1 decision; the test exists so it cannot come back.
"""
from __future__ import annotations

import json
import sys

import pandas as pd

sys.path.insert(0, "src")


def norm_ladder(direction, entry, tps):
    on_side = [t for t in tps if (t > entry if direction == "buy" else t < entry)]
    seen, uniq = set(), []
    for t in on_side:
        if t not in seen:
            seen.add(t)
            uniq.append(t)
    return sorted(uniq, key=(lambda t: t - entry) if direction == "buy" else (lambda t: entry - t))


def simulate(row, policy, bars):
    """Score one setup under a policy. Returns ``(R, exit_reason)`` or ``None``.

    ``R`` is profit divided by the initial stop distance, signed the way a
    broker signs it: a long that is stopped out is **negative**.
    """
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

    ts_list = bars["ts"].tolist()
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
            # SL and TP1 in the same bar: SL wins the tie (pessimistic).
            sl_hit = (low <= stop) if buy else (high >= stop)
            tp1_reached = (high >= tp1) if buy else (low <= tp1)
            if sl_hit:
                pnl = (stop - entry) if buy else (entry - stop)
                exit_reason = "sl_initial"
                break
            if tp1_reached:
                tp1_hit = True
                stop = tp1 if policy == "tp1" else entry
        else:
            sl_hit = (low <= stop) if buy else (high >= stop)
            tp_final = (high >= final_tp) if buy else (low <= final_tp)
            if sl_hit:
                pnl = (stop - entry) if buy else (entry - stop)
                exit_reason = "stop_after_tp1"
                break
            if tp_final:
                pnl = (final_tp - entry) if buy else (entry - final_tp)
                exit_reason = "final_tp"
                break

    if pnl is None:
        j = min(idx + max_bars - 1, len(bars) - 1)
        close = float(bars.loc[j, "close"])
        pnl = (close - entry) if buy else (entry - close)
        exit_reason = "timeout"

    return pnl / sl_dist, exit_reason


def main() -> int:
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

    ls = pd.read_csv("data/labeled_setups.csv")
    ls["fill_ts"] = pd.to_datetime(ls["fill_ts"], utc=True, errors="coerce")
    f = ls[(ls["fill_status"] == "filled") & (~ls["is_recap"])].copy()
    f = f[f["fill_ts"].notna() & f["stop_loss"].notna()].reset_index(drop=True)
    print(f"Setups: {len(f)}")

    for policy in ("tp1", "breakeven"):
        rs, reasons = [], {}
        for _, row in f.iterrows():
            r = simulate(row, policy, bars)
            if r is None:
                continue
            rs.append(r[0])
            reasons[r[1]] = reasons.get(r[1], 0) + 1
        rs = pd.Series(rs)
        print(f"\n=== Policy: stop to {'TP1' if policy == 'tp1' else 'BREAKEVEN'} after TP1 ===")
        print(f"  n={len(rs)}  mean R={rs.mean():.4f}  median R={rs.median():.4f}")
        print(f"  win rate={(rs > 0).mean():.3f}  total R={rs.sum():.1f}")
        print(f"  exits: {reasons}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
