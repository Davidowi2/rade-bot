"""Shadow study processing: mature forward-return rows with new bar data."""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone

import pandas as pd

sys.path.insert(0, "src")

STATE_FILE = "data/shadow_state.json"
OUTPUT_FILE = "data/shadow_study.csv"


def fetch_1m_bars(symbol="XAUUSD", lookback="3D"):
    from tradelocker import TLAPI
    from dotenv import load_dotenv
    load_dotenv(".env")
    import os as _os

    tl = TLAPI(
        environment=_os.getenv("TRADELOCKER_BASE_URL", "https://demo.tradelocker.com"),
        username=_os.getenv("TRADELOCKER_EMAIL"),
        password=_os.getenv("TRADELOCKER_PASSWORD"),
        server=_os.getenv("TRADELOCKER_SERVER"),
        account_id=int(_os.getenv("TRADELOCKER_ACCOUNT_ID")),
        log_level="warning",
    )
    iid = tl.get_instrument_id_from_symbol_name(symbol)
    df = tl.get_price_history(iid, resolution="1m", lookback_period=lookback)
    if df is None or df.empty:
        return pd.DataFrame()
    cols = {c.lower(): c for c in df.columns}
    df = df.rename(columns={
        cols.get("o", "o"): "open",
        cols.get("h", "h"): "high",
        cols.get("l", "l"): "low",
        cols.get("c", "c"): "close",
    })
    df["ts"] = pd.to_datetime(df[cols.get("t", "t")], unit="ms", utc=True)
    return df[["ts", "open", "high", "low", "close"]].sort_values("ts").reset_index(drop=True)


def fetch_d1_atr(symbol="XAUUSD", lookback="60D"):
    from tradelocker import TLAPI
    from dotenv import load_dotenv
    load_dotenv(".env")
    import os as _os

    tl = TLAPI(
        environment=_os.getenv("TRADELOCKER_BASE_URL", "https://demo.tradelocker.com"),
        username=_os.getenv("TRADELOCKER_EMAIL"),
        password=_os.getenv("TRADELOCKER_PASSWORD"),
        server=_os.getenv("TRADELOCKER_SERVER"),
        account_id=int(_os.getenv("TRADELOCKER_ACCOUNT_ID")),
        log_level="warning",
    )
    iid = tl.get_instrument_id_from_symbol_name(symbol)
    df = tl.get_price_history(iid, resolution="1D", lookback_period=lookback)
    if df is None or df.empty:
        return pd.DataFrame()
    cols = {c.lower(): c for c in df.columns}
    df = df.rename(columns={
        cols.get("o", "o"): "open",
        cols.get("h", "h"): "high",
        cols.get("l", "l"): "low",
        cols.get("c", "c"): "close",
    })
    df["ts"] = pd.to_datetime(df[cols.get("t", "t")], unit="ms", utc=True)
    df = df.sort_values("ts").reset_index(drop=True)
    high = df["high"]
    low = df["low"]
    close = df["close"]
    prev_close = close.shift(1)
    tr = pd.concat([high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1).max(axis=1)
    atr = tr.rolling(14).mean()
    df["atr_pct"] = atr / df["close"]
    return df[["ts", "atr_pct"]].dropna().reset_index(drop=True)


def get_atr_for_ts(d1_atr_df, ts):
    if d1_atr_df.empty:
        return None
    post_date = pd.Timestamp(ts).date() if not hasattr(ts, "tzinfo") else pd.Timestamp(ts).tz_convert("UTC").date()
    mask = d1_atr_df["ts"].dt.date < post_date
    if not mask.any():
        return None
    return float(d1_atr_df.loc[mask, "atr_pct"].iloc[-1])


def bar_at_or_after(bars_df, ts):
    mask = bars_df["ts"] >= ts
    if not mask.any():
        return None, None
    idx = int(bars_df[mask].index[0])
    return idx, bars_df.loc[idx, "ts"]


def bar_at_or_before(bars_df, ts):
    mask = bars_df["ts"] <= ts
    if not mask.any():
        return None
    return int(bars_df[mask].index[-1])


def check_price_reached(entry_price, direction, bars_df, posted_at, window_hours=24):
    mask = (bars_df["ts"] >= posted_at) & (bars_df["ts"] <= posted_at + timedelta(hours=window_hours))
    window = bars_df[mask]
    if len(window) == 0:
        return False, None
    if direction == "buy":
        touched = window["low"] <= entry_price
    else:
        touched = window["high"] >= entry_price
    if touched.any():
        first_idx = touched.values.argmax()
        first_touch_ts = window.iloc[first_idx]["ts"]
        return True, first_touch_ts
    return False, None


def compute_forward_returns(entry_ref, direction, bars_df, ref_ts, atr_pct):
    result = {}
    sign = 1 if direction == "buy" else -1
    HORIZONS = {"1h": 1, "2h": 2, "4h": 4, "8h": 8, "15h": 15}

    ref_idx, ref_bar_ts = bar_at_or_before(bars_df, ref_ts), None
    if ref_idx is not None:
        ref_bar_ts = bars_df.loc[ref_idx, "ts"]
    if ref_idx is None:
        return result

    atr_price = entry_ref * atr_pct if atr_pct and atr_pct > 0 else None

    for hname, hours in HORIZONS.items():
        target_ts = ref_bar_ts + timedelta(hours=hours)
        target_idx, _ = bar_at_or_after(bars_df, target_ts)
        if target_idx is not None and target_idx < len(bars_df):
            close = bars_df.loc[target_idx, "close"]
            fwd = (close - entry_ref) * sign
            result[f"fwd_ret_{hname}"] = round(float(fwd), 4)
            if atr_price and atr_price > 0:
                result[f"fwd_ret_{hname}_atr"] = round(float(fwd / atr_price), 4)
            else:
                result[f"fwd_ret_{hname}_atr"] = None
        else:
            result[f"fwd_ret_{hname}"] = None
            result[f"fwd_ret_{hname}_atr"] = None

    start_idx = ref_idx + 1
    end_ts = ref_bar_ts + timedelta(hours=15)
    end_idx = bar_at_or_before(bars_df, end_ts)
    if end_idx is not None and end_idx >= start_idx:
        window = bars_df.iloc[start_idx: end_idx + 1]
        if len(window) > 0:
            if sign == 1:
                mfe = max(0.0, float(window["high"].max() - entry_ref))
                mae = max(0.0, float(entry_ref - window["low"].min()))
            else:
                mfe = max(0.0, float(entry_ref - window["low"].min()))
                mae = max(0.0, float(window["high"].max() - entry_ref))
            result["mfe_15h"] = round(mfe, 4)
            result["mae_15h"] = round(mae, 4)
            if atr_price and atr_price > 0:
                result["mfe_15h_atr"] = round(mfe / atr_price, 4)
                result["mae_15h_atr"] = round(mae / atr_price, 4)
            else:
                result["mfe_15h_atr"] = None
                result["mae_15h_atr"] = None

            window_incl = bars_df.iloc[ref_idx: end_idx + 1]
            if sign == 1:
                mfe_incl = max(0.0, float(window_incl["high"].max() - entry_ref))
                mae_incl = max(0.0, float(entry_ref - window_incl["low"].min()))
            else:
                mfe_incl = max(0.0, float(entry_ref - window_incl["low"].min()))
                mae_incl = max(0.0, float(window_incl["high"].max() - entry_ref))
            result["mfe_15h_incl"] = round(mfe_incl, 4)
            result["mae_15h_incl"] = round(mae_incl, 4)

    result["fill_bar_ambiguous"] = True
    result["hit_sl_15h"] = None
    result["hit_tp1_15h"] = None
    return result


def main():
    if not os.path.exists(OUTPUT_FILE):
        print("No shadow study data yet.")
        return

    df = pd.read_csv(OUTPUT_FILE)
    unmatured = df[df["matured"] == False]
    print(f"Total rows: {len(df)}, unmatured: {len(unmatured)}")

    if len(unmatured) == 0:
        print("All rows matured.")
        return

    bars_df = fetch_1m_bars("XAUUSD", "3D")
    print(f"Fetched {len(bars_df)} 1m bars")

    d1_atr_df = fetch_d1_atr("XAUUSD", "60D")
    print(f"Fetched {len(d1_atr_df)} D1 bars with ATR")

    updated = 0
    for idx, row in unmatured.iterrows():
        entry_type = row["entry_type"]
        direction = row["direction"]
        entry_price = row["entry_price"]
        posted_at = pd.to_datetime(row["posted_at"], utc=True)
        sign = 1 if direction == "buy" else -1

        # For LIMIT: check if price reached (prospectively)
        if entry_type == "limit":
            if not row.get("price_reached", False):
                reached, touch_ts = check_price_reached(entry_price, direction, bars_df, posted_at)
                if reached:
                    df.at[idx, "price_reached"] = True
                    df.at[idx, "first_touch_ts"] = touch_ts.isoformat() if touch_ts else None
                else:
                    # Still not reached — skip, check again next run
                    continue
            # Use touch_ts as reference
            touch_ts = pd.to_datetime(df.at[idx, "first_touch_ts"], utc=True)
            ref_ts = touch_ts
        else:
            # NOW: reference is posted_at
            ref_ts = posted_at

        # Maturity check: 15h after reference (not posted_at)
        needed_ts = ref_ts + pd.Timedelta(hours=15, minutes=5)
        if bars_df["ts"].max() < needed_ts:
            continue  # not enough post-reference data

        atr_pct = get_atr_for_ts(d1_atr_df, posted_at)
        if atr_pct is None:
            atr_pct = row.get("d1_atr14_pct")
        if atr_pct is None or (isinstance(atr_pct, float) and pd.isna(atr_pct)):
            atr_pct = None

        fr = compute_forward_returns(entry_ref=entry_price, direction=direction,
                                    bars_df=bars_df, ref_ts=ref_ts, atr_pct=atr_pct)
        for k, v in fr.items():
            df.at[idx, k] = v

        # Update ATR
        if atr_pct is not None:
            df.at[idx, "d1_atr14_pct"] = atr_pct

        # R-normalized
        stop_loss = row["stop_loss"]
        stop_dist = abs(entry_price - stop_loss) if stop_loss is not None and not pd.isna(stop_loss) else None
        for h in ["1h", "2h", "4h", "8h", "15h"]:
            col = f"fwd_ret_{h}"
            rcol = f"fwd_ret_{h}_r"
            val = df.at[idx, col]
            if val is not None and not pd.isna(val) and stop_dist and stop_dist > 0:
                df.at[idx, rcol] = round(float(val) / stop_dist, 4)
            else:
                df.at[idx, rcol] = None

        df.at[idx, "matured"] = True
        updated += 1

    df.to_csv(OUTPUT_FILE, index=False)
    print(f"Updated {updated} rows. Matured: {df['matured'].sum()}/{len(df)}")


if __name__ == "__main__":
    main()
