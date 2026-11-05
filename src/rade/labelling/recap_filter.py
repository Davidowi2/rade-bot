"""Recap (historical re-post) detection.

A setup is a recap when its entry level was already touched in the market
before the post AND the level is not reachable going forward -- i.e. the
trader is re-posting an old level rather than calling a fresh one.

Detection (defensible, conservative):
  - entry touched in bars within the prior 120 days, AND
  - entry NOT touched in the 24h after the post (no forward fill).

This does not change resolved-count metrics (recaps never fill forward) but
keeps the training set free of stale re-posts.
"""
from __future__ import annotations

import pandas as pd

from .models import Setup


def is_recap(setup: Setup, bars: pd.DataFrame, prior_days: int = 120) -> bool:
    """Return True if ``setup`` is a historical re-post."""
    if setup.entry_price is None:
        return False
    post_ts = pd.Timestamp(setup.posted_at, tz="UTC") if setup.posted_at.tzinfo is None else pd.Timestamp(setup.posted_at)
    entry = setup.entry_price
    direction = setup.direction

    # prior touch?
    window_start = post_ts - pd.Timedelta(days=prior_days)
    prior = bars[(bars["ts"] >= window_start) & (bars["ts"] < post_ts)]
    touched_before = bool(((prior["low"] <= entry + 0.5) & (prior["high"] >= entry - 0.5)).any()) if len(prior) else False
    if not touched_before:
        return False

    # forward fill?
    horizon = post_ts + pd.Timedelta(hours=24)
    fwd = bars[(bars["ts"] >= post_ts) & (bars["ts"] <= horizon)]
    if direction == "buy":
        filled_fwd = bool((fwd["low"] <= entry).any()) if len(fwd) else False
    else:
        filled_fwd = bool((fwd["high"] >= entry).any()) if len(fwd) else False
    return not filled_fwd


def filter_recaps(setups: list[Setup], bars: pd.DataFrame) -> tuple[list[Setup], list[bool]]:
    """Return (kept_setups, recap_flags) aligned with input order."""
    flags = [is_recap(s, bars) for s in setups]
    kept = [s for s, f in zip(setups, flags) if not f]
    return kept, flags
