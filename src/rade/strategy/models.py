"""Model training and evaluation for Phase 2D."""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.calibration import CalibratedClassifierCV
from sklearn.ensemble import GradientBoostingClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import brier_score_loss, roc_auc_score
from sklearn.preprocessing import StandardScaler
from sklearn.tree import DecisionTreeClassifier

GEOMETRY_FEATURES = ["risk_atr", "tp_count", "tp_dist_r_median", "entry_type_market"]


def load_data():
    """Load features, split IDs, and labeled setups."""
    feat = pd.read_csv("data/features.csv")
    labeled = pd.read_csv("data/labeled_setups.csv")
    with open("data/split_train_ids.json") as f:
        train_ids = json.load(f)
    with open("data/split_val_ids.json") as f:
        val_ids = json.load(f)
    with open("data/split_test_ids.json") as f:
        test_ids = json.load(f)
    return feat, labeled, train_ids, val_ids, test_ids


def prepare_features(feat, labeled, feature_set="full"):
    """Join features with labels. Returns DataFrame with target columns."""
    df = feat.merge(labeled[["message_pk", "direction", "exit_type"]], on="message_pk", how="inner")
    if feature_set == "indep":
        drop = [c for c in GEOMETRY_FEATURES if c in df.columns]
        df = df.drop(columns=drop)
    return df


def get_feature_cols(df, exclude=("message_pk", "posted_at", "direction", "exit_type")):
    return [c for c in df.columns if c not in exclude]


def make_models():
    return {
        "logistic": LogisticRegression(max_iter=1000, class_weight="balanced", random_state=42),
        "gradient_boosting": GradientBoostingClassifier(n_estimators=100, max_depth=3, random_state=42),
        "decision_tree": DecisionTreeClassifier(max_depth=4, class_weight="balanced", random_state=42),
    }


def evaluate(model, X, y, scaler=None):
    """Evaluate a fitted model. Returns dict of metrics."""
    Xs = scaler.transform(X) if scaler is not None else X
    proba = model.predict_proba(Xs)[:, 1]
    pred = model.predict(Xs)
    auc = roc_auc_score(y, proba) if len(set(y)) > 1 else 0.5
    brier = brier_score_loss(y, proba)
    acc = (pred == y).mean()
    baseline_acc = max(y.mean(), 1 - y.mean())
    return {
        "auc": round(float(auc), 4),
        "brier": round(float(brier), 4),
        "accuracy": round(float(acc), 4),
        "baseline_accuracy": round(float(baseline_acc), 4),
        "n": len(y),
        "positive_rate": round(float(y.mean()), 4),
    }


def train_and_evaluate(target, feature_set, train_ids, val_ids, test_ids, feat, labeled):
    """Train 3 models for a target, evaluate on train/val. Returns results dict."""
    df = prepare_features(feat, labeled, feature_set)
    feature_cols = get_feature_cols(df)

    # Split
    train_df = df[df["message_pk"].isin(train_ids)]
    val_df = df[df["message_pk"].isin(val_ids)]
    test_df = df[df["message_pk"].isin(test_ids)]

    # Target definition
    if target == "direction":
        y_col = "direction"
        # binary: buy=1, sell=0
        train_df = train_df.copy()
        val_df = val_df.copy()
        test_df = test_df.copy()
        train_df["y"] = (train_df["direction"] == "buy").astype(int)
        val_df["y"] = (val_df["direction"] == "buy").astype(int)
        test_df["y"] = (test_df["direction"] == "buy").astype(int)
    elif target == "quality":
        # resolved only: tp=1, sl=0
        train_df = train_df[train_df["exit_type"].isin(["tp", "sl"])].copy()
        val_df = val_df[val_df["exit_type"].isin(["tp", "sl"])].copy()
        test_df = test_df[test_df["exit_type"].isin(["tp", "sl"])].copy()
        train_df["y"] = (train_df["exit_type"] == "tp").astype(int)
        val_df["y"] = (val_df["exit_type"] == "tp").astype(int)
        test_df["y"] = (test_df["exit_type"] == "tp").astype(int)

    X_train = train_df[feature_cols].fillna(0)
    y_train = train_df["y"].values
    X_val = val_df[feature_cols].fillna(0)
    y_val = val_df["y"].values
    X_test = test_df[feature_cols].fillna(0)
    y_test = test_df["y"].values

    scaler = StandardScaler()
    scaler.fit(X_train)

    models = make_models()
    results = {}
    for name, model in models.items():
        # Train
        if name == "logistic":
            model.fit(scaler.transform(X_train), y_train)
        else:
            model.fit(X_train, y_train)

        train_m = evaluate(model, X_train, y_train, scaler if name == "logistic" else None)
        val_m = evaluate(model, X_val, y_val, scaler if name == "logistic" else None)

        results[name] = {
            "train": train_m,
            "val": val_m,
            "model": model,
            "scaler": scaler if name == "logistic" else None,
        }

    return results, feature_cols, (X_train, y_train, X_val, y_val, X_test, y_test, train_df, val_df, test_df)
