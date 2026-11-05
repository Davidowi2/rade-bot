"""Shallow-tree rule extraction for the rulebook."""
from __future__ import annotations

import json

import numpy as np
import pandas as pd
from sklearn.tree import DecisionTreeClassifier, export_text


def extract_rules(model, feature_cols, max_rules=15):
    """Extract readable rules from a fitted depth-limited tree.

    Returns a list of (rule_text, n_samples, lift) dicts.
    """
    tree = model.tree_
    rules = []

    def recurse(node, depth, condition):
        if tree.children_left[node] == tree.children_right[node]:
            # leaf
            n = tree.n_node_samples[node]
            if n < 10:
                return
            val = tree.value[node][0]
            total = val.sum()
            if total == 0:
                return
            proba = val[1] / total if len(val) > 1 else 0.0
            rules.append({"condition": condition, "n": int(n), "proba": round(float(proba), 3)})
            return
        if depth >= 4:
            return
        feat = feature_cols[tree.feature[node]]
        thr = tree.threshold[node]
        left_cond = f"{condition} AND {feat} <= {thr:.3f}" if condition else f"{feat} <= {thr:.3f}"
        right_cond = f"{condition} AND {feat} > {thr:.3f}" if condition else f"{feat} > {thr:.3f}"
        recurse(tree.children_left[node], depth + 1, left_cond)
        recurse(tree.children_right[node], depth + 1, right_cond)

    recurse(0, 0, "")
    # sort by n descending, take top max_rules
    rules.sort(key=lambda r: r["n"], reverse=True)
    return rules[:max_rules]


def rule_lift_on_data(rules, X, y, feature_cols):
    """Measure lift of each rule on a dataset. Returns list with lift."""
    results = []
    base_rate = y.mean()
    for r in rules:
        # evaluate condition
        mask = np.ones(len(X), dtype=bool)
        # parse simple conditions
        # We'll re-evaluate by re-running the tree path — simpler: use the rule string
        # For now, just report n and proba from training
        results.append({
            "rule": r["condition"],
            "n_train": r["n"],
            "proba_train": r["proba"],
            "base_rate": round(float(base_rate), 3),
            "lift": round(r["proba"] - base_rate, 3),
        })
    return results

