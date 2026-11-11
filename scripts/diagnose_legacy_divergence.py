"""Reconcile the archived S0 baseline against the paired replay.

    python scripts/diagnose_legacy_divergence.py

The archived ``data/s0_report.csv`` and the new replay disagreed by +0.22 R per
setup (mean). This script isolates *why*, in three controlled steps:

1. **Reproducibility** - re-run the legacy engine on the current code and the
   current database, and diff it against the archived CSV. If the baseline does
   not reproduce, the comparison is against a stale universe and nothing else
   can be concluded.
2. **Ladder normalisation** - re-run the *unmodified* legacy engine, changing
   only the input: the raw take-profit list is replaced by the normalised
   (nearest-first, wrong-side removed) ladder. This isolates the contribution
   of that single defect.
3. **Residual** - compare the normalised legacy engine against the replay. Any
   remaining difference is a genuine behavioural difference (bar ordering,
   cap handling), not a ladder artefact.

Nothing here is a re-forecast. It is a reconciliation of two engines that are
supposed to agree.
"""
from __future__ import annotations

import json
import os
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone

import pandas as pd

sys.path.insert(0, "src")

from rade.backtest import BacktestEngine, CostModel
from rade.labelling.models import Setup
from rade.replay.assumptions import Assumptions
from rade.replay.engine import Bar, Setup as ReplaySetup, replay_cohort
from rade.replay.ladder import normalise_ladder

DB = "data/rade.db"
COHORT = "cohort/v1/labeled_setups.csv"
ARCHIVE = "data/s0_report.csv"


# ---------------------------------------------------------------- loading ---
def load_bars(db_path: str = DB) -> list[Bar]:
    import sqlite3

    con = sqlite3.connect(db_path)
    df = pd.read_sql(
        "SELECT ts, open, high, low, close FROM bars WHERE timeframe='30m' ORDER BY ts",
        con,
    )
    con.close()
    ts = pd.to_datetime(df["ts"], utc=True)
    return [
        Bar(ts=r.to_pydatetime(), open=float(o), high=float(h), low=float(l), close=float(c))
        for r, o, h, l, c in zip(ts, df["open"], df["high"], df["low"], df["close"])
    ]


def load_labeled(db_path: str = DB) -> list[Setup]:
    """The eligible set exactly as ``scripts/run_s0_report.py`` builds it."""
    import sqlite3

    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    rows = con.execute("SELECT * FROM labeled_setups").fetchall()
    con.close()

    out: list[Setup] = []
    for r in rows:
        if r["is_recap"]:
            continue
        if r["stop_loss"] is None or r["entry_price"] is None:
            continue
        posted_at = r["posted_at"]
        if posted_at is not None and getattr(posted_at, "tzinfo", None) is None:
            posted_at = pd.Timestamp(posted_at).tz_localize("UTC").to_pydatetime()
        tps = json.loads(r["take_profits"]) if r["take_profits"] else []
        out.append(
            Setup(
                message_pk=r["message_pk"],
                posted_at=posted_at,
                instrument=r["instrument"] or "XAUUSD",
                direction=r["direction"],
                entry_type=r["entry_type"] or "market",
                entry_price=r["entry_price"],
                stop_loss=r["stop_loss"],
                take_profits=tps,
            )
        )
    return out


def to_replay_setups(setups: list[Setup]) -> list[ReplaySetup]:
    return [
        ReplaySetup(
            setup_id=s.message_pk,
            posted_at=s.posted_at,
            direction=s.direction,
            entry_price=s.entry_price,
            stop_loss=s.stop_loss,
            take_profits=list(s.take_profits),
        )
        for s in setups
    ]


# ------------------------------------------------------- legacy execution ---
def run_legacy(setups: list[Setup], bars: pd.DataFrame, *, normalise: bool) -> dict:
    """Run the legacy engine. ``normalise=True`` only changes the *input* ladder."""
    cm = CostModel(spread=0.25, slippage=0.25, slippage_enabled=True)
    used = setups
    if normalise:
        used = []
        for s in setups:
            info = normalise_ladder(s.direction, s.entry_price, s.take_profits)
            used.append(
                Setup(
                    message_pk=s.message_pk,
                    posted_at=s.posted_at,
                    instrument=s.instrument,
                    direction=s.direction,
                    entry_type=s.entry_type,
                    entry_price=s.entry_price,
                    stop_loss=s.stop_loss,
                    take_profits=list(info.levels),
                )
            )
    eng = BacktestEngine(
        bars=bars,
        setups=used,
        cost_model=cm,
        risk_pct=0.005,
        max_hold_hours=15.0,
        move_sl_to_be=False,
        fill_window_hours=24.0,
    )
    res = eng.run()

    groups: dict[int, list] = defaultdict(list)
    for t in res.trades:
        groups[t.setup_id].append(t)

    out: dict[int, dict] = {}
    for sid, legs in groups.items():
        legs_sorted = sorted(legs, key=lambda t: t.exit_ts)
        total_pnl = sum(l.pnl for l in legs)
        risk = legs[0].risk_dollars
        out[sid] = {
            "setup_id": sid,
            "n_legs": len(legs),
            "total_pnl": total_pnl,
            "risk_dollars": risk,
            # scale-free path R: identical to what the replay calls legacy_R
            "path_R": (total_pnl / risk) if risk and risk > 0 else 0.0,
            "exit_type": legs_sorted[-1].exit_type,
            "entry_ts": legs[0].entry_ts,
            "exit_ts": legs_sorted[-1].exit_ts,
            "legs": [
                {
                    "type": l.exit_type,
                    "price": l.exit_price,
                    "size": l.size,
                    "pnl": l.pnl,
                    "R": (l.pnl / risk) if risk and risk > 0 else 0.0,
                    "ts": l.exit_ts,
                }
                for l in legs_sorted
            ],
        }
    return out


def bars_frame(bars: list[Bar]) -> pd.DataFrame:
    return pd.DataFrame([{"ts": b.ts, "open": b.open, "high": b.high,
                          "low": b.low, "close": b.close} for b in bars])


# ------------------------------------------------------------- comparison ---
def compare(name: str, a: dict, b: dict) -> dict:
    common = sorted(set(a) & set(b))
    diffs = [b[i]["path_R"] - a[i]["path_R"] for i in common]
    n_ident = sum(1 for d in diffs if abs(d) < 1e-9)
    worst = sorted(
        ((abs(b[i]["path_R"] - a[i]["path_R"]), i) for i in common), reverse=True
    )[:12]
    return {
        "label": name,
        "n_a": len(a),
        "n_b": len(b),
        "n_common": len(common),
        "n_identical": n_ident,
        "n_differing": len(common) - n_ident,
        "mean_R_a": sum(a[i]["path_R"] for i in common) / len(common) if common else 0.0,
        "mean_R_b": sum(b[i]["path_R"] for i in common) / len(common) if common else 0.0,
        "mean_diff": sum(diffs) / len(diffs) if diffs else 0.0,
        "worst": [
            {"setup_id": i, "diff": b[i]["path_R"] - a[i]["path_R"],
             "a_R": a[i]["path_R"], "b_R": b[i]["path_R"],
             "a_exit": a[i]["exit_type"], "b_exit": b[i]["exit_type"],
             "a_legs": a[i]["n_legs"], "b_legs": b[i]["n_legs"]}
            for _, i in worst
        ],
    }


def main() -> int:
    print("=" * 78)
    print("STEP 1  REPRODUCIBILITY - does the archived baseline still hold?")
    print("=" * 78)

    bars = load_bars()
    print(f"30m bars: {len(bars):,}  ({bars[0].ts.date()} -> {bars[-1].ts.date()})")
    bf = bars_frame(bars)

    setups = load_labeled()
    print(f"eligible setups (recaps and no-SL excluded): {len(setups):,}")

    legacy_raw = run_legacy(setups, bf, normalise=False)
    print(f"legacy engine (fresh) -> {len(legacy_raw):,} filled setups")

    if os.path.exists(ARCHIVE):
        arch = pd.read_csv(ARCHIVE)
        arch_by_id = {int(r.setup_id): r for r in arch.itertuples()}
        common = sorted(set(arch_by_id) & set(legacy_raw))
        n_ident = 0
        max_dev = 0.0
        dev_rows = []
        for i in common:
            d = legacy_raw[i]["path_R"] - float(arch_by_id[i].total_R)
            if abs(d) < 1e-9:
                n_ident += 1
            else:
                dev_rows.append((abs(d), i, d))
            max_dev = max(max_dev, abs(d))
        print(f"\narchived baseline rows        : {len(arch_by_id):,}")
        print(f"fresh legacy rows              : {len(legacy_raw):,}")
        print(f"common setup ids               : {len(common):,}")
        print(f"identical total_R (|d|<1e-9)   : {n_ident:,} / {len(common):,}")
        print(f"maximum |difference|           : {max_dev:.10f} R")
        if dev_rows:
            dev_rows.sort(reverse=True)
            print("largest deviations:")
            for d, i, s in dev_rows[:8]:
                print(f"   pk={i:<7} archived R={float(arch_by_id[i].total_R):+12.6f}  "
                      f"fresh R={legacy_raw[i]['path_R']:+12.6f}  diff={s:+.6f}")
        only_arch = sorted(set(arch_by_id) - set(legacy_raw))
        only_new = sorted(set(legacy_raw) - set(arch_by_id))
        print(f"only in archive: {len(only_arch)}   only in fresh run: {len(only_new)}")
        if only_new:
            print(f"   sample only-in-fresh: {only_new[:10]}")
    else:
        print(f"!! {ARCHIVE} missing - cannot check reproducibility")

    print()
    print("=" * 78)
    print("STEP 2  LADDER NORMALISATION - fix only the wrong-side / mis-ordered ladder")
    print("=" * 78)

    legacy_fixed = run_legacy(setups, bf, normalise=True)
    print(f"legacy engine with normalised ladders -> {len(legacy_fixed):,} filled setups")

    c2 = compare("legacy_raw -> legacy_fixed", legacy_raw, legacy_fixed)
    print(f"\ncommon {c2['n_common']:,}   identical {c2['n_identical']:,}   "
          f"differing {c2['n_differing']:,}")
    print(f"mean R  raw {c2['mean_R_a']:+.4f}  ->  fixed {c2['mean_R_b']:+.4f}   "
          f"mean diff {c2['mean_diff']:+.4f}")
    print("largest movers:")
    for w in c2["worst"][:8]:
        print(f"   pk={w['setup_id']:<7} {w['a_R']:+11.4f} -> {w['b_R']:+11.4f}  "
              f"(diff {w['diff']:+.4f})  exit {w['a_exit']}->{w['b_exit']}  "
              f"legs {w['a_legs']}->{w['b_legs']}")

    print()
    print("=" * 78)
    print("STEP 3  RESIDUAL - normalised legacy vs the paired replay")
    print("=" * 78)

    a = Assumptions(breakeven_after_tp1=False, pre_ladder_cap_hours=15.0)
    rsetups = to_replay_setups(setups)
    rep = replay_cohort(rsetups, bars, "A", a)
    replay_by_id = {
        r.setup_id: {
            "path_R": r.legacy_R,
            "exit_type": r.exit_reason,
            "entry_ts": r.fill_ts,
            "exit_ts": r.exit_ts,
            "n_legs": len(r.legs),
        }
        for r in rep if r.status == "filled"
    }
    print(f"replay (Policy A, BE off, cap 15h) -> {len(replay_by_id):,} filled setups")

    c3 = compare("legacy_fixed -> replay", legacy_fixed, replay_by_id)
    print(f"\ncommon {c3['n_common']:,}   identical {c3['n_identical']:,}   "
          f"differing {c3['n_differing']:,}")
    print(f"mean R  fixed {c3['mean_R_a']:+.4f}  vs  replay {c3['mean_R_b']:+.4f}   "
          f"mean diff {c3['mean_diff']:+.4f}")
    print("largest residuals:")
    for w in c3["worst"][:10]:
        print(f"   pk={w['setup_id']:<7} legacy {w['a_R']:+10.4f} vs replay {w['b_R']:+10.4f}  "
              f"(diff {w['diff']:+.4f})  exit {w['a_exit']}->{w['b_exit']}  "
              f"legs {w['a_legs']}->{w['b_legs']}")

    # --- attribution of the residual -----------------------------------
    print()
    print("-" * 78)
    print("RESIDUAL ATTRIBUTION")
    print("-" * 78)
    reasons = Counter()
    for i in sorted(set(legacy_fixed) & set(replay_by_id)):
        lf, rp = legacy_fixed[i], replay_by_id[i]
        if abs(lf["path_R"] - rp["path_R"]) < 1e-9:
            continue
        if lf["entry_ts"] != rp["entry_ts"]:
            reasons["different fill bar"] += 1
        elif lf["exit_type"] != rp["exit_type"]:
            reasons[f"different exit ({lf['exit_type']} vs {rp['exit_type']})"] += 1
        else:
            reasons["same fill/exit, different R"] += 1
    for k, v in reasons.most_common():
        print(f"  {v:5,}  {k}")

    # --- ladder population ---------------------------------------------
    print()
    print("-" * 78)
    print("LADDER DEFECT POPULATION (whole eligible cohort)")
    print("-" * 78)
    n_ws = n_ro = n_dup = 0
    for s in setups:
        info = normalise_ladder(s.direction, s.entry_price, s.take_profits)
        n_ws += 1 if info.dropped_wrong_side else 0
        n_ro += 1 if info.reordered else 0
        n_dup += 1 if info.dropped_duplicate else 0
    print(f"  eligible setups                       : {len(setups):,}")
    print(f"  with a wrong-side target              : {n_ws:,}")
    print(f"  whose ladder was not nearest-first    : {n_ro:,}")
    print(f"  with a duplicate target               : {n_dup:,}")

    # --- how much of the mean gap do the wrong-side setups carry? ------
    print()
    print("-" * 78)
    print("HOW MUCH OF THE MEAN GAP DO THE WRONG-SIDE SETUPS CARRY?")
    print("-" * 78)
    ws_ids = {
        s.message_pk
        for s in setups
        if normalise_ladder(s.direction, s.entry_price, s.take_profits).dropped_wrong_side
    }
    for label, A, B in (("legacy_raw -> legacy_fixed", legacy_raw, legacy_fixed),
                        ("legacy_raw -> replay", legacy_raw, replay_by_id),
                        ("legacy_fixed -> replay", legacy_fixed, replay_by_id)):
        common = sorted(set(A) & set(B))
        if not common:
            continue
        tot = sum(B[i]["path_R"] - A[i]["path_R"] for i in common)
        ws_tot = sum(B[i]["path_R"] - A[i]["path_R"] for i in common if i in ws_ids)
        ws_n = sum(1 for i in common if i in ws_ids)
        others_n = len(common) - ws_n
        others_tot = tot - ws_tot
        others_mean = others_tot / others_n if others_n else 0.0
        print(f"\n  {label}")
        print(f"    mean diff over {len(common):,} common setups : {tot / len(common):+.4f} R")
        print(f"    wrong-side setups ({ws_n:>4}) contribute     : {ws_tot / len(common):+.4f} R"
              f"   ({ws_tot:+.2f} R total)")
        print(f"    all other setups   ({others_n:>4}) contribute   : "
              f"{others_tot / len(common):+.4f} R   ({others_tot:+.2f} R total, "
              f"mean {others_mean:+.4f} each)")

    out = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "reproducibility": {
            "n_archive": len(arch_by_id) if os.path.exists(ARCHIVE) else None,
            "n_fresh_legacy": len(legacy_raw),
            "n_identical": n_ident if os.path.exists(ARCHIVE) else None,
            "max_abs_dev_R": max_dev if os.path.exists(ARCHIVE) else None,
        },
        "ladder_fix": {"n_wrong_side": n_ws, "n_reordered": n_ro, "n_duplicate": n_dup},
        "comparisons": [c2, c3],
    }
    os.makedirs("data", exist_ok=True)
    with open("data/legacy_divergence_report.json", "w", encoding="utf-8") as fh:
        json.dump(out, fh, indent=2, default=str)
    print("\nwrote data/legacy_divergence_report.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
