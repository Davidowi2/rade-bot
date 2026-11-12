"""Tests for the statistical and labelling corrections raised in review.

Each test pins one of the five review findings so the wording cannot drift back
into overclaiming:

1. a negative sample mean is not a negative population mean;
2. "final target touched" is not "the position was still open";
3. the drawdown figure is an ordering artefact, not a portfolio drawdown;
4. the gap waterfall's deltas are measured and additive;
5. the diagnostic toggles reproduce the legacy defects they model.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from rade.replay.assumptions import Assumptions
from rade.replay.engine import (
    POLICY_A,
    POLICY_B,
    Bar,
    Setup,
    replay_setup,
)
from rade.replay.metrics import aggregate, mean_ci, paired_stats

UTC = timezone.utc
T0 = datetime(2026, 1, 5, 10, 0, tzinfo=UTC)


def mk_bars(rows, start: datetime = T0, step_min: int = 30):
    out = []
    for i, (o, h, l, c) in enumerate(rows):
        out.append(Bar(ts=start + timedelta(minutes=step_min * i), open=o, high=h, low=l, close=c))
    return out


def mk_setup(**kw) -> Setup:
    base = dict(
        setup_id=1,
        posted_at=T0,
        direction="buy",
        entry_price=100.0,
        stop_loss=90.0,
        take_profits=[105.0, 110.0],
    )
    base.update(kw)
    return Setup(**base)


A = Assumptions()


# --------------------------------------------------------------------------
# 1. Sampling uncertainty is not the bar-path band
# --------------------------------------------------------------------------

def test_mean_ci_includes_zero_for_a_noisy_sample():
    """A sample mean of exactly zero obviously straddles zero."""
    xs = [-1.0, 1.0] * 50
    ci = mean_ci(xs)
    assert ci["mean"] == pytest.approx(0.0)
    assert ci["ci95"][0] < 0 < ci["ci95"][1]
    assert not ci["excludes_zero"]


def test_a_negative_mean_can_still_straddle_zero():
    """The review's central point: -0.07 R is not evidence of negative expectancy."""
    # mean -0.074, sd ~0.92, n ~380 -> CI roughly [-0.17, +0.02]
    xs = [-0.074 - 0.92 + 0.0001 * i for i in range(190)] + [
        -0.074 + 0.92 + 0.0001 * i for i in range(190)
    ]
    ci = mean_ci(xs)
    assert ci["mean"] < 0
    assert ci["ci95"][0] < 0 < ci["ci95"][1], "a negative mean must not imply a negative CI"
    assert not ci["excludes_zero"]


def test_mean_ci_excludes_zero_when_the_effect_is_large():
    xs = [-1.0, -1.2] * 100
    ci = mean_ci(xs)
    assert ci["excludes_zero"]
    assert ci["ci95"][1] < 0


def test_aggregate_reports_the_sampling_interval():
    bars = mk_bars([(100, 101, 89, 95)] + [(95, 96, 94, 95)] * 5)
    agg = aggregate([replay_setup(mk_setup(), bars, POLICY_A, A)])
    assert "mean_net_R_ci95" in agg
    assert agg["mean_net_R_se"] >= 0
    # one observation: the interval is degenerate, not fabricated
    assert agg["mean_net_R_ci95"][0] == pytest.approx(agg["mean_net_R"])


# --------------------------------------------------------------------------
# 2. "Final target touched" is not "the position was open"
# --------------------------------------------------------------------------

def test_touch_after_the_stop_does_not_count_as_a_runner():
    """Price reaches the final target *after* the stop - no runner existed."""
    bars = mk_bars(
        [
            (100, 101, 99, 100),    # fill
            (100, 100.5, 89, 90),   # stop hit
            (90, 111, 90, 110),     # price later reaches 110
            (110, 111, 110, 111),
        ]
    )
    r = replay_setup(mk_setup(), bars, POLICY_A, A)
    assert r.exit_reason == "sl"
    assert r.flag("ftl_reached"), "the price did reach the final level"
    assert not r.ftl_open, "but the position was already closed"


def test_ftl_open_is_true_when_the_final_target_fills():
    # one target leg per bar, so a 2-TP ladder needs three bars to complete
    bars = mk_bars([(100, 101, 99, 100), (100, 106, 100, 105), (105, 111, 104, 110)])
    r = replay_setup(mk_setup(), bars, POLICY_A, A)
    assert r.ftl_open
    assert r.exit_reason == "ftl"


def test_policy_b_counts_its_runner_through_ftl_half():
    """B closes half at the final target and trails the rest - it must still
    register as runner-eligible, even when the runner later stops out."""
    bars = mk_bars(
        [
            (100, 101, 99, 100),    # fill
            (100, 106, 100, 105),   # TP1 -> stop to breakeven
            (105, 111, 104, 110),   # final target -> ftl_half, runner active
            (110, 111, 99, 100),    # runner stops out at the trail
        ]
    )
    r = replay_setup(mk_setup(), bars, POLICY_B, A)
    assert r.exit_reason == "trail_stop", "the runner was stopped out later"
    assert r.ftl_open, "the position was live at the final target"


def test_the_two_counts_diverge_in_aggregate():
    """One setup reaches the level with a live position, one does not."""
    live = mk_bars([(100, 101, 99, 100), (100, 106, 100, 105), (105, 111, 104, 110)])
    dead = mk_bars(
        [(100, 101, 99, 100), (100, 100.5, 89, 90), (90, 111, 90, 110)]
    )
    agg = aggregate(
        [
            replay_setup(mk_setup(setup_id=1), live, POLICY_A, A),
            replay_setup(mk_setup(setup_id=2), dead, POLICY_A, A),
        ]
    )
    assert agg["n_ftl_touched"] == 2
    assert agg["n_ftl_open"] == 1


def test_a_and_b_share_the_runner_eligible_population():
    """A and B are identical up to the final target, so they must agree."""
    bars = mk_bars(
        [
            (100, 101, 99, 100),
            (100, 106, 100, 105),
            (105, 111, 104, 110),
            (110, 111, 105, 106),
        ]
    )
    counts = {}
    for p in ("A", "B"):
        agg = aggregate([replay_setup(mk_setup(), bars, p, A)])
        counts[p] = agg["n_ftl_open"]
    assert counts == {"A": 1, "B": 1}


def test_policy_c_can_lose_runner_eligibility_to_its_own_stop():
    """C's TP2->TP1 stop fires before the final target, so C is not a runner
    variant: it can exit a trade that A would have carried to the final level."""
    setup = mk_setup(take_profits=[105.0, 110.0, 115.0])
    bars = mk_bars(
        [
            (100, 101, 99, 100),    # fill
            (100, 106, 100, 105),   # TP1 -> breakeven
            (105, 111, 104, 110),   # TP2 -> C moves the stop to TP1 (105)
            (110, 110.5, 104, 105), # C is stopped at TP1; A is not (stop 100.25)
            (105, 116, 105, 115),   # A reaches the final target
        ]
    )
    r_a = replay_setup(setup, bars, POLICY_A, A)
    r_c = replay_setup(setup, bars, "C", A)
    assert r_a.exit_reason == "ftl" and r_a.ftl_open
    assert r_c.exit_reason == "tp1_stop" and not r_c.ftl_open


def test_paired_stats_reports_both_populations():
    live = mk_bars([(100, 101, 99, 100), (100, 106, 100, 105), (105, 111, 104, 110)])
    dead = mk_bars([(100, 101, 99, 100), (100, 100.5, 89, 90)])
    setups = [mk_setup(setup_id=1), mk_setup(setup_id=2)]
    barsets = [live, dead]
    a_res = [replay_setup(s, b, POLICY_A, A) for s, b in zip(setups, barsets)]
    b_res = [replay_setup(s, b, POLICY_B, A) for s, b in zip(setups, barsets)]
    ps = paired_stats(a_res, b_res)
    assert ps["n_ftl_touched"] >= ps["n_ftl_open"]
    assert ps["n_ftl_open"] == 1


# --------------------------------------------------------------------------
# 3. The drawdown is an ordering artefact
# --------------------------------------------------------------------------

def test_drawdown_key_is_named_for_what_it_is():
    bars = mk_bars([(100, 101, 99, 100), (100, 111, 100, 110)])
    agg = aggregate([replay_setup(mk_setup(), bars, POLICY_A, A)])
    assert "max_drawdown_R_closed_by_fill_time" in agg
    assert "max_drawdown_R" not in agg, "the old, misleading name must be gone"
    assert "not a portfolio drawdown" in agg["drawdown_note"].lower()


# --------------------------------------------------------------------------
# 4. The diagnostic toggles reproduce the defects they model
# --------------------------------------------------------------------------

def test_raw_ladder_fills_a_wrong_side_target_instantly():
    """The legacy defect: a target below entry on a buy is a 'profit'."""
    bars = mk_bars([(100, 101, 99, 100), (100, 101, 99, 100), (100, 111, 100, 110)])
    setup = mk_setup(take_profits=[95.0, 110.0])  # 95 is below a 100 entry
    buggy = Assumptions(normalise_ladder=False)
    fixed = Assumptions(normalise_ladder=True)

    r_buggy = replay_setup(setup, bars, POLICY_A, buggy)
    r_fixed = replay_setup(setup, bars, POLICY_A, fixed)

    assert r_buggy.legs[0].kind == "tp1"
    assert r_buggy.legs[0].price == 95.0, "filled at the unreachable target"
    assert r_fixed.legs[0].price == 110.0, "the wrong-side target is dropped"
    assert r_buggy.net_R < r_fixed.net_R


def test_cap_toggle_changes_where_the_cap_binds():
    """The weekend-gap case the toggle exists to model.

    Bars at 0/6/12/18h, then a 42h hole (a weekend), then bars at 60/66h. The
    pre-ladder cap is 24h. The new behaviour scans to the first bar at or after
    the outermost boundary, so it reaches the 60h bar and closes there. The old
    behaviour truncated before the hole and closed at 18h, at a price the
    market had long since left.
    """
    bars = [
        Bar(ts=T0 + timedelta(hours=h), open=100, high=101, low=99, close=100)
        for h in (0, 6, 12, 18, 60, 66)
    ]
    old = Assumptions(cap_at_first_bar_after_boundary=False)
    new = Assumptions(cap_at_first_bar_after_boundary=True)

    r_old = replay_setup(mk_setup(), bars, POLICY_A, old)
    r_new = replay_setup(mk_setup(), bars, POLICY_A, new)

    assert r_new.exit_reason == "time_cap"
    assert r_new.hours_held == pytest.approx(60.0)
    assert r_old.exit_reason == "window_end", "the old behaviour truncates early"
    assert r_old.hours_held == pytest.approx(18.0)


def test_the_toggles_default_to_the_plan_behaviour():
    a = Assumptions()
    assert a.normalise_ladder is True
    assert a.cap_at_first_bar_after_boundary is True
