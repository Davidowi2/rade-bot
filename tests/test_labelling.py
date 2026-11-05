"""Tests for the Phase 2A labelling engine."""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, "src")

from rade.labelling import (
    extract_setups_from_text,
    label_one,
    run_pipeline,
)
from rade.labelling.models import Setup


def _bars(rows):
    """Build a bars DataFrame from list of (ts_str, o,h,l,c)."""
    df = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close"])
    df["ts"] = pd.to_datetime(df["ts"], utc=True)
    df["volume"] = 1.0
    return df.sort_values("ts").reset_index(drop=True)


# ---------- setup extractor ----------

def test_extractor_full_setup():
    text = "XAUUSD SELL NOW 2982\nSl : 2995\nTp : 2974\nTp : 2965"
    setups = extract_setups_from_text(text, 1, datetime(2025, 4, 9, 21, 21, tzinfo=timezone.utc))
    assert len(setups) == 1
    s = setups[0]
    assert s.direction == "sell"
    assert s.entry_type == "market"
    assert s.entry_price == 2982.0
    assert s.stop_loss == 2995.0
    assert s.take_profits == [2974.0, 2965.0]


def test_extractor_limit():
    text = "XAUUSD BUY LIMIT 2950\nSl : 2930\nTp : 2980"
    setups = extract_setups_from_text(text, 2, datetime(2025, 4, 9, 21, 21, tzinfo=timezone.utc))
    assert setups[0].entry_type == "limit"
    assert setups[0].direction == "buy"


def test_extractor_no_entry_returns_empty():
    assert extract_setups_from_text("gold is going up", 3, datetime(2025, 4, 9, tzinfo=timezone.utc)) == []


def test_extractor_empty():
    assert extract_setups_from_text(None, 4, datetime(2025, 4, 9, tzinfo=timezone.utc)) == []
    assert extract_setups_from_text("", 5, datetime(2025, 4, 9, tzinfo=timezone.utc)) == []


def test_extractor_tp_open_ignored():
    text = "XAUUSD BUY NOW 3000\nSl : 2990\nTp : 3010\nTp : open"
    setups = extract_setups_from_text(text, 6, datetime(2025, 4, 9, tzinfo=timezone.utc))
    assert setups[0].take_profits == [3010.0]


def test_extractor_first_entry_only():
    # multi-entry message -> first entry only
    text = "XAUUSD SELL NOW 2982\nXAUUSD SELL LIMIT 2990\nSl : 2995\nTp : 2974"
    setups = extract_setups_from_text(text, 7, datetime(2025, 4, 9, tzinfo=timezone.utc))
    assert len(setups) == 1
    assert setups[0].entry_price == 2982.0


# ---------- labeler: basic TP hit ----------

def test_label_tp_hit():
    setup = Setup(
        message_pk=1,
        posted_at=datetime(2025, 4, 9, 21, 0, tzinfo=timezone.utc),
        instrument="XAUUSD",
        direction="sell",
        entry_type="limit",
        entry_price=2982.0,
        stop_loss=2995.0,
        take_profits=[2974.0],
    )
    bars = _bars([
        ("2025-04-09 21:00:00+00:00", 2985, 2990, 2978, 2980),  # fills (low 2978 <= 2982)
        ("2025-04-09 21:30:00+00:00", 2980, 2982, 2970, 2972),  # TP 2974 touched
    ])
    lb = label_one(setup, bars)
    assert lb.fill_status == "filled"
    assert lb.exit_type == "tp"
    assert lb.exit_price == 2974.0
    assert lb.R == pytest.approx(abs(2974 - 2982) / abs(2982 - 2995), rel=1e-6)


def test_label_sl_hit():
    setup = Setup(
        message_pk=2,
        posted_at=datetime(2025, 4, 9, 21, 0, tzinfo=timezone.utc),
        instrument="XAUUSD",
        direction="buy",
        entry_type="limit",
        entry_price=3000.0,
        stop_loss=2990.0,
        take_profits=[3010.0],
    )
    bars = _bars([
        ("2025-04-09 21:00:00+00:00", 3002, 3005, 2998, 3001),  # fills
        ("2025-04-09 21:30:00+00:00", 3001, 3002, 2988, 2990),  # SL touched
    ])
    lb = label_one(setup, bars)
    assert lb.exit_type == "sl"
    assert lb.R == -1.0


def test_label_unfilled():
    setup = Setup(
        message_pk=3,
        posted_at=datetime(2025, 4, 9, 21, 0, tzinfo=timezone.utc),
        instrument="XAUUSD",
        direction="buy",
        entry_type="limit",
        entry_price=2500.0,  # far away, never touched
        stop_loss=2490.0,
        take_profits=[2510.0],
    )
    bars = _bars([
        ("2025-04-09 21:00:00+00:00", 3000, 3010, 2990, 3005),
    ])
    lb = label_one(setup, bars)
    assert lb.fill_status == "unfilled"
    assert lb.exit_type == "unfilled"


def test_label_ambiguity_pessimistic():
    # SL and TP both touched in the fill bar -> SL wins (pessimistic)
    setup = Setup(
        message_pk=4,
        posted_at=datetime(2025, 4, 9, 21, 0, tzinfo=timezone.utc),
        instrument="XAUUSD",
        direction="buy",
        entry_type="limit",
        entry_price=3000.0,
        stop_loss=2995.0,
        take_profits=[3005.0],
    )
    bars = _bars([
        ("2025-04-09 21:00:00+00:00", 2998, 3006, 2994, 3000),  # fills AND both SL & TP touched
    ])
    lb = label_one(setup, bars)
    assert lb.ambiguity is True
    assert lb.exit_type == "sl"
    assert lb.ambiguity_optimistic is True


def test_label_timeout():
    setup = Setup(
        message_pk=5,
        posted_at=datetime(2025, 4, 9, 21, 0, tzinfo=timezone.utc),
        instrument="XAUUSD",
        direction="buy",
        entry_type="limit",
        entry_price=3000.0,
        stop_loss=2990.0,
        take_profits=[3020.0],
    )
    bars = _bars([
        ("2025-04-09 21:00:00+00:00", 2998, 3002, 2999, 3001),  # fills
        ("2025-04-09 21:30:00+00:00", 3001, 3003, 3000, 3002),  # no exit
    ])
    lb = label_one(setup, bars)
    assert lb.exit_type == "timeout"


def test_label_invalid_no_sl():
    setup = Setup(
        message_pk=6,
        posted_at=datetime(2025, 4, 9, 21, 0, tzinfo=timezone.utc),
        instrument="XAUUSD",
        direction="buy",
        entry_type="limit",
        entry_price=3000.0,
        stop_loss=None,
        take_profits=[3010.0],
    )
    bars = _bars([("2025-04-09 21:00:00+00:00", 2998, 3002, 2999, 3001)])
    lb = label_one(setup, bars)
    assert lb.exit_type == "invalid"


def test_no_lookahead():
    # bars before post_ts must not be used for fill
    setup = Setup(
        message_pk=7,
        posted_at=datetime(2025, 4, 9, 22, 0, tzinfo=timezone.utc),
        instrument="XAUUSD",
        direction="buy",
        entry_type="limit",
        entry_price=3000.0,
        stop_loss=2990.0,
        take_profits=[3010.0],
    )
    bars = _bars([
        ("2025-04-09 21:00:00+00:00", 2998, 3002, 2999, 3001),  # before post -> must be ignored
        ("2025-04-09 22:30:00+00:00", 3000, 3001, 2999, 3000),  # fills
    ])
    lb = label_one(setup, bars)
    assert lb.fill_ts is not None
    assert pd.Timestamp(lb.fill_ts) >= pd.Timestamp("2025-04-09 22:00:00+00:00")


def test_tp_ladder_nearest_first():
    # For a sell, nearest TP is the largest TP below entry
    setup = Setup(
        message_pk=8,
        posted_at=datetime(2025, 4, 9, 21, 0, tzinfo=timezone.utc),
        instrument="XAUUSD",
        direction="sell",
        entry_type="limit",
        entry_price=3000.0,
        stop_loss=3010.0,
        take_profits=[2990.0, 2970.0, 2950.0],
    )
    bars = _bars([
        ("2025-04-09 21:00:00+00:00", 3002, 3005, 2998, 3001),  # fills
        ("2025-04-09 21:30:00+00:00", 2995, 2996, 2985, 2990),  # 2990 touched (nearest)
    ])
    lb = label_one(setup, bars)
    assert lb.exit_type == "tp"
    assert lb.exit_price == 2990.0


def test_pipeline_runs():
    raw = [
        (1, datetime(2025, 4, 9, 21, 0, tzinfo=timezone.utc),
         "XAUUSD SELL NOW 2982\nSl : 2995\nTp : 2974"),
    ]
    bars = _bars([
        ("2025-04-09 21:00:00+00:00", 2985, 2990, 2978, 2980),
        ("2025-04-09 21:30:00+00:00", 2980, 2982, 2970, 2972),
    ])
    df = run_pipeline(raw, bars)
    assert len(df) == 1
    assert df.iloc[0]["exit_type"] == "tp"
    assert "is_recap" in df.columns
    assert "ambiguity" in df.columns
