"""Observer step 3 — TradeLocker read-only checks, with the writes disarmed.

The plan's task 1 asks for five broker rows: quotes, positions, fills, a stop
modification, and a 1m history fetch. Four are read-only. The fifth needs an
order and is **not authorised**, so this script does not merely avoid it by
convention: it *disarms* every write method on the client before the first
call, so a mistake in this file cannot place, modify or close anything.

What it records
---------------
* session/account state, positions, working orders, executions (fills), quotes,
  instrument details, and the documented route rate limits;
* per-call latency measured with ``perf_counter`` — a duration needs one clock,
  not a correct one, so these numbers are valid even though the host clock is
  5.4h out;
* the reconciliation of ``get_all_positions()`` against the account state and
  against ``get_all_executions()`` where the two should agree.

Environment: refuses to run unless ``TRADELOCKER_ENV=demo``.

Usage
-----
    python scripts/verify_tradelocker_readonly.py
    python scripts/verify_tradelocker_readonly.py --symbol XAUUSD --csv path/to/history.csv
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

EVIDENCE = ROOT / "docs" / "evidence"

WRITE_METHODS = (
    "create_order",
    "modify_order",
    "modify_position",
    "close_position",
    "close_all_positions",
    "delete_order",
    "delete_all_orders",
    "delete_all_orders_manual",
)


class WriteAttempt(RuntimeError):
    """Raised if anything reaches for a broker write on a read-only probe."""


def disarm_writes(tl) -> list[str]:
    """Replace every mutating method with one that raises.

    Belt and braces: this probe has no reason to call these, and the point is
    that it *cannot*, not that it does not.
    """
    patched = []
    for name in WRITE_METHODS:
        if not hasattr(tl, name):
            continue

        def _blocked(*_a, _name=name, **_kw):
            raise WriteAttempt(f"{_name}() is disarmed: this probe is read-only")

        setattr(tl, name, _blocked)
        patched.append(name)
    return patched


def timed(label: str, fn, *a, **kw) -> dict:
    """Run one read-only call, recording duration and outcome."""
    t0 = time.perf_counter()
    try:
        value = fn(*a, **kw)
        return {
            "call": label,
            "ok": True,
            "ms": round((time.perf_counter() - t0) * 1000.0, 1),
            "value": _jsonable(value),
        }
    except Exception as exc:  # network, auth, rate limit - all recorded, not raised
        return {
            "call": label,
            "ok": False,
            "ms": round((time.perf_counter() - t0) * 1000.0, 1),
            "error": f"{type(exc).__name__}: {exc}",
            "traceback": traceback.format_exc(limit=3),
        }


def _rows(value) -> list[dict]:
    """Coerce a DataFrame or a list into records. The client returns both."""
    if value is None:
        return []
    if hasattr(value, "to_dict"):
        try:
            return value.to_dict("records")
        except Exception:
            return []
    if isinstance(value, list):
        return [r for r in value if isinstance(r, dict)]
    return []


def _jsonable(value):
    """Make a client return value safe to serialise, without guessing too hard."""
    if value is None or isinstance(value, (int, float, str, bool)):
        return value
    if hasattr(value, "to_dict"):
        try:
            return value.to_dict("records")
        except Exception:
            return f"<{type(value).__name__}>"
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(v) for v in value]
    return str(value)


def _rate_limits(tl) -> list:
    """The route rate limits come from the session config, not a per-route call."""
    cfg = tl.get_config()
    return cfg.get("rateLimits", []) if isinstance(cfg, dict) else []


def network_baseline(host: str) -> dict:
    """DNS, TCP and TLS cost for one connection, before any API work.

    Without this the API timings cannot be interpreted: an 8s quote fetch is a
    broker problem if the link is 50ms and a link problem if the handshake alone
    is 3s. Measured here so the two are not confused.
    """
    import socket
    import ssl

    out = {"host": host}
    try:
        t0 = time.perf_counter()
        socket.gethostbyname(host)
        out["dns_ms"] = round((time.perf_counter() - t0) * 1000.0, 1)
        t1 = time.perf_counter()
        sock = socket.create_connection((host, 443), timeout=20)
        out["tcp_ms"] = round((time.perf_counter() - t1) * 1000.0, 1)
        t2 = time.perf_counter()
        tls = ssl.create_default_context().wrap_socket(sock, server_hostname=host)
        out["tls_ms"] = round((time.perf_counter() - t2) * 1000.0, 1)
        tls.close()
        out["connect_total_ms"] = round((time.perf_counter() - t0) * 1000.0, 1)
    except Exception as exc:
        out["error"] = f"{type(exc).__name__}: {exc}"
    return out


def _num(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="XAUUSD")
    ap.add_argument("--csv", default=None,
                    help="broker trade-history CSV to reconcile executions against")
    ap.add_argument("--out-dir", default=str(EVIDENCE))
    args = ap.parse_args()

    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
    from rade.config.settings import get_settings

    s = get_settings()
    env = (s.tradelocker_env or "").lower()
    if env != "demo":
        print(f"REFUSING: TRADELOCKER_ENV={env!r}. This probe runs on demo only.")
        return 2
    if not (s.tradelocker_email and s.tradelocker_password and s.tradelocker_account_id):
        print("REFUSING: TradeLocker credentials are incomplete in .env")
        return 2

    from tradelocker import TLAPI

    import logging
    # the client logs a type-inference complaint for every frame it parses;
    # it is noise about its own pandas typing, not about our calls
    logging.getLogger("tradelocker").setLevel(logging.CRITICAL)
    logging.getLogger("tradelocker.tradelocker_api").setLevel(logging.CRITICAL)

    print("=" * 70)
    print("TRADELOCKER READ-ONLY PROBE — writes disarmed, no orders")
    print("=" * 70)

    t_construct = time.perf_counter()
    tl = TLAPI(
        environment=s.tradelocker_base_url,
        username=s.tradelocker_email,
        password=s.tradelocker_password,
        server=s.tradelocker_server,
        account_id=s.tradelocker_account_id,
        log_level="error",
    )
    construct_ms = round((time.perf_counter() - t_construct) * 1000.0, 1)
    disarmed = disarm_writes(tl)
    print(f"  disarmed: {', '.join(disarmed) or 'none found'}")
    print(f"  authenticated in {construct_ms:.0f}ms against {s.tradelocker_base_url}")

    results: list[dict] = []
    pace = [0.3]  # seconds between calls; replaced by the documented limits

    def probe(label, fn, *a, **kw):
        r = timed(label, fn, *a, **kw)
        results.append(r)
        flag = "ok " if r["ok"] else "ERR"
        print(f"  [{flag}] {label:32s} {r['ms']:7.1f}ms"
              + ("" if r["ok"] else f"  {r.get('error', '')[:80]}"))
        time.sleep(pace[0])  # respect the tightest documented route limit
        return r

    # the limits come from the session config, so they cost no route call and
    # can pace everything that follows: GET_POSITIONS is 1/second
    limits_r = probe("rate_limits", _rate_limits, tl)
    if limits_r["ok"]:
        pace[0] = max(
            (float(l.get("intervalNum", 1)) / float(l.get("limit", 1))
             for l in limits_r["value"] if l.get("limit")),
            default=1.0,
        ) + 0.05
        print(f"  pacing {pace[0]:.2f}s between calls (tightest documented limit)")

    iid_r = probe("instrument_id(XAUUSD)", tl.get_instrument_id_from_symbol_name, args.symbol)
    iid = iid_r.get("value") if iid_r["ok"] else None

    probe("accounts", tl.get_all_accounts)
    probe("account_state", tl.get_account_state)
    probe("positions", tl.get_all_positions)
    probe("orders", tl.get_all_orders)
    probe("executions", tl.get_all_executions)
    if iid is not None:
        probe("instrument_details", tl.get_instrument_details, iid)
        probe("quote_bid", tl.get_latest_bid_price, iid)
        probe("quote_ask", tl.get_latest_asking_price, iid)
        probe("quotes", tl.get_quotes, iid)

    # --- the disarmed writes must actually refuse -------------------------
    guard_checks = {}
    for name in ("create_order", "close_position", "modify_position"):
        try:
            getattr(tl, name)()
            guard_checks[name] = "FAILED - call did not raise"
        except WriteAttempt:
            guard_checks[name] = "refused"
        except Exception as exc:  # any other error still proves it never reached the broker
            guard_checks[name] = f"refused ({type(exc).__name__})"

    # --- reconciliation ---------------------------------------------------
    by_call = {r["call"]: r for r in results}
    positions = _rows(by_call.get("positions", {}).get("value"))
    executions = _rows(by_call.get("executions", {}).get("value"))
    state = by_call.get("account_state", {}).get("value") or {}

    recon = {
        "positions_reported": len(positions),
        "executions_reported": len(executions),
        "account_state_keys": sorted(state.keys()) if isinstance(state, dict) else None,
    }
    # a position's unrealised P&L should be visible in the account state
    if isinstance(state, dict) and positions:
        equity = _num(state.get("equity") or state.get("accountBalance"))
        balance = _num(state.get("balance") or state.get("accountBalance"))
        if equity is not None and balance is not None:
            recon["equity_minus_balance"] = round(equity - balance, 2)
            recon["sum_unrealised_pnl"] = round(
                sum(_num(p.get("unrealizedPnl")) or 0.0 for p in positions), 2)
            recon["agrees_within"] = 1.0
            recon["agrees"] = abs(
                recon["equity_minus_balance"] - recon["sum_unrealised_pnl"]) <= 1.0

    if args.csv:
        recon["csv"] = reconcile_csv(Path(args.csv), executions)

    n_ok = sum(1 for r in results if r["ok"])
    quotes = by_call.get("quotes", {}).get("value") or {}
    if isinstance(quotes, dict) and "ap" in quotes and "bp" in quotes:
        recon["quote_ap"] = quotes["ap"]
        recon["quote_bp"] = quotes["bp"]
        recon["spread"] = round(float(quotes["ap"]) - float(quotes["bp"]), 3)

    net = network_baseline(s.tradelocker_base_url.split("//")[-1])

    summary = {
        "probe": "tradelocker read-only",
        "read_only": True,
        "writes_disarmed": disarmed,
        "write_guard": guard_checks,
        "environment": env,
        "base_url": s.tradelocker_base_url,
        "account_id": s.tradelocker_account_id,
        "symbol": args.symbol,
        "instrument_id": iid,
        "auth_ms": construct_ms,
        "pace_s": round(pace[0], 2),
        "calls_ok": n_ok,
        "calls_failed": len(results) - n_ok,
        "latency_ms": {
            "p50": _pct([r["ms"] for r in results], 50),
            "p95": _pct([r["ms"] for r in results], 95),
            "max": max((r["ms"] for r in results), default=None),
        },
        "network_baseline": network_baseline(s.tradelocker_base_url.split("//")[-1]),
        "reconciliation": recon,
        "calls": [{k: v for k, v in r.items() if k != "traceback"} for r in results],
        "errors": [r for r in results if not r["ok"]],
        "verdict": _verdict(results, guard_checks, recon, net),
        "finished_at": datetime.now(timezone.utc).isoformat(),
    }

    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")
    path = out / f"tradelocker-readonly-{stamp}.json"
    path.write_text(json.dumps(summary, indent=2), encoding="utf-8")

    print("\n--- verdict ---")
    for k, v in summary["verdict"].items():
        print(f"  {k:14s}: {v}")
    print(f"\n  calls {n_ok}/{len(results)} ok; "
          f"latency p50 {summary['latency_ms']['p50']}ms "
          f"p95 {summary['latency_ms']['p95']}ms")
    print(f"wrote {path.relative_to(ROOT)}")
    return 0 if n_ok == len(results) else 1


def _pct(vals: list[float], p: float) -> float | None:
    if not vals:
        return None
    vals = sorted(vals)
    if len(vals) == 1:
        return vals[0]
    k = (len(vals) - 1) * p / 100.0
    lo = int(k)
    hi = min(lo + 1, len(vals) - 1)
    return round(vals[lo] + (vals[hi] - vals[lo]) * (k - lo), 1)


def reconcile_csv(csv_path: Path, executions) -> dict:
    """Compare broker executions with a trade-history export, where both exist."""
    try:
        import pandas as pd

        df = pd.read_csv(csv_path)
        df = df[df["symbol"].astype(str).str.upper().str.contains("XAU")] \
            if "symbol" in df.columns else df
        return {
            "csv": csv_path.name,
            "csv_rows": int(len(df)),
            "executions_rows": len(executions) if isinstance(executions, list) else None,
            "csv_columns": list(df.columns)[:20],
        }
    except Exception as exc:
        return {"csv": csv_path.name, "error": f"{type(exc).__name__}: {exc}"}


def _verdict(results, guard_checks, recon, net: dict | None = None) -> dict:
    failed = [r for r in results if not r["ok"]]
    by = {r["call"]: r for r in results}
    connect_ms = (net or {}).get("connect_total_ms")
    return {
        "writes": (
            "PASS - all write methods refuse to execute"
            if all(v == "refused" for v in guard_checks.values())
            else f"FAIL - guard did not hold: {guard_checks}"
        ),
        "auth": "PASS" if by.get("accounts", {}).get("ok") else "FAIL",
        "quotes": (
            "PASS" if by.get("quote_bid", {}).get("ok") else
            "FAIL" if "quote_bid" in by else "not attempted (no instrument id)"
        ),
        "positions": (
            "PASS - position list returned" if by.get("positions", {}).get("ok")
            else "FAIL"
        ),
        "executions": (
            "PASS - fills returned" if by.get("executions", {}).get("ok") else "FAIL"
        ),
        "latency": (
            "not measured"
            if connect_ms is None else
            f"INCONCLUSIVE on this host - a bare TLS connect to the broker costs "
            f"{connect_ms:.0f}ms, so API timings here are link-bound. "
            f"Re-measure on the deployment host"
            if connect_ms > 1000 else
            "measured on a fast link; attributable to the broker"
        ),
        "reconciliation": (
            "not applicable - no open positions"
            if recon.get("positions_reported") == 0 else
            "PASS - equity minus balance matches summed unrealised P&L"
            if recon.get("agrees") else
            "UNRESOLVED - equity and position P&L do not agree within 1.00"
        ),
        "errors": "none" if not failed else f"{len(failed)} call(s) failed",
    }


if __name__ == "__main__":
    raise SystemExit(main())
