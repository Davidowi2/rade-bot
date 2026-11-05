"""Compute forward returns for entry-quality analysis (no exit simulation)."""
from __future__ import annotations

import json
import sys
from datetime import timedelta

import numpy as np
import pandas as pd

sys.path.insert(0, "src")

# Load data
ls = pd.read_csv("data/labeled_setups.csv")
ls["posted_at"] = pd.to_datetime(ls["posted_at"], utc=True)
ls["fill_ts"] = pd.to_datetime(ls["fill_ts"], utc=True, errors="coerce")

feat = pd.read_csv("data/features.csv")
feat["posted_at"] = pd.to_datetime(feat["posted_at"], utc=True)

# Load bars from DB
from rade.storage.db import make_session_factory
from rade.storage.models import Bar

sf = make_session_factory()
with sf() as session:
    bars = pd.DataFrame([
        {"ts": b.ts, "open": b.open, "high": b.high, "low": b.low, "close": b.close}
        for b in session.query(Bar).filter_by(timeframe="30m").all()
    ])
bars["ts"] = pd.to_datetime(bars["ts"], utc=True)
bars = bars.sort_values("ts").reset_index(drop=True)

# Bar lookup by timestamp
bar_by_ts = {row["ts"]: i for i, row in bars.iterrows()}

# Only filled, non-recap setups
filled = ls[(ls["fill_status"] == "filled") & (ls["is_recap"] == False)].copy()
print(f"Filled non-recap setups: {len(filled)}")

# Merge features (for ATR normalization)
# Merge features (for ATR normalization and pattern features)
feat_cols = [c for c in feat.columns if c != "posted_at"]
filled = filled.merge(feat[feat_cols], on="message_pk", how="left")

# Direction sign
filled["dir_sign"] = filled["direction"].map({"buy": 1, "sell": -1})

# Entry reference price: published entry_price (limit price for LIMIT, published for NOW)
filled["entry_ref"] = filled["entry_price"]

# Fill bar index
filled["fill_bar_idx"] = filled["fill_ts"].map(lambda ts: bar_by_ts.get(ts, None))

# Horizon bars (30-min bars): 1h=2, 2h=4, 4h=8, 8h=16, 15h=30
HORIZONS = {"1h": 2, "2h": 4, "4h": 8, "8h": 16, "15h": 30}

rows = []
for _, s in filled.iterrows():
    fbi = s["fill_bar_idx"]
    if fbi is None or pd.isna(fbi):
        continue

    entry_ref = s["entry_ref"]
    sign = s["dir_sign"]
    stop_loss = s["stop_loss"]
    tp_list = json.loads(s["take_profits"]) if isinstance(s["take_profits"], str) else (s["take_profits"] if isinstance(s["take_profits"], list) else [])

    # Stop distance (published)
    stop_dist = abs(entry_ref - stop_loss) if stop_loss is not None and not pd.isna(stop_loss) else None

    # ATR
    atr_pct = s["d1_atr14_pct"] if not pd.isna(s["d1_atr14_pct"]) else None
    atr_price = entry_ref * atr_pct if atr_pct is not None and atr_pct > 0 else None

    # Forward returns: anchor to first fully post-fill bar (fill_bar + 1)
    # Horizon h = fill_bar + 1 + horizon_bars
    result = {
        "message_pk": s["message_pk"],
        "posted_at": s["posted_at"],
        "fill_ts": s["fill_ts"],
        "direction": s["direction"],
        "entry_type": s["entry_type"],
        "entry_ref": entry_ref,
        "fill_price": s["fill_price"],
        "stop_loss": stop_loss,
        "take_profits": s["take_profits"],
        "d1_atr14_pct": atr_pct,
        "fill_bar_ambiguous": True,  # fill bar excluded from MFE/MAE by default
    }

    for hname, hbars in HORIZONS.items():
        target_idx = int(fbi) + 1 + hbars  # first post-fill bar + horizon
        if target_idx < len(bars):
            close = bars.loc[target_idx, "close"]
            fwd = (close - entry_ref) * sign
            result[f"fwd_ret_{hname}"] = round(float(fwd), 4)
            if atr_price and atr_price > 0:
                result[f"fwd_ret_{hname}_atr"] = round(float(fwd / atr_price), 4)
            else:
                result[f"fwd_ret_{hname}_atr"] = None
            if stop_dist and stop_dist > 0:
                result[f"fwd_ret_{hname}_r"] = round(float(fwd / stop_dist), 4)
            else:
                result[f"fwd_ret_{hname}_r"] = None
        else:
            result[f"fwd_ret_{hname}"] = None
            result[f"fwd_ret_{hname}_atr"] = None
            result[f"fwd_ret_{hname}_r"] = None

    # MFE/MAE: exclude fill bar (primary), use bars fill+1 to fill+30
    window_start = int(fbi) + 1
    window_end = min(int(fbi) + 31, len(bars))  # 30 bars = 15h
    window_bars = bars.iloc[window_start:window_end]

    if len(window_bars) > 0:
        if sign == 1:  # buy
            mfe = window_bars["high"].max() - entry_ref
            mae = entry_ref - window_bars["low"].min()
        else:  # sell
            mfe = entry_ref - window_bars["low"].min()
            mae = window_bars["high"].max() - entry_ref

        result["mfe_15h"] = round(float(mfe), 4)
        result["mae_15h"] = round(float(mae), 4)
        if atr_price and atr_price > 0:
            result["mfe_15h_atr"] = round(float(mfe / atr_price), 4)
            result["mae_15h_atr"] = round(float(mae / atr_price), 4)
        else:
            result["mfe_15h_atr"] = None
            result["mae_15h_atr"] = None
    else:
        result["mfe_15h"] = None
        result["mae_15h"] = None
        result["mfe_15h_atr"] = None
        result["mae_15h_atr"] = None

    # Sensitivity: MFE/MAE including fill bar
    window_incl = bars.iloc[int(fbi):window_end]
    if len(window_incl) > 0:
        if sign == 1:
            mfe_incl = window_incl["high"].max() - entry_ref
            mae_incl = entry_ref - window_incl["low"].min()
        else:
            mfe_incl = entry_ref - window_incl["low"].min()
            mae_incl = window_incl["high"].max() - entry_ref
        result["mfe_15h_incl_fillbar"] = round(float(mfe_incl), 4)
        result["mae_15h_incl_fillbar"] = round(float(mae_incl), 4)

    # Descriptive: did price touch SL or any TP within 15h? (reference only)
    if window_bars is not None and len(window_bars) > 0:
        if sign == 1:  # buy
            min_low = window_bars["low"].min()
            max_high = window_bars["high"].max()
            result["hit_sl_15h"] = bool(min_low <= stop_loss) if stop_loss is not None else None
            result["hit_tp1_15h"] = bool(max_high >= tp_list[0]) if tp_list else None
        else:  # sell
            max_high = window_bars["high"].max()
            min_low = window_bars["low"].min()
            result["hit_sl_15h"] = bool(max_high >= stop_loss) if stop_loss is not None else None
            result["hit_tp1_15h"] = bool(min_low <= tp_list[0]) if tp_list else None
    else:
        result["hit_sl_15h"] = None
        result["hit_tp1_15h"] = None

    # Entry type classification
    result["entry_type_market"] = s.get("entry_type_market", None)

    # Pre-entry features (from feat merge)
    for col in ["utc_hour", "weekday", "d1_atr14_pct", "d1_range_percentile_252",
                "d1_ema_stack", "d1_close_vs_ema20_atr", "d1_rsi14",
                "d1_dist_week_high_atr", "d1_dist_week_low_atr",
                "d1_dist_prev_high_atr", "d1_dist_prev_low_atr",
                "h1_atr14_pct", "risk_atr", "tp_count", "tp_dist_r_median",
                "minutes_since_d1_open"]:
        result[col] = s.get(col, None)

    rows.append(result)

out = pd.DataFrame(rows)
out.to_csv("data/forward_returns.csv", index=False)
print(f"Saved {len(out)} rows to data/forward_returns.csv")
print(f"Columns: {len(out.columns)}")
print(f"Entry types: {out['entry_type'].value_counts().to_dict()}")
print(f"Directions: {out['direction'].value_counts().to_dict()}")
