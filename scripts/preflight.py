"""Preflight: is this host fit to measure or run the observer?

Two host defects were found by hand today — a clock 5.4h slow because Windows
Time is stopped, and a network that spends 2–5s on a TLS handshake. Both were
discovered *after* measurements had been taken and had to be thrown away. This
script turns those two discoveries into gates that run **before** anything is
measured, so the next person does not repeat the sequence.

Every check returns PASS / WARN / FAIL with the measured value and the
threshold it was judged against. Exit code is 1 if any check FAILs.

Read-only: no message sent, no order, no write to the database.

Usage
-----
    python scripts/preflight.py
    python scripts/preflight.py --with-telegram --with-broker
    python scripts/preflight.py --json
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import sqlite3
import ssl
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

EVIDENCE = ROOT / "docs" / "evidence"

# thresholds, each with the reason it is what it is
CLOCK_TOLERANCE_MS = 2000.0     # the reception budget is p95 < 2s
TLS_WARN_MS = 500.0             # a healthy handshake; above this, timings are link-bound
TLS_FAIL_MS = 2000.0            # above this, latency cannot be attributed to the peer
HOSTS = ("api.telegram.org", "demo.tradelocker.com", "www.cloudflare.com")


def check_clock() -> dict:
    """Host clock vs an HTTP Date header. The observer cannot time anything without this."""
    import email.utils

    import requests

    try:
        t0 = time.time()
        resp = requests.head("https://www.cloudflare.com", timeout=(10, 25))
        t1 = time.time()
        header = resp.headers.get("Date")
        if not header:
            return _result("clock", "WARN", "no Date header returned", None, CLOCK_TOLERANCE_MS)
        server = email.utils.parsedate_to_datetime(header).timestamp()
        skew_ms = (server + 0.5 + (t1 - t0) / 2.0 - (t0 + t1) / 2.0) * 1000.0
        status = "PASS" if abs(skew_ms) <= CLOCK_TOLERANCE_MS else "FAIL"
        return _result(
            "clock", status,
            f"host is {skew_ms / 1000.0:+.1f}s from true UTC"
            + ("" if status == "PASS" else
               " - enable NTP (w32time on Windows) before measuring latency"),
            round(skew_ms, 1), CLOCK_TOLERANCE_MS,
        )
    except Exception as exc:
        return _result("clock", "WARN", f"could not measure: {type(exc).__name__}", None,
                       CLOCK_TOLERANCE_MS)


def check_network() -> dict:
    """DNS/TCP/TLS cost per host. A slow link invalidates every API timing."""
    rows = []
    for host in HOSTS:
        row = {"host": host}
        try:
            t0 = time.perf_counter()
            socket.gethostbyname(host)
            row["dns_ms"] = round((time.perf_counter() - t0) * 1000.0, 1)
            t1 = time.perf_counter()
            sock = socket.create_connection((host, 443), timeout=20)
            row["tcp_ms"] = round((time.perf_counter() - t1) * 1000.0, 1)
            t2 = time.perf_counter()
            tls = ssl.create_default_context().wrap_socket(sock, server_hostname=host)
            row["tls_ms"] = round((time.perf_counter() - t2) * 1000.0, 1)
            tls.close()
        except Exception as exc:
            row["error"] = f"{type(exc).__name__}: {exc}"
        rows.append(row)

    tls = [r["tls_ms"] for r in rows if "tls_ms" in r]
    worst = max(tls) if tls else None
    if any("error" in r for r in rows):
        status = "FAIL"
        detail = "at least one host could not be reached; timings here are not trustworthy"
    elif worst is not None and worst > TLS_FAIL_MS:
        status = "FAIL"
        detail = f"worst handshake {worst:.0f}ms; API timings are link-bound, not peer-bound"
    elif worst is not None and worst > TLS_WARN_MS:
        status = "WARN"
        detail = f"worst handshake {worst:.0f}ms; usable, but latency comparisons will be noisy"
    else:
        status = "PASS"
        detail = f"worst handshake {worst:.0f}ms"
    out = _result("network", status, detail, worst, TLS_FAIL_MS)
    out["hosts"] = rows
    return out


def check_environment() -> dict:
    """Credentials present, and no live account in play."""
    from rade.config.settings import get_settings

    s = get_settings()
    missing = [
        name for name, value in (
            ("TELEGRAM_API_ID", s.telegram_api_id),
            ("TELEGRAM_API_HASH", getattr(s, "telegram_api_hash", "")),
            ("TRADELOCKER_EMAIL", s.tradelocker_email),
            ("TRADELOCKER_ACCOUNT_ID", s.tradelocker_account_id),
        ) if not value
    ]
    env = (s.tradelocker_env or "").lower()
    if missing:
        return _result("environment", "FAIL", f"missing: {', '.join(missing)}", None, None)
    if env != "demo":
        return _result("environment", "FAIL",
                       f"TRADELOCKER_ENV={env!r}; the observer runs on demo only", None, None)
    return _result("environment", "PASS",
                   f"credentials present; broker environment is {env}", None, None)


def check_database() -> dict:
    """The local store opens, is not corrupt, and holds the bars the replay used."""
    db = ROOT / "data" / "rade.db"
    if not db.exists():
        return _result("database", "FAIL", f"{db.name} not found", None, None)
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            integrity = con.execute("pragma integrity_check").fetchone()[0]
            bars = con.execute("select count(*) from bars").fetchone()[0]
            msgs = con.execute("select count(*) from raw_telegram_messages").fetchone()[0]
        finally:
            con.close()
    except Exception as exc:
        return _result("database", "FAIL", f"{type(exc).__name__}: {exc}", None, None)
    if integrity != "ok":
        return _result("database", "FAIL", f"integrity_check: {integrity}", None, None)
    return _result("database", "PASS",
                   f"integrity ok; {bars:,} bars, {msgs:,} raw messages", None, None)


def check_telegram() -> dict:
    """Session authorises and the channel resolves. One connect, no reads."""
    import asyncio

    from rade.ingestion.telegram.client import make_client

    async def _go():
        client = make_client()
        try:
            await client.connect()
            if not await client.is_user_authorized():
                return "session is not authorised; run the interactive login once", None
            entity = await client.get_entity(
                os.getenv("TELEGRAM_CHANNEL", "@XAUUSD_FOREX4"))
            async for m in client.iter_messages(entity, limit=1):
                return None, m.id
            return None, None
        finally:
            try:
                await client.disconnect()
            except Exception:
                pass

    try:
        problem, latest = asyncio.run(_go())
    except Exception as exc:
        return _result("telegram", "FAIL", f"{type(exc).__name__}: {exc}", None, None)
    if problem:
        return _result("telegram", "FAIL", problem, None, None)
    return _result("telegram", "PASS", f"authorised; latest message id {latest}", latest, None)


def check_broker() -> dict:
    """Demo login and account state. Slow by nature; run it deliberately."""
    from tradelocker import TLAPI

    from rade.config.settings import get_settings

    s = get_settings()
    try:
        t0 = time.perf_counter()
        tl = TLAPI(
            environment=s.tradelocker_base_url,
            username=s.tradelocker_email,
            password=s.tradelocker_password,
            server=s.tradelocker_server,
            account_id=s.tradelocker_account_id,
            log_level="error",
        )
        login_ms = round((time.perf_counter() - t0) * 1000.0, 1)
        state = tl.get_account_state()
    except Exception as exc:
        return _result("broker", "FAIL", f"{type(exc).__name__}: {exc}", None, None)
    balance = state.get("balance") if isinstance(state, dict) else None
    status = "PASS" if login_ms < 10_000 else "WARN"
    return _result("broker", status,
                   f"demo login {login_ms:.0f}ms, balance {balance}", login_ms, 10_000.0)


def _result(name: str, status: str, detail: str, value, threshold) -> dict:
    return {"check": name, "status": status, "detail": detail,
            "value": value, "threshold": threshold}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--with-telegram", action="store_true")
    ap.add_argument("--with-broker", action="store_true")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--out-dir", default=str(EVIDENCE))
    args = ap.parse_args()

    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")

    checks = [check_environment(), check_database(), check_clock(), check_network()]
    if args.with_telegram:
        checks.append(check_telegram())
    if args.with_broker:
        checks.append(check_broker())

    failed = [c for c in checks if c["status"] == "FAIL"]
    warned = [c for c in checks if c["status"] == "WARN"]
    summary = {
        "probe": "preflight",
        "read_only": True,
        "host": socket.gethostname(),
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "checks": checks,
        "failed": [c["check"] for c in failed],
        "warned": [c["check"] for c in warned],
        "verdict": (
            "FAIL - this host must not be used to measure latency or run the observer"
            if failed else
            "PASS with warnings" if warned else
            "PASS - fit to measure"
        ),
    }

    if args.json:
        print(json.dumps(summary, indent=2))
    else:
        print("=" * 74)
        print(f"PREFLIGHT — {summary['host']}")
        print("=" * 74)
        for c in checks:
            print(f"  {c['status']:4s}  {c['check']:12s}  {c['detail']}")
            for row in c.get("hosts", []):
                if "error" in row:
                    print(f"          {row['host']:24s} {row['error'][:60]}")
                else:
                    print(f"          {row['host']:24s} dns {row['dns_ms']:7.0f}  "
                          f"tcp {row['tcp_ms']:7.0f}  tls {row['tls_ms']:7.0f} ms")
        print(f"\n  {summary['verdict']}")

    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")
    path = out / f"preflight-{stamp}.json"
    path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    if not args.json:
        print(f"wrote {path.relative_to(ROOT)}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
