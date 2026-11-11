"""Replay engine — paired exit-policy evaluation on the frozen cohort.

Implements the spec in ``docs/14-redirect-plan.md``. The policy decisions
(S0 baseline framing, Policy A / B / C, 1R trail, validation-for-selection)
are fixed; this package only executes them.
"""
from __future__ import annotations

from .assumptions import Assumptions
from .engine import Bar, Leg, ReplayResult, replay_cohort, replay_setup
from .ladder import LadderInfo, normalise_ladder
from .metrics import aggregate, paired_stats

__all__ = [
    "Assumptions",
    "Bar",
    "Leg",
    "LadderInfo",
    "ReplayResult",
    "aggregate",
    "normalise_ladder",
    "paired_stats",
    "replay_cohort",
    "replay_setup",
]
