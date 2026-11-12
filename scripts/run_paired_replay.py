"""Run the paired exit-policy replay.

    python scripts/run_paired_replay.py --resolution 30m --split all
    python scripts/run_paired_replay.py --resolution 1m  --split val

Reports, for every arm and every split:

* the **full cohort** result (overall strategy performance), and
* the **paired subset** result (where the arms actually differ),

together with the paired variance and the minimum detectable difference, so
the conclusion is always stated relative to what the cohort can resolve.

Selection discipline (AC-11): parameters are chosen on ``val`` only. This
script never chooses anything - it reports. ``--split test`` should be run
once, after the policy is frozen.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone

import pandas as pd

sys.path.insert(0, "src")

from rade.replay.assumptions import Assumptions
from rade.replay.engine import POLICIES, Bar, Setup, replay_cohort
from rade.replay.metrics import aggregate, paired_stats

SPLITS = ("train", "val", "test")


def parse_utc(value: str) -> datetime:
    """Parse a timestamp that is stored naive-but-UTC.

    The cohort export carries timestamps without an offset, because that is how
    the labeller stores them (``store_labeled.py`` parses them with
    ``utc=True``). Calling ``.astimezone()`` on a naive datetime instead
    interprets it as *local* time, which silently shifts every ``posted_at``
    by the machine's UTC offset - and with it every fill window. The
    conversion is therefore explicit: naive means UTC.
    """
    ts = datetime.fromisoformat(value)
    if ts.tzinfo is None:
        return ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc)


def load_bars(db_path: str, timeframe: str) -> list[Bar]:
    con = sqlite3.connect(db_path)
    df = pd.read_sql(
        "SELECT ts, open, high, low, close FROM bars WHERE timeframe = ? ORDER BY ts",
        con,
        params=(timeframe,),
    )
    con.close()
    if df.empty:
        return []
    ts = pd.to_datetime(df["ts"], utc=True)
    return [
        Bar(ts=r.to_pydatetime(), open=float(o), high=float(h), low=float(l), close=float(c))
        for r, o, h, l, c in zip(ts, df["open"], df["high"], df["low"], df["close"])
    ]


def load_setups(cohort_csv: str) -> list[Setup]:
    import csv
    import json as _json

    out: list[Setup] = []
    with open(cohort_csv, encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            tps = _json.loads(r["take_profits"] or "[]")
            out.append(
                Setup(
                    setup_id=int(r["message_pk"]),
                    posted_at=parse_utc(r["posted_at"]),
                    direction=r["direction"],
                    entry_price=float(r["entry_price"]),
                    stop_loss=float(r["stop_loss"]) if r["stop_loss"] else None,
                    take_profits=[float(t) for t in tps],
                )
            )
    return out


def load_split(cohort_dir: str, name: str) -> set[int]:
    with open(os.path.join(cohort_dir, f"split_{name}_ids.json"), encoding="utf-8") as fh:
        return set(json.load(fh))


def fmt(x, nd: int = 4) -> str:
    if x is None:
        return "-"
    if isinstance(x, float):
        if x != x or x in (float("inf"), float("-inf")):
            return str(x)
        return f"{x:+.{nd}f}"
    return str(x)


def report(results_by_policy: dict, label: str, a: Assumptions) -> None:
    print(f"\n{'=' * 78}\n{label}\n{'=' * 78}")
    for policy in POLICIES:
        res = results_by_policy.get(policy)
        if not res:
            continue
        agg = aggregate(res)
        print(f"\n--- Policy {policy} ---")
        print(f"  n filled            : {agg['n_filled']}  "
              f"(unfilled {agg['n_unfilled']}, invalid {agg['n_invalid']})")
        print(f"  mean net R          : {fmt(agg['mean_net_R'])}   "
              f"median {fmt(agg['median_net_R'])}   total {fmt(agg['total_R'], 2)} R")
        print(f"  sampling CI95       : [{fmt(agg['mean_net_R_ci95'][0])}, "
              f"{fmt(agg['mean_net_R_ci95'][1])}]   "
              f"SD {agg['mean_net_R_sd']:.4f}  SE {agg['mean_net_R_se']:.4f}  "
              f"t {agg['mean_net_R_t']:+.2f}   "
              f"{'excludes 0' if agg['mean_net_R_excludes_zero'] else 'INCLUDES 0'}")
        print(f"  mean legacy R       : {fmt(agg['mean_legacy_R'])}   "
              f"(difference is the entry cost: "
              f"{fmt(agg['mean_legacy_R'] - agg['mean_net_R'])} R/setup)")
        print(f"  win rate / PF       : {agg['win_rate']:.3f} / {agg['profit_factor']:.3f}")
        print(f"  max drawdown (not a portfolio DD): "
              f"{agg['max_drawdown_R_closed_by_fill_time']:.3f} R")
        print(f"  hold (mean/median)  : {agg['mean_hours_held']:.2f}h / {agg['median_hours_held']:.2f}h")
        print(f"  exit reasons        : {agg['exit_reasons']}")
        print(f"  final target touched: {agg['n_ftl_touched']} of {agg['n_filled']} "
              f"(price-only, position may be closed)")
        print(f"  runner-eligible     : {agg['n_ftl_open']} of {agg['n_filled']} "
              f"(position live at the final target)")
        if agg["flags"]:
            print(f"  flags               : {agg['flags']}")
        print(f"  ladder altered      : {agg['n_ladder_altered']}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--resolution", default="30m", help="bar timeframe in the DB (30m, 1H, 1D, 1m)")
    ap.add_argument("--split", default="all", choices=("all", *SPLITS))
    ap.add_argument("--db", default="data/rade.db")
    ap.add_argument("--cohort-dir", default="cohort/v1")
    ap.add_argument("--out", default=None, help="write the full JSON report here")
    ap.add_argument("--policies", default=",".join(POLICIES))
    ap.add_argument("--no-breakeven", action="store_true",
                    help="disable the stop-to-breakeven move after TP1; reproduces the "
                         "archived S0 configuration, which did not bundle it")
    ap.add_argument("--pre-ladder-cap", type=float, default=None,
                    help="override the pre-ladder hold cap in hours (the replay is "
                         "highly sensitive to this - always report a sweep)")
    ap.add_argument("--optimistic", action="store_true",
                    help="resolve stop/target ties in favour of the target and treat a "
                         "stop touched on the fill bar as having happened before the "
                         "fill. This is the optimistic bound required by AC-7; the "
                         "default is the pessimistic base case.")
    args = ap.parse_args()

    a = Assumptions(breakeven_after_tp1=not args.no_breakeven,
                    optimistic_ambiguity=args.optimistic)
    if args.pre_ladder_cap is not None:
        a = Assumptions(
            spread=a.spread, slippage=a.slippage, commission=a.commission,
            reference_equity=a.reference_equity, risk_pct=a.risk_pct,
            fill_window_hours=a.fill_window_hours,
            pre_ladder_cap_hours=args.pre_ladder_cap,
            runner_cap_hours=a.runner_cap_hours,
            trail_multiple_r=a.trail_multiple_r,
            runner_fraction_at_ftl=a.runner_fraction_at_ftl,
            breakeven_after_tp1=a.breakeven_after_tp1,
            seed=a.seed,
            optimistic_ambiguity=a.optimistic_ambiguity,
        )
    bars = load_bars(args.db, args.resolution)
    if not bars:
        print(f"ERROR: no bars for timeframe {args.resolution!r} in {args.db}", file=sys.stderr)
        return 1
    print(f"loaded {len(bars):,} {args.resolution} bars  "
          f"({bars[0].ts.date()} -> {bars[-1].ts.date()})")
    print(f"assumptions: risk {a.risk_pct:.3%} of {a.reference_equity:,.0f} · "
          f"spread {a.spread} · slippage {a.slippage} · "
          f"breakeven-after-TP1 {'ON' if a.breakeven_after_tp1 else 'OFF (archived S0 config)'} · "
          f"trail {a.trail_multiple_r}R · runner fraction {a.runner_fraction_at_ftl} · "
          f"ambiguity {'OPTIMISTIC bound' if a.optimistic_ambiguity else 'PESSIMISTIC base case'}")

    setups = load_setups(os.path.join(args.cohort_dir, "labeled_setups.csv"))
    by_id = {s.setup_id: s for s in setups}

    split_ids: dict[str, set[int]] = {s: load_split(args.cohort_dir, s) for s in SPLITS}
    if args.split == "all":
        selected = set(by_id)
        split_label = "ALL SPLITS (train + val + test)"
    else:
        selected = split_ids[args.split]
        split_label = f"{args.split.upper()} split"

    subset = [by_id[i] for i in sorted(selected) if i in by_id]
    print(f"cohort: {len(subset):,} setups in {split_label}")

    policies = [p.strip() for p in args.policies.split(",") if p.strip()]
    results_by_policy: dict[str, list] = {}
    for policy in policies:
        results_by_policy[policy] = replay_cohort(subset, bars, policy, a)

    report(results_by_policy, f"REPLAY — {args.resolution} bars — {split_label}", a)

    # --- paired comparisons -------------------------------------------
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "resolution": args.resolution,
        "split": args.split,
        "assumptions": a.to_dict(),
        "bars": {"n": len(bars), "first": str(bars[0].ts), "last": str(bars[-1].ts)},
        "cohort": {"n": len(subset)},
        "aggregate": {p: aggregate(r) for p, r in results_by_policy.items()},
        "paired": {},
    }

    print(f"\n{'=' * 78}\nPAIRED COMPARISONS\n{'=' * 78}")
    # Every ordered pair, not just A-versus-others: the selection rule asks
    # whether C beats B, and that comparison has to be in the saved output
    # rather than computed ad hoc in a separate script.
    pairs = [
        (a_, b_)
        for i, a_ in enumerate(policies)
        for b_ in policies[i + 1:]
    ]
    for base, other in pairs:
        ps = paired_stats(results_by_policy[base], results_by_policy[other], base, other)
        payload["paired"][f"{base}->{other}"] = ps
        print(f"\n--- {base} vs {other} ---")
        print(f"  shared setups       : {ps['n_shared']}   "
              f"arms differ on {ps['n_differing']} ({ps['pct_differing']:.1%})")
        print(f"  mean R  {base} / {other}   : {fmt(ps['mean_R_a'])} / {fmt(ps['mean_R_b'])}")
        print(f"  [full cohort]  n {ps['n_shared']}  diff : {fmt(ps['mean_diff'])}  "
              f"CI95 [{fmt(ps['ci95'][0])}, {fmt(ps['ci95'][1])}]  "
              f"SD {ps['paired_sd']:.4f}  SE {ps['paired_se']:.4f}  "
              f"MDE {ps['mde_80_full']:.4f} R")
        print(f"  [final tgt touched] n {ps['n_ftl_touched']}  diff : "
              f"{fmt(ps['mean_diff_ftl_touched'])}  "
              f"CI95 [{fmt(ps['ci95_ftl_touched'][0])}, {fmt(ps['ci95_ftl_touched'][1])}]  "
              f"MDE {ps['mde_80_ftl_touched']:.4f} R   "
              f"(differing {ps['n_ftl_touched_differing']}, "
              f"diff {fmt(ps['mean_diff_ftl_touched_differing'])}, "
              f"CI95 [{fmt(ps['ci95_ftl_touched_differing'][0])}, "
              f"{fmt(ps['ci95_ftl_touched_differing'][1])}])")
        print(f"  [open at final tgt] n {ps['n_ftl_open']}  diff : "
              f"{fmt(ps['mean_diff_ftl_open'])}  "
              f"CI95 [{fmt(ps['ci95_ftl_open'][0])}, {fmt(ps['ci95_ftl_open'][1])}]  "
              f"MDE {ps['mde_80_ftl_open']:.4f} R")
        print(f"  [differing]    n {ps['n_differing']}  diff : "
              f"{fmt(ps['mean_diff_differing'])}  "
              f"CI95 [{fmt(ps['ci95_differing'][0])}, {fmt(ps['ci95_differing'][1])}]  "
              f"MDE {ps['mde_80_differing']:.4f} R")
        print(f"  verdict             : {ps['verdict']}")

    if args.out:
        os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, default=str)
        print(f"\nwrote {args.out}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
