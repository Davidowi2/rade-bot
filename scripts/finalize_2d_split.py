"""Finalize 2D modeling split: save setup IDs per period, compute label counts."""
from __future__ import annotations

import json
import sys

import pandas as pd
from dotenv import load_dotenv

sys.path.insert(0, "src")
load_dotenv(".env")

from rade.backtest.walkforward import chronological_split
from rade.storage.db import make_session_factory
from rade.storage.models import LabeledSetup

sf = make_session_factory()
with sf() as session:
    rows = session.query(LabeledSetup).all()

# Eligible: non-recap, valid SL
eligible = [r for r in rows if not r.is_recap and r.stop_loss is not None and r.entry_price is not None]

# posted_at normalization
def norm_pa(r):
    pa = r.posted_at
    if pa is not None and getattr(pa, "tzinfo", None) is None:
        pa = pd.Timestamp(pa).tz_localize("UTC").to_pydatetime()
    return pa

# Build list of dicts with posted_at and message_pk
items = []
for r in eligible:
    items.append({"message_pk": r.message_pk, "posted_at": norm_pa(r)})

# Split by posted_at with 39h purge
split = chronological_split(items, ts_attr="posted_at", train_frac=0.6, val_frac=0.2, purge_hours=39.0)

# Save IDs
for name in ("train", "val", "test"):
    ids = [x["message_pk"] for x in split[name]]
    with open(f"data/split_{name}_ids.json", "w") as f:
        json.dump(ids, f)
    print(f"{name}: {len(ids)} setups, posted_at {min(x['posted_at'] for x in split[name])} .. {max(x['posted_at'] for x in split[name])}")

# Label counts: join with labeled setups to get exit_type
id_to_row = {r.message_pk: r for r in rows}

def label_counts(items, name):
    resolved = [r for r in (id_to_row[x["message_pk"]] for x in items) if r.exit_type in ("tp", "sl")]
    tp = sum(1 for r in resolved if r.exit_type == "tp")
    sl = sum(1 for r in resolved if r.exit_type == "sl")
    unfilled = sum(1 for r in (id_to_row[x["message_pk"]] for x in items) if r.exit_type == "unfilled")
    timeout = sum(1 for r in (id_to_row[x["message_pk"]] for x in items) if r.exit_type == "timeout")
    print(f"  {name}: total={len(items)} resolved(tp+sl)={len(resolved)} (tp={tp}, sl={sl}) unfilled={unfilled} timeout={timeout}")

print("\nLabel counts per period:")
for name in ("train", "val", "test"):
    label_counts(split[name], name)

# Direction balance per period
print("\nDirection balance per period:")
for name in ("train", "val", "test"):
    items = split[name]
    buys = sum(1 for x in items if id_to_row[x["message_pk"]].direction == "buy")
    sells = len(items) - buys
    print(f"  {name}: buy={buys} sell={sells} ({100*buys/len(items):.1f}% buy)")

# Feature eligibility: which setups have features?
feat_df = pd.read_csv("data/features.csv")
feat_pks = set(feat_df["message_pk"].tolist())
print(f"\nFeatures available for {len(feat_pks)} setups")
for name in ("train", "val", "test"):
    items = split[name]
    with_feat = sum(1 for x in items if x["message_pk"] in feat_pks)
    print(f"  {name}: {with_feat}/{len(items)} have features")

# Geometry feature note
print("\nGeometry features (risk_atr, tp_count, tp_dist_r_median) are derived from")
print("the trader's published setup. They are available for quality analysis but")
print("MUST NOT be used in the independent direction model (S1) unless S1 defines")
print("its own stop/target generation. Two feature sets will be created:")
print("  - features_full: all 30 features (for quality analysis)")
print("  - features_indep: 26 features (geometry excluded, for direction model)")
