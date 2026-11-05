"""Limit-fill labelling engine.

Models the trader's entry as a resting limit order: a setup fills when price
touches the entry level within 24h of the post. Resolution then walks 30m bars
from the fill, checking SL and the TP ladder (nearest first), with a 15h hard
cap. Intra-bar ambiguity (SL and a TP touched in the same bar) is labelled
pessimistically: SL wins the tie, and the optimistic outcome is recorded too.
"""
from __future__ import annotations

from datetime import timedelta

import pandas as pd

from .models import Label, Setup

FILL_WINDOW_HOURS = 24.0
MAX_HOLD_HOURS = 15.0
SPREAD = 0.25


def _to_ts(dt) -> pd.Timestamp:
    ts = pd.Timestamp(dt)
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    return ts


def _nearest_tp(direction: str, tps: list[float], entry: float) -> list[float]:
    """Return TPs sorted nearest-first relative to entry in trade direction."""
    if direction == "buy":
        valid = [t for t in tps if t > entry]
        return sorted(valid)  # ascending -> nearest first
    valid = [t for t in tps if t < entry]
    return sorted(valid, reverse=True)  # descending -> nearest first


def label_one(setup: Setup, bars: pd.DataFrame) -> Label:
    """Resolve a single setup against 30m bars. Returns a Label."""
    label = Label(setup=setup)
    if not setup.is_valid():
        label.exit_type = "invalid"
        label.notes = "missing or zero-risk stop loss"
        return label

    entry = setup.entry_price
    sl = setup.stop_loss
    direction = setup.direction
    tps = _nearest_tp(direction, setup.take_profits, entry)
    risk = abs(entry - sl)

    post_ts = _to_ts(setup.posted_at)
    horizon = post_ts + pd.Timedelta(hours=FILL_WINDOW_HOURS)
    window = bars[(bars["ts"] >= post_ts) & (bars["ts"] <= horizon)]

    # --- fill detection (limit touch) ---
    fill_idx = None
    for idx, row in window.iterrows():
        if row["low"] <= entry <= row["high"]:
            fill_idx = idx
            break

    if fill_idx is None:
        label.fill_status = "unfilled"
        label.exit_type = "unfilled"
        return label

    label.fill_status = "filled"
    label.fill_price = float(entry)
    fill_ts = bars.loc[fill_idx, "ts"]
    label.fill_ts = fill_ts.to_pydatetime() if hasattr(fill_ts, "to_pydatetime") else fill_ts

    # --- resolution ---
    hold_cap = pd.Timedelta(hours=MAX_HOLD_HOURS)
    end_ts = fill_ts + hold_cap
    res_bars = bars[(bars["ts"] >= fill_ts) & (bars["ts"] <= end_ts)]

    outcome = None  # "tp" | "sl" | "timeout"
    exit_px = None
    exit_ts = None
    ambiguity = False
    ambig_optimistic = False

    for _, rb in res_bars.iterrows():
        if direction == "buy":
            sl_touched = bool(rb["low"] <= sl)
            tp_touched = [tp for tp in tps if rb["high"] >= tp]
        else:
            sl_touched = bool(rb["high"] >= sl)
            tp_touched = [tp for tp in tps if rb["low"] <= tp]

        if sl_touched and tp_touched:
            ambiguity = True
            ambig_optimistic = True  # TP would also have been reached
            # pessimistic: SL wins
            outcome = "sl"
            exit_px = sl
            exit_ts = rb["ts"]
            break
        if sl_touched:
            outcome = "sl"
            exit_px = sl
            exit_ts = rb["ts"]
            break
        if tp_touched:
            # nearest touched tp
            if direction == "buy":
                hit = min(tp_touched)
            else:
                hit = max(tp_touched)
            outcome = "tp"
            exit_px = hit
            exit_ts = rb["ts"]
            break

    if outcome is None:
        if len(res_bars):
            last = res_bars.iloc[-1]
            outcome = "timeout"
            exit_px = float(last["close"])
            exit_ts = last["ts"]
        else:
            label.fill_status = "unfilled"
            label.exit_type = "unfilled"
            return label

    label.exit_type = outcome
    label.exit_price = float(exit_px) if exit_px is not None else None
    if exit_ts is not None:
        ets = exit_ts
        label.exit_ts = ets.to_pydatetime() if hasattr(ets, "to_pydatetime") else ets
    label.ambiguity = ambiguity
    label.ambiguity_optimistic = ambig_optimistic

    # hours held
    if label.fill_ts is not None and label.exit_ts is not None:
        delta = pd.Timestamp(label.exit_ts) - pd.Timestamp(label.fill_ts)
        label.hours_held = delta.total_seconds() / 3600.0

    # R
    if outcome == "sl":
        label.R = -1.0
    elif outcome == "tp":
        label.R = abs(exit_px - entry) / risk
    else:  # timeout
        if direction == "buy":
            label.R = (exit_px - entry) / risk
        else:
            label.R = (entry - exit_px) / risk

    return label


def label_all(setups: list[Setup], bars: pd.DataFrame) -> list[Label]:
    """Label a list of setups. Bars must be a DataFrame with ts/open/high/low/close."""
    return [label_one(s, bars) for s in setups]
