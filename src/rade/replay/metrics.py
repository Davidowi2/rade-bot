"""Per-setup aggregation and paired statistics for the replay.

Two populations are always reported together (AC-9):

* the **full cohort** — overall strategy performance;
* the **paired subset** — the setups where the two arms actually differ,
  which is where the runner's effect lives.

Neither substitutes for the other, and the conclusion is always stated
relative to the minimum detectable difference (AC-10).
"""
from __future__ import annotations

import math
from collections import Counter
from typing import Sequence

from .engine import ReplayResult

Z_ALPHA_05 = 1.959963985
Z_BETA_80 = 0.8416212336
MDE_MULTIPLIER = Z_ALPHA_05 + Z_BETA_80  # 2.8016


def _mean(xs: Sequence[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def _median(xs: Sequence[float]) -> float:
    if not xs:
        return 0.0
    s = sorted(xs)
    n = len(s)
    mid = n // 2
    return s[mid] if n % 2 else (s[mid - 1] + s[mid]) / 2.0


def _stdev(xs: Sequence[float]) -> float:
    if len(xs) < 2:
        return 0.0
    m = _mean(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - 1))


def max_drawdown(cumulative: Sequence[float]) -> float:
    """Max peak-to-trough decline of a cumulative curve (positive number)."""
    peak = None
    worst = 0.0
    for v in cumulative:
        if peak is None or v > peak:
            peak = v
        if peak is not None:
            worst = max(worst, peak - v)
    return worst


def mean_ci(xs: Sequence[float]) -> dict:
    """Sampling uncertainty of the *mean* of one arm.

    This is a different quantity from the pessimistic/optimistic ambiguity band.
    The band measures uncertainty about the price path *inside* a bar; this
    measures uncertainty about the population mean given the sample size. Both
    are real and neither absorbs the other.
    """
    n = len(xs)
    if n < 2:
        m = _mean(xs)
        return {"n": n, "mean": m, "sd": 0.0, "se": 0.0, "ci95": [m, m],
                "t_stat": 0.0, "excludes_zero": False}
    m = _mean(xs)
    sd = _stdev(xs)
    se = sd / math.sqrt(n)
    half = Z_ALPHA_05 * se
    lo, hi = m - half, m + half
    return {
        "n": n,
        "mean": m,
        "sd": sd,
        "se": se,
        "ci95": [lo, hi],
        "t_stat": (m / se) if se else 0.0,
        "excludes_zero": (lo > 0) or (hi < 0),
    }


def aggregate(results: Sequence[ReplayResult]) -> dict:
    """Summarise one arm over one split."""
    filled = [r for r in results if r.status == "filled"]
    Rs = [r.net_R for r in filled]
    legacy = [r.legacy_R for r in filled]

    ordered = sorted(filled, key=lambda r: (r.fill_ts, r.setup_id))
    curve: list[float] = []
    running = 0.0
    for r in ordered:
        running += r.net_R
        curve.append(running)

    wins = [r for r in filled if r.net_pnl > 0]
    losses = [r for r in filled if r.net_pnl < 0]
    gross_win = sum(r.net_pnl for r in wins)
    gross_loss = abs(sum(r.net_pnl for r in losses))

    reasons = Counter(r.exit_reason for r in filled)
    flag_counts = Counter()
    for r in filled:
        for k, v in r.flags.items():
            if v:
                flag_counts[k] += 1

    ci = mean_ci(Rs)

    return {
        "n_total": len(results),
        "n_filled": len(filled),
        "n_unfilled": sum(1 for r in results if r.status == "unfilled"),
        "n_invalid": sum(1 for r in results if r.status == "invalid"),
        "mean_net_R": ci["mean"],
        "median_net_R": _median(Rs),
        "mean_legacy_R": _mean(legacy),
        # --- sampling uncertainty of the mean (not the bar-path band) ----
        "mean_net_R_sd": ci["sd"],
        "mean_net_R_se": ci["se"],
        "mean_net_R_ci95": ci["ci95"],
        "mean_net_R_t": ci["t_stat"],
        "mean_net_R_excludes_zero": ci["excludes_zero"],
        "total_R": sum(Rs),
        "win_rate": len(wins) / len(filled) if filled else 0.0,
        "profit_factor": (gross_win / gross_loss) if gross_loss > 0 else float("inf") if gross_win > 0 else 0.0,
        # NOTE: this is NOT a portfolio drawdown - see drawdown_note.
        "max_drawdown_R_closed_by_fill_time": max_drawdown(curve),
        "drawdown_note": (
            "Ordering artefact, not a portfolio drawdown: completed setups are "
            "sorted by fill time and their net R accumulated in that order. It "
            "ignores exit-time cash flows, concurrent exposure and mark-to-market "
            "on open positions, so it must not be used to size or approve a real "
            "account."
        ),
        "mean_hours_held": _mean([r.hours_held for r in filled]),
        "median_hours_held": _median([r.hours_held for r in filled]),
        "total_net_pnl": sum(r.net_pnl for r in filled),
        "total_entry_cost": sum(r.entry_cost for r in filled),
        "total_exit_cost": sum(r.exit_cost for r in filled),
        "exit_reasons": dict(reasons),
        "flags": dict(flag_counts),
        "n_ladder_altered": sum(1 for r in filled if r.flag("ladder_altered")),
        # --- the two FTL counts, which are NOT the same thing -----------
        # (a) price-only and arm-independent: the final level was touched
        #     inside the runner horizon, whether or not the position was open.
        "n_ftl_touched": sum(1 for r in filled if r.flag("ftl_reached")),
        # (b) position-aware: a final-target leg actually filled, so the
        #     position was live at the final level and a runner existed.
        "n_ftl_open": sum(1 for r in filled if r.ftl_open),
    }


def _subset_stats(diffs: Sequence[float], label: str = "") -> dict:
    """Mean, SD, SE, CI95 and MDE for one paired population."""
    n = len(diffs)
    if n == 0:
        return {
            "n": 0, "mean_diff": 0.0, "paired_sd": 0.0, "paired_se": 0.0,
            "ci95": [0.0, 0.0], "mde_80": 0.0,
        }
    mean_d = _mean(diffs)
    sd = _stdev(diffs)
    se = sd / math.sqrt(n) if n else 0.0
    half = Z_ALPHA_05 * se
    return {
        "n": n,
        "mean_diff": mean_d,
        "paired_sd": sd,
        "paired_se": se,
        "ci95": [mean_d - half, mean_d + half],
        "mde_80": MDE_MULTIPLIER * se,
    }


def paired_stats(
    a_results: Sequence[ReplayResult],
    b_results: Sequence[ReplayResult],
    label_a: str = "A",
    label_b: str = "B",
) -> dict:
    """Paired comparison of two arms over the same setups.

    Three populations are reported, because each answers a different question
    and none substitutes for another (AC-9, AC-10):

    * **full cohort** - overall strategy performance. This is the useful
      policy comparison.
    * **final-target-touched subset** - the setups where the final target
      *price* was touched inside the runner horizon, **regardless of whether
      the position was still open**. It is arm-independent and it is an upper
      bound on where a runner could have mattered, not a count of trades that
      actually had a runner. Many of these setups had already stopped out.
    * **differing subset** - the setups where the arms actually produce
      different outcomes. This is the only population in which a runner
      demonstrably existed.

    Do not describe the second population as "trades that reached the final
    target with a live position". For that, use ``aggregate()["n_ftl_open"]``,
    which counts exits *at* the final target and is arm-specific.
    """
    by_id_a = {r.setup_id: r for r in a_results if r.status == "filled"}
    by_id_b = {r.setup_id: r for r in b_results if r.status == "filled"}
    shared = sorted(set(by_id_a) & set(by_id_b))

    diffs = [by_id_b[i].net_R - by_id_a[i].net_R for i in shared]
    differing = [d for d in diffs if abs(d) > 1e-12]

    n = len(diffs)
    mean_d = _mean(diffs)
    sd = _stdev(diffs)
    se = sd / math.sqrt(n) if n else 0.0
    half = Z_ALPHA_05 * se
    mde = MDE_MULTIPLIER * se

    # restricted to the setups where the arms actually differ
    n_d = len(differing)
    mean_dd = _mean(differing)
    sd_dd = _stdev(differing)
    se_dd = sd_dd / math.sqrt(n_d) if n_d else 0.0
    mde_dd = MDE_MULTIPLIER * se_dd

    # the final-target-touched population (price-only, arm-independent)
    ftl_ids = [i for i in shared if by_id_a[i].flag("ftl_reached")]
    ftl_diffs = [by_id_b[i].net_R - by_id_a[i].net_R for i in ftl_ids]
    ftl_stats = _subset_stats(ftl_diffs)
    ftl_differing = [d for d in ftl_diffs if abs(d) > 1e-12]
    ftl_diff_stats = _subset_stats(ftl_differing)

    # the position-aware population: arm A was still open when the final
    # level was reached, so a runner genuinely had something to run on
    open_ids = [i for i in shared if by_id_a[i].ftl_open]
    open_diffs = [by_id_b[i].net_R - by_id_a[i].net_R for i in open_ids]
    open_stats = _subset_stats(open_diffs)

    mean_a = _mean([by_id_a[i].net_R for i in shared])
    mean_b = _mean([by_id_b[i].net_R for i in shared])

    def _verdict(pop: Sequence[float]) -> str:
        if not pop:
            return "no difference detectable"
        m = _mean(pop)
        lo = m - Z_ALPHA_05 * (_stdev(pop) / math.sqrt(len(pop)))
        hi = m + Z_ALPHA_05 * (_stdev(pop) / math.sqrt(len(pop)))
        if lo > 0:
            return f"{label_b} better on differing subset (CI excludes 0)"
        if hi < 0:
            return f"{label_b} worse on differing subset (CI excludes 0)"
        return f"{label_b} vs {label_a}: CI includes 0 on differing subset"

    verdict = _verdict(differing)
    ftl_verdict = _verdict(ftl_differing)

    return {
        "label_a": label_a,
        "label_b": label_b,
        "n_shared": n,
        "n_differing": n_d,
        "pct_differing": (n_d / n) if n else 0.0,
        "mean_R_a": mean_a,
        "mean_R_b": mean_b,
        "mean_diff": mean_d,
        "paired_sd": sd,
        "paired_se": se,
        "ci95": [mean_d - half, mean_d + half],
        "mde_80_full": mde,
        "mean_diff_differing": mean_dd,
        "paired_sd_differing": sd_dd,
        "paired_se_differing": se_dd,
        "ci95_differing": [mean_dd - Z_ALPHA_05 * se_dd, mean_dd + Z_ALPHA_05 * se_dd],
        "mde_80_differing": mde_dd,
        "verdict": verdict,
        # --- the final-target-touched population (price-only) ---
        "n_ftl_touched": ftl_stats["n"],
        "mean_diff_ftl_touched": ftl_stats["mean_diff"],
        "paired_sd_ftl_touched": ftl_stats["paired_sd"],
        "paired_se_ftl_touched": ftl_stats["paired_se"],
        "ci95_ftl_touched": ftl_stats["ci95"],
        "mde_80_ftl_touched": ftl_stats["mde_80"],
        "n_ftl_touched_differing": ftl_diff_stats["n"],
        "mean_diff_ftl_touched_differing": ftl_diff_stats["mean_diff"],
        "ci95_ftl_touched_differing": ftl_diff_stats["ci95"],
        "mde_80_ftl_touched_differing": ftl_diff_stats["mde_80"],
        "verdict_ftl_touched": ftl_verdict,
        # --- the position-aware population (A was still open at the final level) ---
        "n_ftl_open": open_stats["n"],
        "mean_diff_ftl_open": open_stats["mean_diff"],
        "paired_sd_ftl_open": open_stats["paired_sd"],
        "paired_se_ftl_open": open_stats["paired_se"],
        "ci95_ftl_open": open_stats["ci95"],
        "mde_80_ftl_open": open_stats["mde_80"],
    }
