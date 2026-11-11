"""Acceptance-criteria tests for the paired replay engine.

Each test maps to a numbered criterion in ``docs/14-redirect-plan.md`` §4.6.
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
    POLICY_C,
    Bar,
    Setup,
    replay_setup,
)
from rade.replay.ladder import normalise_ladder
from rade.replay.metrics import aggregate, paired_stats

UTC = timezone.utc
T0 = datetime(2026, 1, 5, 10, 0, tzinfo=UTC)


def mk_bars(rows, start: datetime = T0, step_min: int = 30):
    """rows: iterable of (open, high, low, close)."""
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
# AC-5 — ladder normalisation
# --------------------------------------------------------------------------

def test_ladder_drops_targets_on_the_wrong_side():
    info = normalise_ladder("buy", 100.0, [105.0, 95.0, 110.0])
    assert info.levels == [105.0, 110.0]
    assert info.dropped_wrong_side == 1
    assert info.altered


def test_ladder_sorts_nearest_first_and_reports_reordering():
    info = normalise_ladder("buy", 100.0, [110.0, 105.0])
    assert info.levels == [105.0, 110.0]
    assert info.reordered
    assert info.dropped_wrong_side == 0


def test_ladder_for_a_short_sorts_descending():
    info = normalise_ladder("sell", 100.0, [95.0, 90.0, 105.0])
    assert info.levels == [95.0, 90.0]
    assert info.dropped_wrong_side == 1


def test_ladder_removes_duplicates():
    info = normalise_ladder("buy", 100.0, [105.0, 105.0])
    assert info.levels == [105.0]
    assert info.dropped_duplicate == 1


# --------------------------------------------------------------------------
# AC-3 — the entry cost must be inside R
# --------------------------------------------------------------------------

def test_entry_cost_is_inside_r():
    """A fill that immediately stops must be strictly worse than -1.00 R."""
    bars = mk_bars([
        (100, 100, 100, 100),   # fill
        (100, 100, 89, 95),    # stop hit
    ])
    r = replay_setup(mk_setup(take_profits=[105.0, 110.0]), bars, POLICY_A, A)
    assert r.status == "filled"
    assert r.exit_reason == "sl"
    assert r.net_R < -1.0, f"net_R {r.net_R} should include the entry cost"
    # the legacy figure omits the entry spread and is therefore less negative
    assert r.legacy_R > r.net_R
    assert r.net_R == pytest.approx(r.legacy_R - r.entry_cost / r.risk_dollars)


def test_entry_cost_is_charged_once_per_setup_not_per_leg():
    bars = mk_bars([
        (100, 100, 100, 100),
        (100, 106, 99, 105),
        (105, 111, 104, 110),
    ])
    r = replay_setup(mk_setup(), bars, POLICY_A, A)
    assert len(r.legs) == 2
    traded = sum(leg.size for leg in r.legs)
    # charged once on the whole position, not once per leg
    assert r.entry_cost == pytest.approx(A.spread * traded)
    assert r.entry_cost != pytest.approx(A.spread * r.legs[0].size)


def test_entry_cost_makes_the_headline_expectancy_worse():
    bars = mk_bars([
        (100, 100, 100, 100),
        (100, 106, 99, 105),
        (105, 111, 104, 110),
    ])
    r = replay_setup(mk_setup(), bars, POLICY_A, A)
    assert r.net_R < r.legacy_R
    assert r.net_pnl < r.legacy_pnl


# --------------------------------------------------------------------------
# AC-4 — sizing is frozen against a constant reference equity
# --------------------------------------------------------------------------

def test_sizing_is_independent_of_the_equity_path():
    """Size derives from the constant reference equity, not a running curve."""
    bars = mk_bars([(100, 100, 100, 100), (100, 106, 99, 105), (105, 111, 104, 110)])
    r = replay_setup(mk_setup(), bars, POLICY_A, A)
    expected_size = (A.risk_pct * A.reference_equity) / abs(100.0 - 90.0)
    assert sum(leg.size for leg in r.legs) == pytest.approx(expected_size)
    assert r.risk_dollars == pytest.approx(A.risk_pct * A.reference_equity)


def test_both_arms_hold_identical_sizes_on_identical_entries():
    bars = mk_bars([
        (100, 100, 100, 100),
        (100, 106, 99, 105),
        (105, 111, 104, 110),
        (110, 110, 105, 108),
        (108, 108, 100, 101),
    ])
    ra = replay_setup(mk_setup(), bars, POLICY_A, A)
    rb = replay_setup(mk_setup(), bars, POLICY_B, A)
    assert ra.legs[0].size == rb.legs[0].size
    assert ra.entry_cost == rb.entry_cost
    assert ra.risk_dollars == rb.risk_dollars


# --------------------------------------------------------------------------
# AC-6 — intra-bar ordering
# --------------------------------------------------------------------------

def test_stop_wins_a_tie_inside_one_bar():
    """Stop and target touched in the same bar: the stop wins."""
    bars = mk_bars([
        (100, 100, 100, 100),
        (100, 106, 89, 100),   # high >= TP1 AND low <= SL
    ])
    r = replay_setup(mk_setup(), bars, POLICY_A, A)
    assert r.exit_reason == "sl"
    assert r.flag("ambiguous_bar")
    assert r.net_R < -1.0


def test_only_one_target_is_taken_per_bar():
    """A bar that gaps through both targets records only the nearest one."""
    bars = mk_bars([
        (100, 100, 100, 100),
        (100, 112, 99, 111),   # touches TP1=105 and TP2=110 in one bar
    ])
    r = replay_setup(mk_setup(), bars, POLICY_A, A)
    target_legs = [leg for leg in r.legs if leg.kind.startswith("tp")]
    assert len(target_legs) == 1
    assert target_legs[0].kind == "tp1"
    assert target_legs[0].price == 105.0
    # the remainder is still open and is closed at the data end
    assert any(leg.kind == "window_end" for leg in r.legs)


def test_open_gap_fill_is_flagged_and_filled_at_the_limit():
    """A bar that opened beyond the level still fills at the limit price."""
    bars = mk_bars([
        (98, 99, 97, 98),      # opened below the limit for a long
        (100, 106, 99, 105),
    ])
    r = replay_setup(mk_setup(), bars, POLICY_A, A)
    assert r.status == "filled"
    assert r.flag("open_gap")
    assert r.fill_px == 100.0


def test_no_fill_when_the_level_is_never_touched():
    # price stays above the buy limit for the whole fill window
    bars = mk_bars([(105, 106, 104, 105)] * 4)
    r = replay_setup(mk_setup(), bars, POLICY_A, A)
    assert r.status == "unfilled"
    assert r.fill_ts is None


def test_entry_bar_stop_is_flagged():
    """Fill and stop in the same bar: fill-then-stop, flagged."""
    bars = mk_bars([(100, 101, 89, 95)])
    r = replay_setup(mk_setup(), bars, POLICY_A, A)
    assert r.status == "filled"
    assert r.exit_reason == "sl"
    assert r.flag("entry_bar_stop")


# --------------------------------------------------------------------------
# AC-7 — the optimistic bound
# --------------------------------------------------------------------------

OPT = Assumptions(optimistic_ambiguity=True)


def test_optimistic_bound_gives_the_target_the_tie():
    """Under the optimistic bound, the target wins a same-bar tie."""
    bars = mk_bars([
        (100, 100, 100, 100),
        (100, 106, 89, 100),   # high >= TP1 AND low <= SL
    ])
    r = replay_setup(mk_setup(), bars, POLICY_A, OPT)
    assert r.flag("ambiguous_bar"), "the ambiguity is still reported"
    assert r.exit_reason != "sl"
    assert r.net_R > 0.0


def test_optimistic_bound_ignores_a_stop_touched_on_the_fill_bar():
    """A stop breached on the fill bar is assumed to predate the fill."""
    bars = mk_bars([
        (100, 101, 89, 95),   # the fill bar also dips through the stop
        (100, 101, 99, 100),
        (100, 106, 99, 105),
    ])
    r = replay_setup(mk_setup(), bars, POLICY_A, OPT)
    assert r.flag("entry_bar_stop"), "the case is still counted"
    assert r.flag("entry_bar_stop_ignored")
    assert r.exit_reason != "sl"
    assert r.net_R > -1.0


def test_optimistic_bound_never_reports_a_worse_mean_than_the_base_case():
    """The optimistic bound is an upper bound, not a different strategy."""
    bars = mk_bars([
        (100, 100, 100, 100),
        (100, 106, 89, 100),
        (100, 101, 99, 100),
        (100, 112, 99, 111),
    ])
    base = replay_setup(mk_setup(), bars, POLICY_A, A)
    opt = replay_setup(mk_setup(), bars, POLICY_A, OPT)
    assert opt.net_R >= base.net_R


# --------------------------------------------------------------------------
# AC-13 — no lookahead
# --------------------------------------------------------------------------

def test_engine_never_fills_on_a_bar_before_the_post():
    bars = mk_bars(
        [(100, 100, 100, 100)] * 3,
        start=T0 - timedelta(hours=1, minutes=30),
    )
    r = replay_setup(mk_setup(), bars, POLICY_A, A)
    assert r.status == "unfilled"
    assert r.fill_ts is None


def test_exit_is_never_before_the_fill():
    bars = mk_bars([
        (100, 100, 100, 100),
        (100, 106, 99, 105),
        (105, 111, 104, 110),
    ])
    r = replay_setup(mk_setup(), bars, POLICY_A, A)
    assert r.exit_ts >= r.fill_ts
    assert r.hours_held >= 0


# --------------------------------------------------------------------------
# Policy behaviour — trail, ratchet, runner size, breakeven
# --------------------------------------------------------------------------

def _trail_scenario():
    return mk_bars([
        (100, 100, 100, 100),   # fill
        (100, 106, 99, 105),    # TP1
        (105, 111, 104, 110),   # FTL, high = 111 -> peak
        (110, 110, 105, 108),   # drifts
        (108, 108, 100, 101),   # hits the trail stop
    ])


def test_trail_sits_one_r_behind_the_peak():
    bars = _trail_scenario()
    r = replay_setup(mk_setup(), bars, POLICY_B, A)
    # peak 111, 1R = 10 (risk_price), so the trail stop is 101
    trail_leg = [leg for leg in r.legs if leg.kind == "trail_stop"]
    assert trail_leg, f"expected a trail stop, got {[leg.kind for leg in r.legs]}"
    assert trail_leg[0].price == pytest.approx(101.0)


def test_trail_stop_never_moves_backward():
    bars = mk_bars([
        (100, 100, 100, 100),
        (100, 106, 99, 105),
        (105, 111, 104, 110),   # peak 111 -> trail 101
        (111, 113, 108, 112),   # peak 113 -> trail 103 (tighter)
        (112, 112, 102, 103),   # retraces: must stop at 103, not 101
    ])
    r = replay_setup(mk_setup(), bars, POLICY_B, A)
    trail_leg = [leg for leg in r.legs if leg.kind == "trail_stop"]
    assert trail_leg
    assert trail_leg[0].price == pytest.approx(103.0)


def test_runner_trails_only_half_the_final_tranche():
    bars = _trail_scenario()
    r = replay_setup(mk_setup(), bars, POLICY_B, A)
    ftl_half = [leg for leg in r.legs if leg.kind == "ftl_half"]
    assert ftl_half
    tranche = ftl_half[0].size * 2  # the whole final tranche
    assert ftl_half[0].size == pytest.approx(tranche / 2)
    runner = [leg for leg in r.legs if leg.kind == "trail_stop"]
    assert runner and runner[0].size == pytest.approx(ftl_half[0].size)


def test_policy_a_closes_everything_at_the_final_target():
    bars = _trail_scenario()
    r = replay_setup(mk_setup(), bars, POLICY_A, A)
    assert r.exit_reason == "ftl"
    assert all(leg.kind in ("tp1", "ftl") for leg in r.legs)
    assert r.exit_price == pytest.approx(110.0)


def test_stop_moves_to_breakeven_after_the_first_target():
    bars = mk_bars([
        (100, 100, 100, 100),
        (100, 106, 99, 105),    # TP1
        (105, 106, 100, 101),   # retraces to the breakeven floor
    ])
    r = replay_setup(mk_setup(), bars, POLICY_A, A)
    be_leg = [leg for leg in r.legs if leg.kind == "be_stop"]
    assert be_leg
    assert be_leg[0].price == pytest.approx(100.0 + A.breakeven_offset)


def test_tp2_does_not_move_the_stop_by_default():
    """The frozen ladder leaves the stop at breakeven after TP2 (policy A)."""
    bars = mk_bars([
        (100, 100, 100, 100),
        (100, 106, 99, 105),    # TP1 = 105
        (105, 111, 104, 110),   # TP2 = 108
        (110, 110, 100, 101),   # retraces to the breakeven floor
    ])
    r = replay_setup(mk_setup(take_profits=[105.0, 108.0, 110.0]), bars, POLICY_A, A)
    assert not [leg for leg in r.legs if leg.kind == "tp1_stop"]
    assert [leg for leg in r.legs if leg.kind == "be_stop"]


def test_policy_c_moves_the_stop_to_tp1():
    bars = mk_bars([
        (100, 100, 100, 100),
        (100, 106, 99, 105),    # TP1 = 105
        (105, 111, 104, 110),   # TP2 = 108 -> stop moves to TP1 = 105
        (110, 110, 104, 105),   # retraces to TP1
    ])
    r = replay_setup(mk_setup(take_profits=[105.0, 108.0, 110.0]), bars, POLICY_C, A)
    tp1_stop = [leg for leg in r.legs if leg.kind == "tp1_stop"]
    assert tp1_stop
    assert tp1_stop[0].price == pytest.approx(105.0)


# --------------------------------------------------------------------------
# AC-8 — caps are reported
# --------------------------------------------------------------------------

def test_pre_ladder_cap_binds_and_is_flagged():
    """A setup that goes nowhere for 24h is closed by the cap."""
    bars = mk_bars([(100, 100, 100, 100)] + [(100, 101, 99, 100)] * 60)
    r = replay_setup(mk_setup(), bars, POLICY_A, A)
    assert r.exit_reason == "time_cap"
    assert r.flag("cap_pre_ladder")


def test_runner_cap_binds_and_is_flagged():
    """After the FTL, a runner that goes nowhere is closed at 48h."""
    bars = mk_bars(
        [(100, 100, 100, 100), (100, 106, 99, 105), (105, 111, 104, 110)]
        + [(110, 111, 109, 110)] * 120
    )
    r = replay_setup(mk_setup(), bars, POLICY_B, A)
    assert r.exit_reason == "runner_cap"
    assert r.flag("cap_runner")


def test_policy_a_never_hits_the_runner_cap():
    bars = mk_bars(
        [(100, 100, 100, 100), (100, 106, 99, 105), (105, 111, 104, 110)]
        + [(110, 111, 109, 110)] * 120
    )
    r = replay_setup(mk_setup(), bars, POLICY_A, A)
    assert not r.flag("cap_runner")


# --------------------------------------------------------------------------
# AC-12 — determinism
# --------------------------------------------------------------------------

def test_replay_is_deterministic():
    bars = _trail_scenario()
    a = replay_setup(mk_setup(), bars, POLICY_B, A)
    b = replay_setup(mk_setup(), bars, POLICY_B, A)
    assert a.net_R == b.net_R
    assert a.net_pnl == b.net_pnl
    assert [leg.kind for leg in a.legs] == [leg.kind for leg in b.legs]


def test_same_seed_and_data_give_identical_metrics():
    bars = _trail_scenario()
    ra = [replay_setup(mk_setup(), bars, POLICY_A, A)]
    rb = [replay_setup(mk_setup(), bars, POLICY_A, A)]
    assert aggregate(ra) == aggregate(rb)


# --------------------------------------------------------------------------
# AC-9 / AC-10 — both populations, and the paired statistics
# --------------------------------------------------------------------------

def test_paired_stats_reports_both_populations():
    bars = _trail_scenario()
    ra = replay_setup(mk_setup(), bars, POLICY_A, A)
    rb = replay_setup(mk_setup(), bars, POLICY_B, A)
    ps = paired_stats([ra], [rb], "A", "B")
    assert ps["n_shared"] == 1
    assert ps["n_differing"] == 1
    assert ps["mean_diff"] == pytest.approx(rb.net_R - ra.net_R)
    # a single observation cannot support a variance estimate, so the MDE is 0
    # and the CI collapses onto the point estimate
    assert ps["mde_80_full"] >= 0
    assert ps["ci95_differing"][0] <= ps["ci95_differing"][1]
    assert ps["ci95_differing"][0] == pytest.approx(ps["mean_diff_differing"])


def test_paired_stats_estimates_variance_once_there_are_varied_setups():
    """With more than one differing setup the MDE becomes meaningful."""
    scenarios = [
        ([(100, 100, 100, 100), (100, 106, 99, 105), (105, 111, 104, 110),
          (110, 110, 105, 108), (108, 108, 100, 101)], 10.0),
        ([(100, 100, 100, 100), (100, 106, 99, 105), (105, 111, 104, 110),
          (110, 110, 109, 110), (110, 110, 108, 109)], 20.0),
        ([(100, 100, 100, 100), (100, 106, 99, 105), (105, 111, 104, 110),
          (110, 110, 100, 101), (101, 101, 95, 96)], 30.0),
    ]
    a_res, b_res = [], []
    for i, (rows, sl) in enumerate(scenarios, start=1):
        bars = mk_bars(rows)
        setup = mk_setup(setup_id=i, stop_loss=100.0 - sl)
        a_res.append(replay_setup(setup, bars, POLICY_A, A))
        b_res.append(replay_setup(setup, bars, POLICY_B, A))
    ps = paired_stats(a_res, b_res, "A", "B")
    assert ps["n_shared"] == 3
    assert ps["n_differing"] >= 1
    assert ps["paired_sd"] > 0
    assert ps["mde_80_full"] > 0


def test_identical_arms_show_no_differing_setups():
    bars = _trail_scenario()
    ra = replay_setup(mk_setup(), bars, POLICY_A, A)
    ps = paired_stats([ra], [ra], "A", "B")
    assert ps["n_differing"] == 0
    assert ps["mean_diff"] == pytest.approx(0.0)
