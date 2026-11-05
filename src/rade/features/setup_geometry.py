"""Setup-geometry features (normalized)."""
from __future__ import annotations

import math


def setup_geometry_features(setup, d1_atr: float) -> dict:
    if setup.stop_loss is None:
        return {
            "risk_atr": 0.0,
            "tp_count": len(setup.take_profits) if setup.take_profits else 0,
            "tp_dist_r_median": 0.0,
            "entry_type_market": int(setup.entry_type == "market"),
        }
    entry = float(setup.entry_price)
    sl = float(setup.stop_loss)
    risk = abs(entry - sl)
    tps = list(setup.take_profits) if setup.take_profits else []
    tp_count = len(tps)

    # TP distance in R (median)
    if tps and risk > 0:
        if setup.direction == "buy":
            tp_dists = [abs(t - entry) / risk for t in tps if t > entry]
        else:
            tp_dists = [abs(t - entry) / risk for t in tps if t < entry]
        tp_dist_r_median = float(sorted(tp_dists)[len(tp_dists) // 2]) if tp_dists else 0.0
    else:
        tp_dist_r_median = 0.0

    risk_atr = risk / d1_atr if d1_atr and d1_atr > 0 else 0.0

    return {
        "risk_atr": risk_atr,
        "tp_count": tp_count,
        "tp_dist_r_median": tp_dist_r_median,
        "entry_type_market": int(setup.entry_type == "market"),
    }
