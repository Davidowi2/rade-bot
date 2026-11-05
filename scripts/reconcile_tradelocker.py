"""Reconcile TradeLocker history with Telegram setups."""
from __future__ import annotations

import json
import sys
from datetime import timedelta

import pandas as pd

sys.path.insert(0, "src")

# Load TradeLocker history
tl = pd.read_csv("C:/Users/owida/Downloads/1793756969-trade_history.csv")
tl["close_time"] = pd.to_datetime(tl["close_time"], utc=True)
tl = tl[tl["symbol"] == "XAUUSD"].copy()
print(f"XAUUSD trades: {len(tl)}")

# Load labeled setups
ls = pd.read_csv("data/labeled_setups.csv")
ls["posted_at"] = pd.to_datetime(ls["posted_at"], utc=True)
ls = ls[ls["is_recap"] == False].copy()
print(f"Non-recap setups: {len(ls)}")

# Filter to Oct 2026 (overlap with TL history)
ls_oct = ls[(ls["posted_at"] >= pd.Timestamp("2026-10-01", tz="UTC")) &
            (ls["posted_at"] <= pd.Timestamp("2026-11-01", tz="UTC"))].copy()
print(f"Setups in Oct 2026: {len(ls_oct)}")

# Build candidate matches
PRICE_TOL = 3.0  # dollars
TIME_WINDOW_H = 24.0

matches = []
for _, trade in tl.iterrows():
    side = "buy" if trade["cmd"] == "BUY" else "sell"
    close_ts = trade["close_time"]
    open_price = trade["open_price"]

    # Candidate setups: same side, posted within time window before close
    cands = ls_oct[
        (ls_oct["direction"] == side) &
        (ls_oct["posted_at"] <= close_ts) &
        (ls_oct["posted_at"] >= close_ts - timedelta(hours=TIME_WINDOW_H))
    ].copy()

    # Price proximity
    cands["price_diff_entry"] = (cands["entry_price"] - open_price).abs()
    cands["price_diff_fill"] = (cands["fill_price"] - open_price).abs() if "fill_price" in cands.columns else float("inf")
    # use min of entry and fill diff
    cands["price_diff"] = cands[["price_diff_entry"]].min(axis=1)
    # also check fill_price where available
    if "fill_price" in cands.columns:
        fp = cands["fill_price"].fillna(cands["entry_price"])
        cands["price_diff"] = (fp - open_price).abs()

    # Filter by price tolerance
    cands = cands[cands["price_diff"] <= PRICE_TOL]

    if len(cands) == 0:
        matches.append({
            "order": trade["order"],
            "close_time": close_ts,
            "side": side,
            "open_price": open_price,
            "close_price": trade["close_price"],
            "volume": trade["true_volume"],
            "true_profit": trade["true_profit"],
            "storage": trade["storage"],
            "setup_pk": None,
            "setup_entry": None,
            "setup_fill": None,
            "price_diff": None,
            "posted_at": None,
            "confidence": "unmatched",
            "notes": "No candidate setup within price/time window",
        })
    elif len(cands) == 1:
        c = cands.iloc[0]
        conf = "high" if c["price_diff"] <= 1.0 else "medium"
        matches.append({
            "order": trade["order"],
            "close_time": close_ts,
            "side": side,
            "open_price": open_price,
            "close_price": trade["close_price"],
            "volume": trade["true_volume"],
            "true_profit": trade["true_profit"],
            "storage": trade["storage"],
            "setup_pk": int(c["message_pk"]),
            "setup_entry": c["entry_price"],
            "setup_fill": c["fill_price"] if pd.notna(c["fill_price"]) else None,
            "price_diff": round(float(c["price_diff"]), 2),
            "posted_at": c["posted_at"],
            "exit_type": c["exit_type"],
            "setup_R": c["R"],
            "hours_held": c["hours_held"],
            "confidence": conf,
            "notes": f"Single candidate, price diff {c['price_diff']:.2f}",
        })
    else:
        # Multiple candidates — pick closest price, mark medium/low
        cands = cands.sort_values("price_diff")
        best = cands.iloc[0]
        second = cands.iloc[1]
        if best["price_diff"] <= 1.0 and second["price_diff"] - best["price_diff"] > 0.5:
            conf = "high"
        elif best["price_diff"] <= 2.0:
            conf = "medium"
        else:
            conf = "low"
        matches.append({
            "order": trade["order"],
            "close_time": close_ts,
            "side": side,
            "open_price": open_price,
            "close_price": trade["close_price"],
            "volume": trade["true_volume"],
            "true_profit": trade["true_profit"],
            "storage": trade["storage"],
            "setup_pk": int(best["message_pk"]),
            "setup_entry": best["entry_price"],
            "setup_fill": best["fill_price"] if pd.notna(best["fill_price"]) else None,
            "price_diff": round(float(best["price_diff"]), 2),
            "posted_at": best["posted_at"],
            "exit_type": best["exit_type"],
            "setup_R": best["R"],
            "hours_held": best["hours_held"],
            "confidence": conf,
            "notes": f"{len(cands)} candidates; best diff {best['price_diff']:.2f}, next {second['price_diff']:.2f}",
        })

m_df = pd.DataFrame(matches)
print("\nConfidence distribution:")
print(m_df["confidence"].value_counts().to_dict())
print(f"\nMatched: {(m_df['confidence']!='unmatched').sum()}")
print(f"Unmatched: {(m_df['confidence']=='unmatched').sum()}")

# Save
m_df.to_csv("data/tradelocker_matches.csv", index=False)
print("Saved data/tradelocker_matches.csv")

# Summary stats
matched = m_df[m_df["confidence"] != "unmatched"]
print(f"\nMatched trades profit: {matched['true_profit'].sum():.2f}")
print(f"Unmatched trades profit: {m_df[m_df['confidence']=='unmatched']['true_profit'].sum():.2f}")
print(f"Total XAUUSD profit: {m_df['true_profit'].sum():.2f}")
