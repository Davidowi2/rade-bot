"""Context (time-of-day) features."""
from __future__ import annotations

import pandas as pd


def context_features(post_ts: pd.Timestamp) -> dict:
    ts = pd.Timestamp(post_ts)
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    return {
        "utc_hour": int(ts.hour),
        "weekday": int(ts.weekday()),
        "minutes_since_d1_open": int(ts.hour * 60 + ts.minute),
    }
