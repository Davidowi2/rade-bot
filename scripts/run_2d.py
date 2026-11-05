"""Phase 2D: train direction and quality models, evaluate, extract rules."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from dotenv import load_dotenv

sys.path.insert(0, "src")
load_dotenv(".env")

from rade.backtest import BacktestEngine, CostModel
from rade.backtest.walkforward import chronological_split
from rade.labelling.models import Setup
from rade.strategy.models import (
    GEOMETRY_FEATURES,
    evaluate,
    get_feature_cols,
    load_data,
    make_models,
    prepare_features,
    train_and_evaluate,
)
from rade.strategy.rules import extract_rules
from rade.storage.db import make_session_factory
from rade.storage.models import Bar, LabeledSetup

sf = make_session_factory()
with sf() as session:
    bars = pd.DataFrame(
        [{"ts": b.ts, "open": b.open, "high": b.high, "low": b.low, "close": b.close, "volume": b.volume}
         for b in session.query(Bar).filter_by(timeframe="30m").all()]
    )
    labeled_rows = session.query(LabeledSetup).all()

bars["ts"] = pd.to_datetime(bars["ts"], utc=True)
bars = bars.sort_values("ts").reset_index(drop=True)

feat, labeled, train_ids, val_ids, test_ids = load_data()

# posted_at map
posted_at_map = {}
for r in labeled_rows:
    pa = r.posted_at
    if pa is not None and getattr(pa, "tzinfo", None) is None:
        pa = pd.Timestamp(pa).tz_localize("UTC").to_pydatetime()
    posted_at_map[r.message_pk] = pa

# helper: setups for backtest
def make_setups(id_list, quality_filter=None):
    setups = []
    for mid in id_list:
        r = next((x for x in labeled_rows if x.message_pk == mid), None)
        if r is None or r.is_recap or r.stop_loss is None or r.entry_price is None:
            continue
        if quality_filter is not None and mid not in quality_filter:
            continue
        tps = json.loads(r.take_profits) if r.take_profits else []
        pa = posted_at_map[mid]
        setups.append(Setup(message_pk=r.message_pk, posted_at=pa,
                            instrument=r.instrument or "XAUUSD", direction=r.direction,
                            entry_type=r.entry_type or "market", entry_price=r.entry_price,
                            stop_loss=r.stop_loss, take_profits=tps))
    return setups

# ============================================================
# 2D: Direction model (independent features, 26)
# ============================================================
print("=" * 70)
print("PHASE 2D — DIRECTION MODEL (independent features, 26)")
print("=" * 70)

dir_results, dir_feat_cols, dir_data = train_and_evaluate(
    "direction", "indep", train_ids, val_ids, test_ids, feat, labeled
)
X_tr, y_tr, X_val, y_val, X_te, y_te, tr_df, val_df, test_df = dir_data

print(f"\nFeature count: {len(dir_feat_cols)}")
print(f"Train: {len(y_tr)} (buy={y_tr.sum()}, sell={len(y_tr)-y_tr.sum()})")
print(f"Val:   {len(y_val)} (buy={y_val.sum()}, sell={len(y_val)-y_val.sum()})")
print(f"Test:  {len(y_te)} (buy={y_te.sum()}, sell={len(y_te)-y_te.sum()})")

print("\n--- Direction: Train/Val metrics ---")
for name in ("logistic", "gradient_boosting", "decision_tree"):
    tr = dir_results[name]["train"]
    va = dir_results[name]["val"]
    print(f"  {name}: train AUC={tr['auc']} Brier={tr['brier']} acc={tr['accuracy']} (base={tr['baseline_accuracy']})")
    print(f"           val   AUC={va['auc']} Brier={va['brier']} acc={va['accuracy']} (base={va['baseline_accuracy']})")

# baseline: majority class
majority_val = max(y_val.mean(), 1 - y_val.mean())
print(f"\n  Majority-class baseline val accuracy: {majority_val:.4f}")

# select best on val AUC
best_dir = max(dir_results, key=lambda n: dir_results[n]["val"]["auc"])
print(f"\n  Selected (val AUC): {best_dir} (AUC={dir_results[best_dir]['val']['auc']})")

# ============================================================
# 2D: Quality model (full features, 30, resolved subset)
# ============================================================
print("\n" + "=" * 70)
print("PHASE 2D — QUALITY MODEL (full features, 30, resolved TP/SL only)")
print("=" * 70)

qual_results, qual_feat_cols, qual_data = train_and_evaluate(
    "quality", "full", train_ids, val_ids, test_ids, feat, labeled
)
Xq_tr, yq_tr, Xq_val, yq_val, Xq_te, yq_te, tr_q, val_q, test_q = qual_data

print(f"\nFeature count: {len(qual_feat_cols)}")
print(f"Train: {len(yq_tr)} (TP={yq_tr.sum()}, SL={len(yq_tr)-yq_tr.sum()})")
print(f"Val:   {len(yq_val)} (TP={yq_val.sum()}, SL={len(yq_val)-yq_val.sum()})")
print(f"Test:  {len(yq_te)} (TP={yq_te.sum()}, SL={len(yq_te)-yq_te.sum()})")

print("\n--- Quality: Train/Val metrics ---")
for name in ("logistic", "gradient_boosting", "decision_tree"):
    tr = qual_results[name]["train"]
    va = qual_results[name]["val"]
    print(f"  {name}: train AUC={tr['auc']} Brier={tr['brier']} acc={tr['accuracy']} (base={tr['baseline_accuracy']})")
    print(f"           val   AUC={va['auc']} Brier={va['brier']} acc={va['accuracy']} (base={va['baseline_accuracy']})")

best_qual = max(qual_results, key=lambda n: qual_results[n]["val"]["auc"])
print(f"\n  Selected (val AUC): {best_qual} (AUC={qual_results[best_qual]['val']['auc']})")

# ============================================================
# Rule extraction (from train, select on val, measure on test)
# ============================================================
print("\n" + "=" * 70)
print("RULE EXTRACTION (direction, train → val → test)")
print("=" * 70)

# Use depth-4 tree trained on train
tree_model = dir_results["decision_tree"]["model"]
rules = extract_rules(tree_model, dir_feat_cols, max_rules=15)
print(f"Extracted {len(rules)} rules from training tree")
for r in rules[:10]:
    print(f"  {r['condition']} (n={r['n']}, proba={r['proba']})")

# ============================================================
# Final test evaluation (once)
# ============================================================
print("\n" + "=" * 70)
print("FINAL TEST EVALUATION (one-time)")
print("=" * 70)

# Direction
test_dir_m = evaluate(dir_results[best_dir]["model"], X_te, y_te,
                      dir_results[best_dir]["scaler"])
print(f"\nDirection ({best_dir}): test AUC={test_dir_m['auc']} Brier={test_dir_m['brier']} "
      f"acc={test_dir_m['accuracy']} base={test_dir_m['baseline_accuracy']} n={test_dir_m['n']}")

# Quality
test_qual_m = evaluate(qual_results[best_qual]["model"], Xq_te, yq_te,
                       qual_results[best_qual]["scaler"])
print(f"Quality ({best_qual}): test AUC={test_qual_m['auc']} Brier={test_qual_m['brier']} "
      f"acc={test_qual_m['accuracy']} base={test_qual_m['baseline_accuracy']} n={test_qual_m['n']}")

# ============================================================
# Risk-limited strategy backtest on test (S1: quality-filtered S0)
# ============================================================
print("\n" + "=" * 70)
print("STRATEGY BACKTEST ON TEST (S1 = quality-filtered S0, risk stops)")
print("=" * 70)

# Quality predictions on test setups
test_setups_all = make_setups(test_ids)
quality_model = qual_results[best_qual]["model"]
quality_scaler = qual_results[best_qual]["scaler"]

# Predict quality for each test setup
quality_good_ids = set()
for s in test_setups_all:
    # find features for this setup
    row = feat[feat["message_pk"] == s.message_pk]
    if len(row) == 0:
        continue
    X = row[qual_feat_cols].fillna(0).values
    if quality_scaler is not None:
        X = quality_scaler.transform(X)
    proba = quality_model.predict_proba(X)[:, 1][0]
    if proba >= 0.5:
        quality_good_ids.add(s.message_pk)

print(f"Test setups: {len(test_setups_all)}, quality-filtered (good): {len(quality_good_ids)}")

# Run S1 (quality-filtered) with risk stops
cm = CostModel(spread=0.25, slippage=0.25, slippage_enabled=True)
s1_setups = make_setups(test_ids, quality_filter=quality_good_ids)
eng_s1 = BacktestEngine(bars=bars, setups=s1_setups, cost_model=cm, risk_pct=0.005,
                        max_hold_hours=15.0, move_sl_to_be=False,
                        max_daily_loss_pct=0.02, max_dd_pct=0.05, max_open_positions=2)
res_s1 = eng_s1.run()
m_s1 = res_s1.metrics()
print(f"\nS1 (quality-filtered, risk stops):")
for k, v in m_s1.items():
    print(f"  {k}: {v}")

# Run S0 on same test dates with risk stops (for fair comparison)
s0_test_setups = make_setups(test_ids)
eng_s0_risk = BacktestEngine(bars=bars, setups=s0_test_setups, cost_model=cm, risk_pct=0.005,
                              max_hold_hours=15.0, move_sl_to_be=False,
                              max_daily_loss_pct=0.02, max_dd_pct=0.05, max_open_positions=2)
res_s0_risk = eng_s0_risk.run()
m_s0_risk = res_s0_risk.metrics()
print(f"\nS0 (raw, risk stops) on test:")
for k, v in m_s0_risk.items():
    print(f"  {k}: {v}")

# ============================================================
# Save report
# ============================================================
report = {
    "direction": {
        "feature_set": "indep (26)",
        "best_model": best_dir,
        "val_auc": dir_results[best_dir]["val"]["auc"],
        "test": test_dir_m,
        "models": {n: {"train": dir_results[n]["train"], "val": dir_results[n]["val"]} for n in dir_results},
    },
    "quality": {
        "feature_set": "full (30)",
        "best_model": best_qual,
        "val_auc": qual_results[best_qual]["val"]["auc"],
        "test": test_qual_m,
        "models": {n: {"train": qual_results[n]["train"], "val": qual_results[n]["val"]} for n in qual_results},
    },
    "rules": rules,
    "strategy_backtest": {
        "S1_quality_filtered": m_s1,
        "S0_raw_risk_stops": m_s0_risk,
    },
    "assumptions": {
        "slippage": "0.25 (unverified, base case)",
        "risk_pct": 0.005,
        "max_daily_loss": 0.02,
        "max_dd": 0.05,
        "max_open_positions": 2,
        "session_filter": "omitted (first comparison)",
        "geometry_features_excluded_from_direction": True,
    },
}

with open("data/2d_report.json", "w") as f:
    json.dump(report, f, indent=2, default=str)
print("\nSaved data/2d_report.json")
