"""Shadow study collection: fetch new Telegram calls + 1m bars, compute forward returns."""
from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
from dotenv import load_dotenv

sys.path.insert(0, "src")
load_dotenv(".env")

STATE_FILE = "data/shadow_state.json"
OUTPUT_FILE = "data/shadow_study.csv"
REVIEW_FILE = "data/shadow_study_review_log.csv"

COLUMNS = [
    "message_id", "posted_at", "edited_at", "raw_text", "original_text", "is_recap",
    "entry_changed", "original_version_unknown", "review_status",
    "direction", "entry_type", "entry_price", "stop_loss", "take_profits",
    "executable_price_unknown", "price_reached", "first_touch_ts",
    "fill_bar_ambiguous",
    "fwd_ret_1h", "fwd_ret_2h", "fwd_ret_4h", "fwd_ret_8h", "fwd_ret_15h",
    "fwd_ret_1h_atr", "fwd_ret_2h_atr", "fwd_ret_4h_atr", "fwd_ret_8h_atr", "fwd_ret_15h_atr",
    "fwd_ret_1h_r", "fwd_ret_2h_r", "fwd_ret_4h_r", "fwd_ret_8h_r", "fwd_ret_15h_r",
    "mfe_15h", "mae_15h", "mfe_15h_incl", "mae_15h_incl",
    "mfe_15h_atr", "mae_15h_atr",
    "hit_sl_15h", "hit_tp1_15h",
    "d1_atr14_pct", "utc_hour", "weekday",
    "collected_at", "matured",
]


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {"last_message_id": 0, "last_bar_ts": None, "collected_count": 0,
            "recent_message_ids": []}


def save_state(state):
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2, default=str)


def fetch_messages(channel, since_id, recent_ids=None):
    """Fetch new messages AND re-fetch recent ones to detect edits."""
    from rade.ingestion.telegram.client import make_client
    from rade.config.settings import get_settings

    s = get_settings()
    ch = channel or s.telegram_channel

    async def _fetch():
        client = make_client()
        await client.start()
        entity = await client.get_entity(ch)
        msgs = []
        async for m in client.iter_messages(entity, min_id=since_id, limit=200, reverse=True):
            msgs.append(m)
        if recent_ids:
            for mid in recent_ids[-50:]:
                try:
                    m = await client.get_messages(entity, ids=mid)
                    if m is not None:
                        msgs.append(m)
                except Exception:
                    pass
        await client.disconnect()
        return msgs, ch

    return asyncio.run(_fetch())


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

    # MFE/MAE: exclude reference bar (primary), floored at 0
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

            # Sensitivity: include reference bar
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
    # SL/TP touch: deferred (unavailable from 1m bars without SL/TP history)
    result["hit_sl_15h"] = None
    result["hit_tp1_15h"] = None
    return result


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


def main():
    state = load_state()
    print(f"Last message ID: {state['last_message_id']}")

    from rade.config.settings import get_settings
    s = get_settings()
    channel = s.telegram_channel

    recent_ids = state.get("recent_message_ids", [])
    print(f"Fetching messages from {channel} since id {state['last_message_id']}...")
    msgs, ch = fetch_messages(channel, state["last_message_id"], recent_ids)
    print(f"Fetched {len(msgs)} messages")

    if not msgs:
        print("No messages.")
        return

    # Deduplicate by message id (keep latest version for edits)
    msg_map = {}
    for m in msgs:
        if m.id not in msg_map or (m.edit_date and (msg_map[m.id].edit_date is None or m.edit_date > msg_map[m.id].edit_date)):
            msg_map[m.id] = m
    msgs = list(msg_map.values())
    print(f"After dedup: {len(msgs)} unique messages")

    # Fetch 1m bars and D1 ATR
    print("Fetching 1m bars (3D lookback)...")
    bars_df = fetch_1m_bars("XAUUSD", "3D")
    print(f"Fetched {len(bars_df)} 1m bars")

    print("Fetching D1 ATR (60D lookback)...")
    d1_atr_df = fetch_d1_atr("XAUUSD", "60D")
    print(f"Fetched {len(d1_atr_df)} D1 bars with ATR")

    from rade.ingestion.telegram.parser import parse_signal

    rows = []
    max_msg_id = state["last_message_id"]
    recent_ids_updated = list(recent_ids)

    for m in msgs:
        max_msg_id = max(max_msg_id, m.id)
        if m.id not in recent_ids_updated:
            recent_ids_updated.append(m.id)
        recent_ids_updated = recent_ids_updated[-100:]

        parsed = parse_signal(m.message)
        if parsed is None:
            continue

        posted_at = m.date.astimezone(timezone.utc) if m.date else None
        if posted_at is None:
            continue

        entry_price = parsed.get("entry_price")
        if entry_price is None:
            continue

        direction = parsed.get("direction", "unknown")
        entry_type = parsed.get("entry_type", "unknown")
        stop_loss = parsed.get("stop_loss")
        take_profits = parsed.get("take_profit", [])
        edited_at = m.edit_date.astimezone(timezone.utc) if m.edit_date else None
        # Preserve original text for edits
        original_text = m.message or ""
        if os.path.exists(OUTPUT_FILE):
            try:
                ex_df = pd.read_csv(OUTPUT_FILE)
                ex_row = ex_df[ex_df["message_id"] == m.id]
                if len(ex_row) > 0 and "original_text" in ex_row.columns:
                    ot = ex_row["original_text"].iloc[0]
                    if pd.notna(ot):
                        original_text = str(ot)
            except Exception:
                pass

        # Detect entry change and original-version-unknown
        entry_changed = False
        original_version_unknown = False
        has_existing = False
        if os.path.exists(OUTPUT_FILE):
            try:
                ex_df = pd.read_csv(OUTPUT_FILE)
                ex_row = ex_df[ex_df["message_id"] == m.id]
                if len(ex_row) > 0:
                    has_existing = True
                    ex = ex_row.iloc[0]
                    if (str(ex.get("entry_price")) != str(entry_price) or
                        str(ex.get("direction")) != str(direction) or
                        str(ex.get("entry_type")) != str(entry_type) or
                        str(ex.get("stop_loss")) != str(stop_loss)):
                        entry_changed = True
            except Exception:
                pass

        # If edited but no prior version stored -> original unknown
        if edited_at is not None and not has_existing:
            original_version_unknown = True
            entry_changed = False  # can't determine if entry changed

        if not has_existing and edited_at is None:
            entry_changed = False

        is_recap = False  # manual flag, review later

        atr_pct = get_atr_for_ts(d1_atr_df, posted_at)

        row = {
            "message_id": m.id,
            "posted_at": posted_at.isoformat(),
            "edited_at": edited_at.isoformat() if edited_at else None,
            "raw_text": m.message or "",
            "original_text": original_text,
            "is_recap": is_recap,
            "entry_changed": entry_changed,
            "original_version_unknown": original_version_unknown,
            "review_status": "pending_original_unknown" if original_version_unknown else "pending",
            "direction": direction,
            "entry_type": entry_type,
            "entry_price": entry_price,
            "stop_loss": stop_loss,
            "take_profits": json.dumps(take_profits),
            "executable_price_unknown": (entry_type == "market"),
            "d1_atr14_pct": atr_pct,
            "utc_hour": posted_at.hour,
            "weekday": posted_at.weekday(),
            "collected_at": datetime.now(timezone.utc).isoformat(),
            "matured": False,
        }
        if entry_type == "limit":
            reached, touch_ts = check_price_reached(entry_price, direction, bars_df, posted_at)
            row["price_reached"] = reached
            row["first_touch_ts"] = touch_ts.isoformat() if touch_ts else None
            if reached and touch_ts:
                fr = compute_forward_returns(entry_price, direction, bars_df, touch_ts, atr_pct)
                row.update(fr)
            else:
                for h in ["1h", "2h", "4h", "8h", "15h"]:
                    row[f"fwd_ret_{h}"] = None
                    row[f"fwd_ret_{h}_atr"] = None
                    row[f"fwd_ret_{h}_r"] = None
                row["mfe_15h"] = None
                row["mae_15h"] = None
                row["mfe_15h_incl"] = None
                row["mae_15h_incl"] = None
                row["mfe_15h_atr"] = None
                row["mae_15h_atr"] = None
        else:
            row["price_reached"] = True
            row["first_touch_ts"] = None
            fr = compute_forward_returns(entry_price, direction, bars_df, posted_at, atr_pct)
            row.update(fr)

        row["fill_bar_ambiguous"] = True

        stop_dist = abs(entry_price - stop_loss) if stop_loss is not None else None
        for h in ["1h", "2h", "4h", "8h", "15h"]:
            col = f"fwd_ret_{h}"
            rcol = f"fwd_ret_{h}_r"
            if row.get(col) is not None and stop_dist and stop_dist > 0:
                row[rcol] = round(row[col] / stop_dist, 4)
            else:
                row[rcol] = None

        # SL/TP touch: deferred (unavailable from bars without SL/TP history)
        row["hit_sl_15h"] = None
        row["hit_tp1_15h"] = None

        rows.append(row)

    # Deduplicate: new messages appended; edits update only metadata fields
    if os.path.exists(OUTPUT_FILE):
        existing = pd.read_csv(OUTPUT_FILE)
        existing_ids = set(existing["message_id"].tolist())
        new_rows = [r for r in rows if r["message_id"] not in existing_ids]
        upd_rows = [r for r in rows if r["message_id"] in existing_ids]

        # For edits: update only metadata, preserve computed fields + review decisions
        if upd_rows:
            for ur in upd_rows:
                mid = ur["message_id"]
                mask = existing["message_id"] == mid
                if mask.any():
                    existing.loc[mask, "edited_at"] = ur.get("edited_at")
                    existing.loc[mask, "raw_text"] = ur.get("raw_text")
                    if "original_text" in existing.columns:
                        # preserve original_text if already set
                        if pd.isna(existing.loc[mask, "original_text"].iloc[0]):
                            existing.loc[mask, "original_text"] = ur.get("original_text")
                    existing.loc[mask, "entry_changed"] = ur.get("entry_changed", False)
                    # Do NOT overwrite: matured, review_status, fwd_ret_*, mfe, mae, d1_atr14_pct, etc.

        if new_rows:
            new_df = pd.DataFrame(new_rows, columns=COLUMNS)
            combined = pd.concat([existing, new_df], ignore_index=True)
        else:
            combined = existing
    else:
        combined = pd.DataFrame(rows, columns=COLUMNS)
        review_df = pd.DataFrame(columns=["message_id", "review_decision", "reviewer", "review_date", "notes"])
        review_df.to_csv(REVIEW_FILE, index=False)

    combined.to_csv(OUTPUT_FILE, index=False)
    print(f"Processed {len(rows)} messages. Total rows: {len(combined)}")

    state["last_message_id"] = max_msg_id
    state["last_bar_ts"] = bars_df["ts"].max().isoformat() if len(bars_df) > 0 else state["last_bar_ts"]
    state["collected_count"] = state.get("collected_count", 0) + len(rows)
    state["recent_message_ids"] = recent_ids_updated
    save_state(state)
    print(f"State updated. Last message ID: {max_msg_id}")


if __name__ == "__main__":
    main()
