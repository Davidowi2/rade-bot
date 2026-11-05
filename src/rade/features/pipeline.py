"""Feature engineering pipeline for Phase 2B."""
from __future__ import annotations

import pandas as pd

from .context import context_features
from .d1 import d1_features
from .h1 import h1_features
from .setup_geometry import setup_geometry_features


def compute_features_for_setup(setup, d1_bars: pd.DataFrame, h1_bars: pd.DataFrame) -> dict:
    post_ts = pd.Timestamp(setup.posted_at)
    if post_ts.tzinfo is None:
        post_ts = post_ts.tz_localize("UTC")

    d1 = d1_features(d1_bars, post_ts)
    if not d1:
        return {"message_pk": setup.message_pk}

    h1 = h1_features(h1_bars, post_ts)
    ctx = context_features(post_ts)
    last_atr_dollars = d1["d1_atr14_pct"] * d1["d1_close"]
    geo = setup_geometry_features(setup, last_atr_dollars)

    feats = {"message_pk": setup.message_pk, "posted_at": setup.posted_at}
    feats.update(d1)
    feats.update(h1)
    feats.update(ctx)
    feats.update(geo)
    return feats


def run_feature_pipeline(setups, d1_bars: pd.DataFrame, h1_bars: pd.DataFrame) -> pd.DataFrame:
    rows = [compute_features_for_setup(s, d1_bars, h1_bars) for s in setups]
    return pd.DataFrame(rows)
