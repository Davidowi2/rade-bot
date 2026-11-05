"""Technical indicators (pure functions on pandas Series)."""
from __future__ import annotations

import numpy as np
import pandas as pd


def ema(series: pd.Series, span: int) -> pd.Series:
    return series.ewm(span=span, adjust=False).mean()


def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = (-delta).clip(lower=0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def true_range(high, low, close_prev) -> pd.Series:
    hl = high - low
    hc = (high - close_prev).abs()
    lc = (low - close_prev).abs()
    return pd.concat([hl, hc, lc], axis=1).max(axis=1)


def atr(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    tr = true_range(high, low, close.shift(1))
    return tr.ewm(alpha=1 / period, adjust=False).mean()


def rolling_percentile(series: pd.Series, window: int) -> pd.Series:
    """Percentile rank of the last value within the trailing window (0..1)."""
    def _pct(x):
        if len(x) < 2:
            return np.nan
        last = x.iloc[-1]
        return (x < last).sum() / (len(x) - 1)
    return series.rolling(window=window, min_periods=2).apply(_pct, raw=False)
