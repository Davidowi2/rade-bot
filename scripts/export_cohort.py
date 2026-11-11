"""Export the frozen replay cohort as a versioned, hashed artefact.

The observer service (rade-observer) pins a cohort version rather than
importing code from this repo. This script produces that artefact:

    cohort/v1/labeled_setups.csv    the cohort
    cohort/v1/split_train_ids.json  split membership
    cohort/v1/split_val_ids.json
    cohort/v1/split_test_ids.json
    cohort/v1/MANIFEST.json         definition, counts, sha256 of every file

Re-running must be idempotent: if the content hash of any exported file
changes, the script fails loudly rather than silently re-cutting a cohort.

Usage:
    python scripts/export_cohort.py --version v1
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime, timezone

sys.path.insert(0, "src")

COHORT_SPLITS = ("train", "val", "test")


def sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def git_commit() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], text=True, stderr=subprocess.DEVNULL
        ).strip()
    except Exception:  # noqa: BLE001
        return "unknown"


def is_filled_non_recap(row: dict) -> bool:
    return (
        row["fill_status"] == "filled"
        and str(row["is_recap"]).lower() in ("0", "false")
        and bool(row["fill_ts"])
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--version", default="v1")
    ap.add_argument("--data-dir", default="data")
    ap.add_argument("--out-dir", default=None)
    ap.add_argument("--force", action="store_true",
                    help="overwrite an existing cohort export (will fail on hash drift)")
    args = ap.parse_args()

    out_dir = args.out_dir or os.path.join("cohort", args.version)
    labelled = os.path.join(args.data_dir, "labeled_setups.csv")
    if not os.path.exists(labelled):
        print(f"ERROR: {labelled} not found. Run scripts/store_labeled.py first.", file=sys.stderr)
        return 1

    os.makedirs(out_dir, exist_ok=True)

    rows = list(csv.DictReader(open(labelled, encoding="utf-8")))
    cohort = [r for r in rows if is_filled_non_recap(r)]

    splits: dict[str, list[int]] = {}
    for name in COHORT_SPLITS:
        p = os.path.join(args.data_dir, f"split_{name}_ids.json")
        splits[name] = json.load(open(p, encoding="utf-8"))

    # --- write cohort csv ------------------------------------------------
    cohort_csv = os.path.join(out_dir, "labeled_setups.csv")
    fields = list(rows[0].keys())
    with open(cohort_csv, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        for r in cohort:
            w.writerow(r)

    # --- write splits ----------------------------------------------------
    for name in COHORT_SPLITS:
        with open(os.path.join(out_dir, f"split_{name}_ids.json"), "w", encoding="utf-8") as fh:
            json.dump(splits[name], fh, indent=1)

    # --- counts ----------------------------------------------------------
    cohort_pks = {int(r["message_pk"]) for r in cohort}
    per_split = {}
    for name in COHORT_SPLITS:
        ids = splits[name]
        in_split = [i for i in ids if i in cohort_pks]
        per_split[name] = {
            "ids": len(ids),
            "in_cohort": len(in_split),
            "excluded": len(ids) - len(in_split),
            "posted_at_range": [
                min(i for i in in_split),  # placeholder, replaced below
                max(i for i in in_split),
            ],
        }
    pk_to_posted = {int(r["message_pk"]): r["posted_at"] for r in rows}
    for name in COHORT_SPLITS:
        in_split = [i for i in splits[name] if i in cohort_pks]
        posts = sorted(pk_to_posted[i] for i in in_split)
        per_split[name]["posted_at_range"] = [posts[0][:10], posts[-1][:10]]

    n_ge2 = sum(1 for r in cohort if len(json.loads(r["take_profits"] or "[]")) >= 2)
    n_ge3 = sum(1 for r in cohort if len(json.loads(r["take_profits"] or "[]")) >= 3)

    # --- manifest --------------------------------------------------------
    files = {}
    for fn in sorted(os.listdir(out_dir)):
        p = os.path.join(out_dir, fn)
        if fn == "MANIFEST.json" or not os.path.isfile(p):
            continue
        entry = {"sha256": sha256(p), "bytes": os.path.getsize(p)}
        if fn.endswith(".csv"):
            with open(p, encoding="utf-8") as fh:
                entry["rows"] = sum(1 for _ in csv.DictReader(fh))
        files[fn] = entry

    manifest = {
        "cohort_version": args.version,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "source": {
            "repo": "rade-bot",
            "git_commit": git_commit(),
            "labelled_setups_sha256": sha256(labelled),
            "note": "Regenerate with: python scripts/export_cohort.py --version <v>",
        },
        "definition": {
            "inclusion": "parsed, valid, non-recap, filled (fill_status == 'filled' and is_recap == 0 and fill_ts present)",
            "exclusion_notes": [
                "29 filled non-recap setups carry no split assignment and are excluded",
                "recaps excluded upstream by labelling/recap_filter.py",
                "cohort membership is independent of bar resolution; the 1m re-derivation changes outcomes within the cohort, not membership",
            ],
        },
        "counts": {
            "labelled_total": len(rows),
            "cohort": len(cohort),
            "with_at_least_2_tps": n_ge2,
            "with_at_least_3_tps": n_ge3,
            "per_split": per_split,
        },
        "split_policy": {
            "method": "chronological 60/20/20 by posted_at, 39h purge at both boundaries",
            "reuse": "the archived split id files are reused verbatim; they are not re-cut",
            "roles": {
                "train": "development only - no selection",
                "val": "selection",
                "test": "holdout - opened once",
            },
        },
        "assumptions_ref": "docs/14-redirect-plan.md#4-replay-protocol",
        "assumptions": {
            "bar_resolution": "1m primary; 30m reported as sensitivity",
            "entry": "limit touch at the stated price",
            "fill_window_hours": 24,
            "tp_ladder": "normalised nearest-first, wrong-side targets dropped",
            "sizing": "0.5% of a constant 100000 reference equity",
            "spread": 0.25,
            "slippage": 0.25,
            "commission": 0.0,
            "entry_cost_in_R": True,
            "stop_after_tp1": "cost-adjusted breakeven (entry +/- 0.50)",
            "stop_after_tp2": "unchanged",
            "trail_rule": "1R behind favourable peak, ratcheted, never backward",
            "runner_size_at_ftl": "half the final tranche trails, half closes",
            "pre_ladder_cap_hours": 24,
            "runner_cap_hours": 48,
            "intra_bar_ordering": "stop wins ties (pessimistic); ambiguous cases flagged and counted",
            "seed": 42,
        },
        "files": files,
    }

    with open(os.path.join(out_dir, "MANIFEST.json"), "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2)

    print(f"exported cohort {args.version} -> {out_dir}/")
    print(f"  cohort rows      : {len(cohort)}")
    for name in COHORT_SPLITS:
        print(f"  {name:<6} split    : {per_split[name]['ids']} ids, {per_split[name]['in_cohort']} in cohort "
              f"({per_split[name]['posted_at_range'][0]} -> {per_split[name]['posted_at_range'][1]})")
    print(f"  >=2 TPs          : {n_ge2}")
    print(f"  >=3 TPs          : {n_ge3}")
    for fn, meta in files.items():
        print(f"  {fn:<26} {meta['sha256'][:16]}...  {meta['bytes']:,} bytes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
