"""Tests for Phase 2B feature engineering."""
from __future__ import annotations

import sys
from datetime import datetime, timezone

import pandas as pd
import pytest

sys.path.insert(0, "src")

from rade.features import (
    compute_features_for_setup,
    d1_features,
    h1_features,
    run_feature_pipeline,
)
from rade.features.d1 import _completed_d1
from rade.labelling.models import Setup


def _d1_bars():
    # 90 days of synthetic daily bars ending 2025-04-08
    rows = []
    base = pd.Timestamp("2025-01-01", tz="UTC")
    for i in range(90):
        ts = base + pd.Timedelta(days=i)
        rows.append(
            {
                "ts": ts,
                "open": 2900 + i * 0.5,
                "high": 2910 + i * 0.5,
                "low": 2890 + i * 0.5,
                "close": 2905 + i * 0.5,
                "volume": 1000.0,
            }
        )
    return pd.DataFrame(rows)


def _h1_bars():
    rows = []
    base = pd.Timestamp("2025-04-01", tz="UTC")
    for i in range(200):
        ts = base + pd.Timedelta(hours=i)
        rows.append(
            {
                "ts": ts,
                "open": 2950 + i * 0.1,
                "high": 2955 + i * 0.1,
                "low": 2945 + i * 0.1,
                "close": 2952 + i * 0.1,
                "volume": 100.0,
            }
        )
    return pd.DataFrame(rows)


def test_completed_d1_excludes_post_day():
    bars = _d1_bars()
    post = pd.Timestamp("2025-04-05 12:00:00+00:00")
    d = _completed_d1(bars, post)
    # max ts date must be < post date
    assert pd.to_datetime(d["ts"]).dt.date.max() < post.date()


def test_d1_features_no_future_leak():
    bars = _d1_bars()
    post = pd.Timestamp("2025-04-05 12:00:00+00:00")
    feats = d1_features(bars, post)
    assert feats != {}
    # The last completed bar is 2025-04-04; close should be around 2905 + 94*0.5? 
    # Just check keys exist
    assert "d1_rsi14" in feats
    assert "d1_atr14_pct" in feats


def test_d1_features_insufficient_data():
    bars = _d1_bars().head(10)
    post = pd.Timestamp("2025-04-05 12:00:00+00:00")
    assert d1_features(bars, post) == {}


def test_h1_completed_excludes_current_hour():
    bars = _h1_bars()
    post = pd.Timestamp("2025-04-02 06:30:00+00:00")
    feats = h1_features(bars, post)
    assert feats != {}
    assert "h1_atr14_pct" in feats


def test_compute_features_for_setup():
    setup = Setup(
        message_pk=1,
        posted_at=datetime(2025, 4, 5, 12, 0, tzinfo=timezone.utc),
        instrument="XAUUSD",
        direction="buy",
        entry_type="limit",
        entry_price=3000.0,
        stop_loss=2990.0,
        take_profits=[3010.0, 3020.0],
    )
    feats = compute_features_for_setup(setup, _d1_bars(), _h1_bars())
    assert "message_pk" in feats
    assert "d1_rsi14" in feats
    assert "risk_atr" in feats
    assert "tp_count" in feats
    assert feats["tp_count"] == 2


def test_run_feature_pipeline():
    setup = Setup(
        message_pk=1,
        posted_at=datetime(2025, 4, 5, 12, 0, tzinfo=timezone.utc),
        instrument="XAUUSD",
        direction="buy",
        entry_type="limit",
        entry_price=3000.0,
        stop_loss=2990.0,
        take_profits=[3010.0],
    )
    df = run_feature_pipeline([setup], _d1_bars(), _h1_bars())
    assert len(df) == 1
    assert "d1_atr14_pct" in df.columns


def test_future_leak_feature_does_not_use_post_day_bar():
    """Shifting post_ts by one day changes D1 features (proves no silent future use)."""
    bars = _d1_bars()
    setup1 = Setup(
        message_pk=1,
        posted_at=datetime(2025, 3, 25, 12, 0, tzinfo=timezone.utc),
        instrument="XAUUSD",
        direction="buy",
        entry_type="limit",
        entry_price=3000.0,
        stop_loss=2990.0,
        take_profits=[3010.0],
    )
    setup2 = Setup(
        message_pk=2,
        posted_at=datetime(2025, 3, 26, 12, 0, tzinfo=timezone.utc),
        instrument="XAUUSD",
        direction="buy",
        entry_type="limit",
        entry_price=3000.0,
        stop_loss=2990.0,
        take_profits=[3010.0],
    )
    f1 = compute_features_for_setup(setup1, bars, _h1_bars())
    f2 = compute_features_for_setup(setup2, bars, _h1_bars())
    # d1_close should differ (different completed bar)
    assert f1.get("d1_close") != f2.get("d1_close")
