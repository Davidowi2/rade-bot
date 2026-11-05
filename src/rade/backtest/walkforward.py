"""Walk-forward split with purge for Phase 2C."""
from __future__ import annotations

from datetime import timedelta

import pandas as pd


def _get_ts(x, attr):
    if isinstance(x, dict):
        v = x[attr]
    else:
        v = getattr(x, attr)
    return pd.Timestamp(v)

def chronological_split(
    items: list,
    ts_attr: str = "entry_ts",
    train_frac: float = 0.6,
    val_frac: float = 0.2,
    purge_hours: float = 39.0,
) -> dict:
    if not items:
        return {"train": [], "val": [], "test": []}
    sorted_items = sorted(items, key=lambda x: _get_ts(x, ts_attr))
    n = len(sorted_items)
    i_train = int(n * train_frac)
    i_val = int(n * (train_frac + val_frac))

    train = sorted_items[:i_train]
    val = sorted_items[i_train:i_val]
    test = sorted_items[i_val:]

    if train and val:
        boundary = _get_ts(val[0], ts_attr)
        purge_cutoff = boundary - pd.Timedelta(hours=purge_hours)
        train = [x for x in train if _get_ts(x, ts_attr) < purge_cutoff]

    if val and test:
        boundary2 = _get_ts(test[0], ts_attr)
        purge_cutoff2 = boundary2 - pd.Timedelta(hours=purge_hours)
        val = [x for x in val if _get_ts(x, ts_attr) < purge_cutoff2]

    return {"train": train, "val": val, "test": test}


def aggregate_by_setup(trades: list) -> list:
    """Aggregate partial-exit trade legs by setup_id into one record per setup."""
    from collections import defaultdict

    groups = defaultdict(list)
    for t in trades:
        groups[t.setup_id].append(t)

    out = []
    for sid, legs in groups.items():
        legs_sorted = sorted(legs, key=lambda t: t.exit_ts)
        total_pnl = sum(l.pnl for l in legs)
        total_R = sum(l.R for l in legs)  # note: R is per-leg risk; summing is approximate
        # better: total_R = total_pnl / risk_dollars (risk is same for all legs of a setup)
        risk = legs[0].risk_dollars if legs else 0.0
        total_R = total_pnl / risk if risk > 0 else 0.0
        exit_types = [l.exit_type for l in legs]
        # final exit type = last leg's type
        final_exit = legs_sorted[-1].exit_type
        entry_ts = legs[0].entry_ts
        exit_ts = legs_sorted[-1].exit_ts
        hours = (pd.Timestamp(exit_ts) - pd.Timestamp(entry_ts)).total_seconds() / 3600.0
        side = legs[0].side
        entry_price = legs[0].entry_price
        # win if total pnl > 0
        out.append(
            {
                "setup_id": sid,
                "side": side,
                "entry_price": entry_price,
                "entry_ts": entry_ts,
                "exit_ts": exit_ts,
                "exit_type": final_exit,
                "exit_types": exit_types,
                "n_legs": len(legs),
                "total_pnl": total_pnl,
                "total_R": total_R,
                "hours_held": hours,
                "risk_dollars": risk,
            }
        )
    return out
