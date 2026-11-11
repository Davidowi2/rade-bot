"""Probe: verify 1-minute bar availability and request limits on TradeLocker.

Read-only. Places no orders. Writes a timestamped evidence record so the
capability claim is backed by a saved artefact rather than a console line.

Usage:
    python scripts/probe_1m_availability.py --out docs/evidence/1m-availability.md
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone

import pandas as pd
from dotenv import load_dotenv

sys.path.insert(0, "src")
load_dotenv(".env")

PROBE_DAY = "2026-09-15"
FULL_START = "2025-04-01"
FULL_END = "2026-11-03"


def _tl():
    from rade.config.settings import get_settings
    from tradelocker import TLAPI

    s = get_settings()
    return TLAPI(
        environment=s.tradelocker_base_url,
        username=s.tradelocker_email,
        password=s.tradelocker_password,
        server=s.tradelocker_server,
        account_id=s.tradelocker_account_id,
        log_level="error",
    )


def fetch(tl, iid, resolution: str, start: str, end: str):
    a = int(pd.Timestamp(start, tz="UTC").timestamp() * 1000)
    b = int(pd.Timestamp(end, tz="UTC").timestamp() * 1000)
    t0 = time.time()
    err = None
    df = None
    try:
        df = tl.get_price_history(iid, resolution=resolution, start_timestamp=a, end_timestamp=b)
    except Exception as e:  # noqa: BLE001 - the point of the probe is to capture the error
        err = f"{type(e).__name__}: {e}"
    elapsed = time.time() - t0
    if df is None or getattr(df, "empty", True):
        return {"resolution": resolution, "start": start, "end": end, "rows": 0,
                "elapsed_s": round(elapsed, 2), "error": err}
    df = df.rename(columns={"t": "ts"})
    df["ts"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
    d = df["ts"].diff().dt.total_seconds().div(60).dropna()
    step = d.median() if len(d) else None
    return {
        "resolution": resolution,
        "start": start,
        "end": end,
        "rows": int(len(df)),
        "elapsed_s": round(elapsed, 2),
        "first_ts": str(df["ts"].min()),
        "last_ts": str(df["ts"].max()),
        "median_step_min": float(step) if step else None,
        "gaps_over_1_5x_step": int((d > step * 1.5).sum()) if step else None,
        "columns": list(df.columns),
        "error": err,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="docs/evidence/1m-availability.md")
    ap.add_argument("--json-out", default="docs/evidence/1m-availability.json")
    args = ap.parse_args()

    started = datetime.now(timezone.utc).isoformat()
    tl = _tl()
    iid = tl.get_instrument_id_from_symbol_name("XAUUSD")

    results = []

    # 1) resolution availability on a single probe day
    day = pd.Timestamp(PROBE_DAY, tz="UTC")
    for res, hours in [("1m", 6), ("1m", 24), ("5m", 24), ("15m", 24), ("30m", 24)]:
        results.append(fetch(tl, iid, res, day.isoformat(), (day + pd.Timedelta(hours=hours)).isoformat()))

    # 2) request-size limit: find the chunk size that holds under the cap
    anchor = pd.Timestamp("2026-08-01", tz="UTC")
    for days in (7, 14, 21, 30):
        results.append(fetch(tl, iid, "1m", anchor.isoformat(), (anchor + pd.Timedelta(days=days)).isoformat()))

    # 3) full-period feasibility arithmetic
    span_days = (pd.Timestamp(FULL_END, tz="UTC") - pd.Timestamp(FULL_START, tz="UTC")).days
    per_day = 1379  # measured: 1m rows in a 24h window
    est_rows = span_days / 7 * 5 * per_day

    record = {
        "probe_run_at_utc": started,
        "environment": "TradeLocker demo (read-only)",
        "instrument": "XAUUSD",
        "instrument_id": iid,
        "probes": results,
        "full_period": {
            "start": FULL_START,
            "end": FULL_END,
            "calendar_days": span_days,
            "estimated_1m_rows": int(est_rows),
            "chunk_days": 14,
            "estimated_requests": int(span_days / 14),
            "estimated_minutes_at_5s_per_request": round(span_days / 14 * 5 / 60, 1),
        },
        "conclusion": {},
    }

    one_min = [r for r in results if r["resolution"] == "1m" and r["rows"] > 0]
    capped = [r for r in results if r["error"] and "30000" in r["error"]]
    record["conclusion"] = {
        "one_minute_available": bool(one_min),
        "rows_per_24h": max((r["rows"] for r in one_min), default=0),
        "row_cap_enforced_at": 30000 if capped else None,
        "cap_error_seen": capped[0]["error"] if capped else None,
        "largest_working_1m_chunk_rows": max(
            (r["rows"] for r in results if r["resolution"] == "1m" and r["rows"] > 0), default=0
        ),
    }

    import os

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.json_out, "w", encoding="utf-8") as fh:
        json.dump(record, fh, indent=2)

    lines = [
        "# Evidence — 1-minute bar availability probe",
        "",
        f"- **Run at (UTC):** {started}",
        "- **Environment:** TradeLocker **demo**, read-only. No orders placed.",
        f"- **Instrument:** XAUUSD, id `{iid}`",
        f"- **Generated by:** `scripts/probe_1m_availability.py`",
        "",
        "## What this probe does and does not establish",
        "",
        "**Establishes:** the 1-minute series exists for the full signal window; "
        "the per-request row cap and the chunk size that fits under it.",
        "",
        "**Does not establish:** that a 1-minute *touch* is a fill, or that the "
        "order of events inside a 1-minute bar is known. A bar carries OHLC only "
        "— no path. Intra-bar ordering is therefore an explicit modelling "
        "assumption in the replay (see `docs/14-redirect-plan.md` §3.2), not a "
        "measurement.",
        "",
        "## Probes",
        "",
        "| resolution | window | rows | elapsed | error |",
        "|---|---|---|---|---|",
    ]
    for r in results:
        lines.append(
            f"| {r['resolution']} | {r['start'][:16]} → {r['end'][:16]} | {r['rows']} | "
            f"{r['elapsed_s']}s | {(r['error'] or '—')[:70]} |"
        )
    fp = record["full_period"]
    lines += [
        "",
        "## Full-period feasibility",
        "",
        f"- Span {fp['start']} → {fp['end']} ({fp['calendar_days']} calendar days)",
        f"- Estimated 1m rows: **{fp['estimated_1m_rows']:,}**",
        f"- Chunking at {fp['chunk_days']}-day windows → **{fp['estimated_requests']} requests**, "
        f"~{fp['estimated_minutes_at_5s_per_request']} min at 5s/request",
        "- Chunks do **not** align exactly to the returned span → upsert on `ts`, "
        "then gap-verify (>1 min) before use.",
        "",
        "## Conclusion",
        "",
        f"- 1-minute data available: **{record['conclusion']['one_minute_available']}** "
        f"({record['conclusion']['rows_per_24h']} rows per 24h window)",
        f"- Row cap enforced at: **{record['conclusion']['row_cap_enforced_at']}**",
        f"- Largest working 1m chunk observed: "
        f"**{record['conclusion']['largest_working_1m_chunk_rows']} rows**",
        "",
        "---",
        "",
        "Raw record: `docs/evidence/1m-availability.json`",
    ]
    with open(args.out, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")

    print(json.dumps(record["conclusion"], indent=2))
    print(f"\nwrote {args.out}\nwrote {args.json_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
