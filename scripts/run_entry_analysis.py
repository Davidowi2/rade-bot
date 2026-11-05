"""Entry-quality pattern analysis (no exit simulation)."""
from __future__ import annotations

import json
import sys
from datetime import timedelta

import numpy as np
import pandas as pd
from scipy import stats

sys.path.insert(0, "src")

fr = pd.read_csv("data/forward_returns.csv")
fr["posted_at"] = pd.to_datetime(fr["posted_at"], utc=True)

# Load split IDs
import json as _json
with open("data/split_train_ids.json") as f:
    train_ids = set(_json.load(f))
with open("data/split_val_ids.json") as f:
    val_ids = set(_json.load(f))
with open("data/split_test_ids.json") as f:
    test_ids = set(_json.load(f))

fr["split"] = fr["message_pk"].apply(
    lambda pk: "train" if pk in train_ids else ("val" if pk in val_ids else ("test" if pk in test_ids else "unknown"))
)

print(f"Forward returns: {len(fr)} rows")
print(f"Split distribution: {fr['split'].value_counts().to_dict()}")

# Pre-specified patterns (limited set)
# Each returns a group label per row
def make_patterns(df):
    df = df.copy()

    # P0: Direction (baseline)
    df["pat_direction"] = df["direction"]

    # P1: Direction x UTC hour bucket
    def hour_bucket(h):
        if h < 8:
            return "asian"
        elif h < 13:
            return "london"
        elif h < 17:
            return "ny_am"
        else:
            return "ny_pm"
    df["pat_dir_hour"] = df["direction"] + "_" + df["utc_hour"].apply(hour_bucket)

    # P2: Direction x volatility regime (ATR percentile)
    atr_med = df["d1_atr14_pct"].median()
    df["vol_regime"] = df["d1_atr14_pct"].apply(lambda x: "high_vol" if x >= atr_med else "low_vol")
    df["pat_dir_vol"] = df["direction"] + "_" + df["vol_regime"]

    # P3: Direction x trend state (ema_stack)
    df["trend_state"] = df["d1_ema_stack"].apply(lambda x: "aligned" if x == 1 else "not_aligned")
    df["pat_dir_trend"] = df["direction"] + "_" + df["trend_state"]

    # P4: Direction x distance to week high (near vs far)
    wh_med = df["d1_dist_week_high_atr"].abs().median()
    df["near_week_high"] = df["d1_dist_week_high_atr"].abs().apply(lambda x: "near" if x <= wh_med else "far")
    df["pat_dir_weekhigh"] = df["direction"] + "_" + df["near_week_high"]

    # P5: Direction x risk_atr bucket (tight vs wide stop)
    risk_med = df["risk_atr"].median()
    df["risk_bucket"] = df["risk_atr"].apply(lambda x: "tight" if x <= risk_med else "wide")
    df["pat_dir_risk"] = df["direction"] + "_" + df["risk_bucket"]

    # P6: Direction x weekday (Mon-Fri)
    df["day_group"] = df["weekday"].apply(lambda x: "mon" if x == 0 else ("fri" if x == 4 else "mid"))
    df["pat_dir_day"] = df["direction"] + "_" + df["day_group"]

    return df

fr = make_patterns(fr)

PATTERNS = [
    "pat_direction",
    "pat_dir_hour",
    "pat_dir_vol",
    "pat_dir_trend",
    "pat_dir_weekhigh",
    "pat_dir_risk",
    "pat_dir_day",
]

HORIZONS = ["1h", "2h", "4h", "8h", "15h"]


def analyze_group(g, horizon):
    col = f"fwd_ret_{horizon}_atr"
    vals = g[col].dropna()
    n = len(vals)
    if n < 5:
        return None
    mean = vals.mean()
    se = vals.std(ddof=1) / np.sqrt(n)
    ci_lo = mean - stats.t.ppf(0.975, df=n-1) * se
    ci_hi = mean + stats.t.ppf(0.975, df=n-1) * se
    # secondary: R-normalized
    col_r = f"fwd_ret_{horizon}_r"
    vals_r = g[col_r].dropna()
    mean_r = vals_r.mean() if len(vals_r) > 0 else None
    # MFE/MAE (separate, not ratio)
    mfe = g["mfe_15h_atr"].dropna().mean()
    mae = g["mae_15h_atr"].dropna().mean()
    # descriptive touch rates
    tp_rate = g["hit_tp1_15h"].mean() if g["hit_tp1_15h"].notna().any() else None
    sl_rate = g["hit_sl_15h"].mean() if g["hit_sl_15h"].notna().any() else None
    return {
        "n": n,
        "mean_fwd_atr": round(float(mean), 4),
        "ci_95_lo": round(float(ci_lo), 4),
        "ci_95_hi": round(float(ci_hi), 4),
        "mean_fwd_r": round(float(mean_r), 4) if mean_r is not None else None,
        "mean_mfe_atr": round(float(mfe), 4),
        "mean_mae_atr": round(float(mae), 4),
        "hit_tp1_rate": round(float(tp_rate), 4) if tp_rate is not None else None,
        "hit_sl_rate": round(float(sl_rate), 4) if sl_rate is not None else None,
    }


def run_analysis(split_name):
    subset = fr[fr["split"] == split_name]
    results = {}
    for pat in PATTERNS:
        groups = subset.groupby(pat)
        pat_results = {}
        for gname, g in groups:
            group_data = {}
            for h in HORIZONS:
                r = analyze_group(g, h)
                if r is not None:
                    group_data[h] = r
            if group_data:
                pat_results[str(gname)] = group_data
        results[pat] = pat_results
    return results


train_results = run_analysis("train")
val_results = run_analysis("val")

# Build report
report = {
    "description": "Entry-quality analysis: forward price movement after published entry, no exit simulation.",
    "normalization": {
        "primary": "ATR-normalized (fwd_ret / (entry * d1_atr14_pct))",
        "secondary": "R-normalized (fwd_ret / published stop distance)",
    },
    "horizons": HORIZONS,
    "horizon_definition": "Completed bars after entry reference. 1h=2 bars, 2h=4 bars, 4h=8 bars, 8h=16 bars, 15h=30 bars. Anchored to first fully post-fill bar (fill bar excluded).",
    "patterns": PATTERNS,
    "patterns_note": "Pre-specified limited set. Direction baseline + 6 interaction patterns. No data-driven feature selection.",
    "caveats": [
        "Per-setup CIs are not independent across groups (overlapping market periods, shared bars).",
        "Multiple patterns tested on same data; no multiple-comparison correction applied.",
        "SL/TP touch rates are descriptive diagnostics only, not success labels or pattern-selection targets.",
        "MFE/MAE are separate measures; ratio not used as primary score (undefined when MAE=0).",
        "Train/val/test split by posted_at with 39h purge. Test period untouched (reviewed, not used here).",
        "Entry reference for LIMIT calls = published limit price. For NOW calls = published price (actual executable unknown).",
        "Fill bar excluded from MFE/MAE (primary). Fill-bar-inclusive reported as sensitivity in forward_returns.csv.",
        "False positive review: see data/false_positive_review.csv. No exclusions applied (0 candidates flagged).",
    ],
    "train": train_results,
    "val": val_results,
    "sample_sizes": {
        "train": int((fr["split"] == "train").sum()),
        "val": int((fr["split"] == "val").sum()),
        "test_untouched": int((fr["split"] == "test").sum()),
    },
}

with open("data/entry_analysis_report.json", "w") as f:
    json.dump(report, f, indent=2, default=str)
print("Saved data/entry_analysis_report.json")

# Print summary
print("\n=== TRAIN: Direction baseline ===")
for gname, gdata in train_results["pat_direction"].items():
    r15 = gdata.get("15h", {})
    print(f"  {gname}: n={r15.get('n')} mean_15h_atr={r15.get('mean_fwd_atr')} CI=[{r15.get('ci_95_lo')},{r15.get('ci_95_hi')}] mfe={r15.get('mean_mfe_atr')} mae={r15.get('mean_mae_atr')}")

print("\n=== VAL: Direction baseline ===")
for gname, gdata in val_results["pat_direction"].items():
    r15 = gdata.get("15h", {})
    print(f"  {gname}: n={r15.get('n')} mean_15h_atr={r15.get('mean_fwd_atr')} CI=[{r15.get('ci_95_lo')},{r15.get('ci_95_hi')}] mfe={r15.get('mean_mfe_atr')} mae={r15.get('mean_mae_atr')}")
