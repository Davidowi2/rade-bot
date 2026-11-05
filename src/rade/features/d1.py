"""D1 (daily) feature computation, leak-safe. Precomputes a feature table once per bars frame."""
from __future__ import annotations

import numpy as np
import pandas as pd

from .indicators import atr, ema, rsi, rolling_percentile

_CACHE: dict[int, pd.DataFrame] = {}


def _completed_d1_mask(bars: pd.DataFrame, post_ts: pd.Timestamp):
    cutoff_date = pd.Timestamp(post_ts).date()
    return pd.to_datetime(bars["ts"]).dt.date < cutoff_date


def _build_d1_table(bars: pd.DataFrame) -> pd.DataFrame:
    d = bars.copy().reset_index(drop=True)
    d["ts"] = pd.to_datetime(d["ts"], utc=True)
    close = d["close"].astype(float)
    high = d["high"].astype(float)
    low = d["low"].astype(float)

    ema20 = ema(close, 20)
    ema50 = ema(close, 50)
    rsi14 = rsi(close, 14)
    atr14 = atr(high, low, close, 14)

    ema20_5ago = ema20.shift(5)
    ema50_5ago = ema50.shift(5)
    ema20_slope = (ema20 - ema20_5ago) / ema20_5ago.replace(0, np.nan)
    ema50_slope = (ema50 - ema50_5ago) / ema50_5ago.replace(0, np.nan)

    hh = (d["high"].diff() > 0).rolling(10, min_periods=2).sum()
    hl = (d["low"].diff() > 0).rolling(10, min_periods=2).sum()

    swing_high = high.rolling(20, min_periods=2).max()
    swing_low = low.rolling(20, min_periods=2).min()

    week_high = high.rolling(5, min_periods=2).max()
    week_low = low.rolling(5, min_periods=2).min()

    prev_high = high.shift(1)
    prev_low = low.shift(1)

    ranges = (high - low).astype(float)
    range_pctile = rolling_percentile(ranges, 252)

    last_range = (high - low).astype(float)
    range_pct = last_range / close

    nearest_round = (close / 50.0).round() * 50.0

    out = pd.DataFrame(index=d.index)
    out["ts"] = d["ts"]
    out["d1_close"] = close
    out["d1_ema20"] = ema20
    out["d1_ema50"] = ema50
    out["d1_ema20_slope"] = ema20_slope
    out["d1_ema50_slope"] = ema50_slope
    out["d1_ema_stack"] = (ema20 > ema50).astype(int)
    out["d1_close_vs_ema20_atr"] = (close - ema20) / atr14
    out["d1_rsi14"] = rsi14
    out["d1_atr14_pct"] = atr14 / close
    out["d1_range_pct"] = range_pct
    out["d1_range_percentile_252"] = range_pctile
    out["d1_hh_count_10"] = hh
    out["d1_hl_count_10"] = hl
    out["d1_dist_swing_high_atr"] = (swing_high - close) / atr14
    out["d1_dist_swing_low_atr"] = (close - swing_low) / atr14
    out["d1_dist_week_high_atr"] = (week_high - close) / atr14
    out["d1_dist_week_low_atr"] = (close - week_low) / atr14
    out["d1_dist_prev_high_atr"] = (prev_high - close) / atr14
    out["d1_dist_prev_low_atr"] = (close - prev_low) / atr14
    out["d1_dist_round50_atr"] = (close - nearest_round) / atr14
    return out


def _table(bars: pd.DataFrame) -> pd.DataFrame:
    key = id(bars)
    if key not in _CACHE:
        _CACHE[key] = _build_d1_table(bars)
    return _CACHE[key]


def d1_features(bars: pd.DataFrame, post_ts: pd.Timestamp) -> dict:
    tbl = _table(bars)
    mask = _completed_d1_mask(bars, post_ts)
    completed_idx = [i for i, m in enumerate(mask) if m]
    if len(completed_idx) < 60:
        return {}
    last_i = completed_idx[-1]
    row = tbl.iloc[last_i]
    if pd.isna(row["d1_atr14_pct"]) or row["d1_atr14_pct"] <= 0:
        return {}
    out = {}
    for k, v in row.items():
        if k == "ts":
            continue
        if isinstance(v, float) and pd.isna(v):
            out[k] = None
        else:
            out[k] = v
            # convert numpy types to python
            if hasattr(v, "item"):
                out[k] = v.item()
    return out


def _completed_d1(bars: pd.DataFrame, post_ts: pd.Timestamp) -> pd.DataFrame:
    mask = _completed_d1_mask(bars, post_ts)
    return bars[mask].copy().reset_index(drop=True)
