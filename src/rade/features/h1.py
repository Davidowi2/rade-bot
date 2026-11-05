"""1H feature computation, leak-safe. Precomputes ATR once per bars frame."""
from __future__ import annotations

import pandas as pd

from .indicators import atr

_CACHE: dict[int, pd.DataFrame] = {}


def _completed_h1_mask(bars: pd.DataFrame, post_ts: pd.Timestamp):
    ts = pd.to_datetime(bars["ts"], utc=True)
    return (ts + pd.Timedelta(hours=1)) <= pd.Timestamp(post_ts)


def _build_h1_table(bars: pd.DataFrame) -> pd.DataFrame:
    d = bars.copy().reset_index(drop=True)
    d["ts"] = pd.to_datetime(d["ts"], utc=True)
    close = d["close"].astype(float)
    high = d["high"].astype(float)
    low = d["low"].astype(float)
    atr14 = atr(high, low, close, 14)
    out = pd.DataFrame(index=d.index)
    out["ts"] = d["ts"]
    out["close"] = close
    out["high"] = high
    out["low"] = low
    out["h1_atr14"] = atr14
    return out


def _table(bars: pd.DataFrame) -> pd.DataFrame:
    key = id(bars)
    if key not in _CACHE:
        _CACHE[key] = _build_h1_table(bars)
    return _CACHE[key]


def h1_features(bars: pd.DataFrame, post_ts: pd.Timestamp) -> dict:
    tbl = _table(bars)
    mask = _completed_h1_mask(bars, post_ts)
    completed_idx = [i for i, m in enumerate(mask) if m]
    if len(completed_idx) < 20:
        return {}
    last_i = completed_idx[-1]
    last_atr = float(tbl["h1_atr14"].iloc[last_i])
    last_close = float(tbl["close"].iloc[last_i])
    if last_atr <= 0 or last_close <= 0:
        return {}

    post_date = pd.Timestamp(post_ts).date()
    completed = tbl.iloc[completed_idx]
    prev_day = completed[pd.to_datetime(completed["ts"]).dt.date < post_date]
    if len(prev_day) > 0:
        sess_high = float(prev_day["high"].max())
        sess_low = float(prev_day["low"].min())
        dist_sess_high_atr = (sess_high - last_close) / last_atr
        dist_sess_low_atr = (last_close - sess_low) / last_atr
    else:
        dist_sess_high_atr = 0.0
        dist_sess_low_atr = 0.0

    return {
        "h1_atr14_pct": last_atr / last_close,
        "h1_dist_prev_session_high_atr": dist_sess_high_atr,
        "h1_dist_prev_session_low_atr": dist_sess_low_atr,
    }
