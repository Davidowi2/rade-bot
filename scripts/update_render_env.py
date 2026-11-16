"""Set the Render service's mirror configuration, preserving its secrets.

``GET /env-vars`` then ``PUT`` the union, so a setting that is already correct
is left alone and a secret this script does not know about is never dropped.
Render's ``PUT`` replaces the whole set, which is why the read comes first.

Secret-shaped values are never printed: the report shows a key's name, whether
it changed, and its value only when the key is not credential-shaped.

    .venv/Scripts/python.exe scripts/update_render_env.py
    .venv/Scripts/python.exe scripts/update_render_env.py --dry-run
"""
from __future__ import annotations

import os
import sys

import requests
from dotenv import load_dotenv

load_dotenv(".env")

SERVICE = os.getenv("RADE_RENDER_SERVICE_ID", "srv-dbsaork9v7es73c6hheg")
API = "https://api.render.com/v1"

#: Keys whose value is a credential. Never printed, never invented.
SECRET_SHAPED = ("PASSWORD", "SECRET", "TOKEN", "KEY", "HASH", "SESSION",
                 "SERVICE_ROLE", "PRIVATE", "CREDENTIAL")

#: The mirror's own settings. Everything here is a fact about how the mirror
#: runs, not a credential. Values that must come from a human are absent and
#: are reported as missing rather than defaulted.
DESIRED: dict[str, str] = {
    # runtime
    "PYTHON_VERSION": "3.12.6",
    "PYTHONPATH": "src",
    # intake
    "MIRROR_CHANNEL": "@XAUUSD_FOREX4",
    "MIRROR_RUN_LISTENER": "1",
    "MIRROR_START_AT": "latest",
    "MIRROR_ALLOW_HISTORY_IMPORT": "0",
    "MIRROR_MAX_SIGNAL_AGE_S": "120",
    "MIRROR_CATCHUP_EVERY_S": "15",
    "MIRROR_RECONNECT_S": "900",
    "MIRROR_NOTIFY_RETRY_S": "300",
    "MIRROR_HEALTH_MAX_AGE_S": "90",
    "MIRROR_STATE": "/tmp/mirror_state.json",
    "MIRROR_HEARTBEAT": "/tmp/mirror_listener.heartbeat.json",
    "MIRROR_DUPLICATE_WINDOW_S": "900",
    # sizing. 100 units/lot is the broker's own lotSize for XAUUSD on the
    # destination account, read 2026-11-15. CONFIRMED=1 is what lets a signal
    # be sized at all; while it is 0 every market call is refused.
    #
    # 0.02 lots, not 0.03: at 100 units/lot the lot is $2.00 per point, so the
    # $25 ceiling admits a 12.5-point stop. The channel posts 10-point stops.
    # At 0.03 lots ($3.00/point) the same ceiling admitted only 8.33 points and
    # refused every signal the channel had ever posted.
    "MIRROR_FIXED_LOTS": "0.02",
    "MIRROR_MAX_TRADE_RISK_USD": "25",
    "MIRROR_CONTRACT_UNITS_PER_LOT": "100",
    "MIRROR_CONTRACT_UNITS_CONFIRMED": "1",
    "MIRROR_EQUITY": "4862.73",
    # quotes: a NOW call cannot be priced without a provider
    "MIRROR_QUOTE_PROVIDER": "tradelocker",
    "MIRROR_QUOTE_MAX_AGE_S": "2",
    # execution: ON. Orders go to the pinned demo-host destination (BLBRY
    # 2346225) only; the gateway refuses any other environment or account.
    "MIRROR_EXECUTE": "1",
    "MIRROR_MANAGE_EVERY_S": "5",
    "MIRROR_FILL_TIMEOUT_S": "300",
    # pre-trade risk controls
    "MIRROR_MAX_OPEN_POSITIONS": "1",
    "MIRROR_DAILY_LOSS_FRACTION": "0.01",
    "MIRROR_MAX_DRAWDOWN_FRACTION": "0.0335",
    "MIRROR_MAX_SPREAD": "1.0",
    "MIRROR_MAX_SLIPPAGE": "2.0",
}


def is_secret(name: str) -> bool:
    return any(part in name.upper() for part in SECRET_SHAPED)


def current_env(headers: dict) -> dict[str, str]:
    """Every env var, following Render's cursor pagination.

    The endpoint returns 20 rows per page. Reading only the first page and
    writing the union back would silently delete everything past it.
    """
    out: dict[str, str] = {}
    cursor: str | None = None
    for _ in range(20):
        params = {"cursor": cursor} if cursor else {}
        r = requests.get(f"{API}/services/{SERVICE}/env-vars", headers=headers,
                         params=params, timeout=30)
        r.raise_for_status()
        batch = r.json()
        if not isinstance(batch, list) or not batch:
            break
        for item in batch:
            env_var = item.get("envVar") or {}
            out[env_var.get("key", "")] = env_var.get("value") or ""
        cursor = batch[-1].get("cursor") or None
        if not cursor:
            break
    return out


def main() -> int:
    dry = "--dry-run" in sys.argv
    token = (os.getenv("RENDER_API_KEY") or "").strip()
    if not token:
        print("RENDER_API_KEY is not set")
        return 2
    headers = {"Authorization": f"Bearer {token}",
               "Accept": "application/json",
               "Content-Type": "application/json"}

    before = current_env(headers)
    after = dict(before)
    after.update(DESIRED)

    print(f"service {SERVICE}: {len(before)} vars present, "
          f"{len(DESIRED)} mirror settings declared\n")
    print(f"{'KEY':<40} {'STATE':<10} VALUE")
    print("-" * 96)
    changed = 0
    for key in sorted(after):
        old = before.get(key)
        new = after[key]
        if old is None:
            state = "ADDED"
        elif old != new:
            state = "CHANGED"
        else:
            state = "same"
        if state != "same":
            changed += 1
        shown = "***" if is_secret(key) else (new or "(empty)")
        if not is_secret(key) and len(shown) > 44:
            shown = shown[:41] + "..."
        print(f"{key:<40} {state:<10} {shown}")

    missing = sorted(k for k in DESIRED if not after.get(k))
    if missing:
        print(f"\ndeclared but empty: {', '.join(missing)}")

    if dry:
        print("\n--dry-run: nothing written")
        return 0

    payload = [{"key": k, "value": v} for k, v in after.items() if v != ""]
    r = requests.put(f"{API}/services/{SERVICE}/env-vars", headers=headers,
                     json=payload, timeout=60)
    print(f"\nPUT -> {r.status_code} ({len(payload)} vars)")
    if r.status_code >= 400:
        print(r.text[:500])
        return 1
    print(f"{changed} setting(s) changed; {len(payload) - changed} unchanged")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
