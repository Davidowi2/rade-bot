"""Fetch 1-minute XAUUSD bars into the database, in verified chunks.

    python scripts/fetch_1m_bars.py --start 2025-04-01 --end 2026-11-03

Read-only against the broker: it calls ``get_price_history`` and nothing
else. No order is placed, no position is touched. It writes bars into the
``bars`` table with ``timeframe='1m'``, alongside the existing 30m / 1H / 1D
series, which it does not modify.

Why 14-day chunks
-----------------
TradeLocker enforces a **30,000-row** cap per request. A 1-minute window of
14 days is 20,160 minutes; the broker returns ~13,772 rows for it (the market
is closed at weekends, so fewer minutes exist than the calendar implies). A
21-day window is 30,240 minutes and **raises** ``ValueError: No. of requested
rows (30240) larger than max allowed (30000)``. This was measured, not
assumed - see ``docs/evidence/1m-availability.md``.

Resumability
------------
Each chunk is upserted on ``ts``, so re-running is safe and only fills what
is missing. Chunks that fail are recorded and retried; the final gap
verification reports anything still unresolved.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone

import pandas as pd
from dotenv import load_dotenv

sys.path.insert(0, "src")
load_dotenv(".env")

CHUNK_DAYS = 14
DB = "data/rade.db"
TIMEFRAME = "1m"


def _tl():
    from rade.config.settings import get_settings
    from tradelocker import TLAPI

    s = get_settings()

    # The broker client hard-codes a (10s connect, 30s read) timeout, which is
    # tight through a proxy for a 14-day 1m response. Loosen it, and retry the
    # construction itself: an initialisation failure is a network event, not a
    # reason to abandon a 42-request fetch that is designed to be resumable.
    last_err: Exception | None = None
    for attempt in range(1, 6):
        try:
            TLAPI._TIMEOUT = (30, 120)
            return TLAPI(
                environment=s.tradelocker_base_url,
                username=s.tradelocker_email,
                password=s.tradelocker_password,
                server=s.tradelocker_server,
                account_id=s.tradelocker_account_id,
                log_level="error",
            )
        except Exception as e:  # noqa: BLE001
            last_err = e
            print(f"  client init attempt {attempt}/5 failed: {type(e).__name__}: {e}")
            time.sleep(5.0 * attempt)
    raise RuntimeError(f"could not connect to the broker after 5 attempts: {last_err}")


def chunks(start: pd.Timestamp, end: pd.Timestamp, days: int):
    step = timedelta(days=days)
    cur = start
    while cur < end:
        nxt = min(cur + step, end)
        yield cur, nxt
        cur = nxt


def fetch_chunk(tl, iid: int, start: pd.Timestamp, end: pd.Timestamp, retries: int = 3):
    """Fetch one chunk. Returns (dataframe, error_string)."""
    a = int(start.timestamp() * 1000)
    b = int(end.timestamp() * 1000)
    last_err = None
    for attempt in range(1, retries + 1):
        try:
            df = tl.get_price_history(iid, resolution=TIMEFRAME, start_timestamp=a, end_timestamp=b)
            if df is None:
                last_err = "empty response"
            else:
                return df, None
        except Exception as e:  # noqa: BLE001
            last_err = f"{type(e).__name__}: {e}"
        if attempt < retries:
            time.sleep(2.0 * attempt)
    return None, last_err


def upsert(con: sqlite3.Connection, iid: int, df: pd.DataFrame) -> int:
    """Bulk upsert on (instrument_id, timeframe, ts). Returns rows written."""
    if df is None or df.empty:
        return 0
    d = df.rename(columns={"t": "ts", "o": "open", "h": "high", "l": "low", "c": "close", "v": "volume"})
    d["ts"] = pd.to_datetime(d["ts"], unit="ms", utc=True)
    rows = [
        (
            iid,
            TIMEFRAME,
            r.ts.strftime("%Y-%m-%d %H:%M:%S.%f"),
            float(r.open), float(r.high), float(r.low), float(r.close),
            float(r.volume) if pd.notna(getattr(r, "volume", None)) else None,
        )
        for r in d.itertuples()
    ]
    con.executemany(
        """
        INSERT INTO bars (instrument_id, timeframe, ts, open, high, low, close, volume)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(instrument_id, timeframe, ts) DO UPDATE SET
            open=excluded.open, high=excluded.high, low=excluded.low,
            close=excluded.close, volume=excluded.volume
        """,
        rows,
    )
    con.commit()
    return len(rows)


def verify(con: sqlite3.Connection) -> dict:
    """Gap analysis over the stored 1m series."""
    ts = pd.read_sql(
        "SELECT ts FROM bars WHERE timeframe='1m' ORDER BY ts", con
    )["ts"]
    if ts.empty:
        return {"n": 0}
    t = pd.to_datetime(ts, utc=True)
    d = t.diff().dt.total_seconds().div(60).dropna()
    step = d.median() if len(d) else None
    intraday = d[d <= 180]  # ignore weekend/holiday closures
    return {
        "n": int(len(t)),
        "first": str(t.min()),
        "last": str(t.max()),
        "median_step_min": float(step),
        "gaps_over_1m": int((d > 1.0).sum()),
        "gaps_over_5m_intraday": int((intraday > 5).sum()),
        "max_gap_min": float(d.max()),
        "n_gaps_over_1h": int((d > 60).sum()),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2025-04-01")
    ap.add_argument("--end", default="2026-11-03")
    ap.add_argument("--db", default=DB)
    ap.add_argument("--chunk-days", type=int, default=CHUNK_DAYS)
    ap.add_argument("--out", default="docs/evidence/1m-fetch.json")
    ap.add_argument("--dry-run", action="store_true", help="plan the chunks, fetch nothing")
    args = ap.parse_args()

    start = pd.Timestamp(args.start, tz="UTC")
    end = pd.Timestamp(args.end, tz="UTC")

    plan = list(chunks(start, end, args.chunk_days))
    print(f"plan: {len(plan)} chunks of {args.chunk_days} days")
    print(f"      {start.date()} -> {end.date()}")
    if args.dry_run:
        for i, (a, b) in enumerate(plan, 1):
            print(f"  {i:3d}  {a.date()} -> {b.date()}")
        return 0

    tl = _tl()
    iid = tl.get_instrument_id_from_symbol_name("XAUUSD")
    print(f"instrument id: {iid}")

    con = sqlite3.connect(args.db)
    inst = con.execute("SELECT id FROM instruments WHERE symbol='XAUUSD'").fetchone()
    if inst is None:
        con.execute(
            "INSERT INTO instruments (symbol, broker, asset_class) VALUES ('XAUUSD','tradelocker','metal')"
        )
        con.commit()
        inst = con.execute("SELECT id FROM instruments WHERE symbol='XAUUSD'").fetchone()
    iid_local = int(inst[0])

    results = []
    total_rows = 0
    t_start = time.time()
    for i, (a, b) in enumerate(plan, 1):
        df, err = fetch_chunk(tl, iid, a, b)
        if err:
            print(f"  [{i:3d}/{len(plan)}] {a.date()} -> {b.date()}  FAILED  {err}")
            results.append({"start": str(a), "end": str(b), "rows": 0, "error": err})
            continue
        n = upsert(con, iid_local, df)
        total_rows += n
        print(f"  [{i:3d}/{len(plan)}] {a.date()} -> {b.date()}  {n:>6,} rows")
        results.append({"start": str(a), "end": str(b), "rows": n, "error": None})

    elapsed = time.time() - t_start
    failed = [r for r in results if r["error"]]
    v = verify(con)
    con.close()

    print()
    print(f"fetched {total_rows:,} rows in {elapsed:.0f}s ({len(results) - len(failed)}/{len(plan)} chunks ok)")
    print(f"stored 1m bars : {v.get('n', 0):,}")
    print(f"first / last   : {v.get('first')} -> {v.get('last')}")
    print(f"median step    : {v.get('median_step_min')} min")
    print(f"gaps > 1m      : {v.get('gaps_over_1m')}")
    print(f"gaps > 5m (intraday only): {v.get('gaps_over_5m_intraday')}")
    print(f"gaps > 1h      : {v.get('n_gaps_over_1h')}  (weekends and holidays)")

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "resolution": TIMEFRAME,
        "instrument_id": iid,
        "start": str(start),
        "end": str(end),
        "chunk_days": args.chunk_days,
        "chunks_total": len(plan),
        "chunks_failed": len(failed),
        "rows_fetched": total_rows,
        "elapsed_s": round(elapsed, 1),
        "verification": v,
        "chunks": results,
    }
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2)
    print(f"\nwrote {args.out}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
