"""The exit-policy comparison must sign P&L the way a broker does.

A long that is stopped out is a loss. The first version of the comparison
script wrote ``entry - stop`` for that case, which booked every stop-out as a
win and reversed the TP1-vs-breakeven decision. These tests pin the sign.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pandas as pd
import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "compare_exit_policies.py"


def _load():
    spec = importlib.util.spec_from_file_location("compare_exit_policies", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _bars(*rows):
    """Build a 30-minute bar frame from (ts, high, low, close) tuples."""
    frame = pd.DataFrame(rows, columns=["ts", "high", "low", "close"])
    frame["ts"] = pd.to_datetime(frame["ts"], utc=True)
    return frame.sort_values("ts").reset_index(drop=True)


def _row(direction, entry, stop, tps, fill_ts):
    return {
        "direction": direction,
        "entry_price": entry,
        "stop_loss": stop,
        "take_profits": json.dumps(tps),
        "fill_ts": pd.Timestamp(fill_ts, tz="UTC"),
    }


def test_a_long_that_is_stopped_out_scores_minus_one_R():
    mod = _load()
    bars = _bars(("2026-01-01 00:00", 101.0, 89.0, 95.0))
    row = _row("buy", 100.0, 90.0, [110.0, 120.0], "2026-01-01 00:00")
    r, reason = mod.simulate(row, "tp1", bars)
    assert reason == "sl_initial"
    assert r == pytest.approx(-1.0)


def test_a_short_that_is_stopped_out_scores_minus_one_R():
    mod = _load()
    bars = _bars(("2026-01-01 00:00", 111.0, 99.0, 105.0))
    row = _row("sell", 100.0, 110.0, [90.0, 80.0], "2026-01-01 00:00")
    r, reason = mod.simulate(row, "tp1", bars)
    assert reason == "sl_initial"
    assert r == pytest.approx(-1.0)


def test_a_long_that_reaches_tp1_then_is_stopped_there_scores_the_tp1_profit():
    mod = _load()
    bars = _bars(
        ("2026-01-01 00:00", 101.0, 99.0, 100.0),
        ("2026-01-01 00:30", 110.5, 100.0, 110.0),   # TP1 touched
        ("2026-01-01 01:00", 110.0, 109.0, 109.5),   # stopped at TP1
    )
    row = _row("buy", 100.0, 90.0, [110.0, 120.0], "2026-01-01 00:00")
    r, reason = mod.simulate(row, "tp1", bars)
    assert reason == "stop_after_tp1"
    assert r == pytest.approx(1.0)


def test_the_breakeven_policy_books_zero_when_price_returns_to_the_entry():
    mod = _load()
    bars = _bars(
        ("2026-01-01 00:00", 101.0, 99.0, 100.0),
        ("2026-01-01 00:30", 110.5, 100.0, 110.0),   # TP1 touched
        ("2026-01-01 01:00", 101.0, 99.5, 100.0),    # back to entry -> breakeven stop
    )
    row = _row("buy", 100.0, 90.0, [110.0, 120.0], "2026-01-01 00:00")
    r, reason = mod.simulate(row, "breakeven", bars)
    assert reason == "stop_after_tp1"
    assert r == pytest.approx(0.0)


def test_the_final_target_is_a_win_under_both_policies():
    mod = _load()
    bars = _bars(
        ("2026-01-01 00:00", 101.0, 99.0, 100.0),
        ("2026-01-01 00:30", 110.5, 100.0, 110.0),
        ("2026-01-01 01:00", 121.0, 110.5, 120.0),
    )
    row = _row("buy", 100.0, 90.0, [110.0, 120.0], "2026-01-01 00:00")
    for policy in ("tp1", "breakeven"):
        r, reason = mod.simulate(row, policy, bars)
        assert reason == "final_tp"
        assert r == pytest.approx(2.0)
