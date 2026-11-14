"""Configure the Render service for the rade mirror (paper mode).

Reads every secret from .env and pushes it to Render over the API. Secret
values are never printed. Idempotent: re-running replaces the env var set.

    .venv/Scripts/python.exe scripts/configure_render_service.py
"""
from __future__ import annotations

import os
import secrets as pysecrets
import sys

import requests
from dotenv import load_dotenv

load_dotenv(".env")

SERVICE_ID = os.getenv("RADE_RENDER_SERVICE_ID", "srv-dbsaork9v7es73c6hheg")
API = "https://api.render.com/v1"


def env(name: str, default: str = "") -> str:
    return (os.getenv(name, default) or "").strip()


def telegram_string_session() -> str:
    """Export the local Telethon session as a StringSession (a credential)."""
    from telethon import TelegramClient
    from telethon.sessions import StringSession

    client = TelegramClient(
        "rade_session",
        int(env("TELEGRAM_API_ID", "0")),
        env("TELEGRAM_API_HASH"),
    )
    return StringSession.save(client.session)


def build_env_vars() -> list[dict]:
    session = telegram_string_session()
    api_token = "mirror_" + pysecrets.token_urlsafe(32)

    values: dict[str, str] = {
        # --- runtime ---------------------------------------------------------
        "PYTHON_VERSION": "3.12.6",
        "PYTHONPATH": "src",
        # --- mirror behaviour (non-secret) ----------------------------------
        "MIRROR_CHANNEL": env("TELEGRAM_CHANNEL", "@XAUUSD_FOREX4"),
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
        # --- sizing (paper: contract size unconfirmed) ---------------------
        "MIRROR_FIXED_LOTS": "0.03",
        "MIRROR_MAX_TRADE_RISK_USD": "25",
        "MIRROR_CONTRACT_UNITS_PER_LOT": "100",
        "MIRROR_CONTRACT_UNITS_CONFIRMED": "0",
        # --- quotes ---------------------------------------------------------
        "MIRROR_QUOTE_PROVIDER": "none",
        "MIRROR_QUOTE_MAX_AGE_S": "2",
        # --- execution (off) ------------------------------------------------
        "MIRROR_EXECUTE": "0",
        "MIRROR_MANAGE_EVERY_S": "5",
        "MIRROR_FILL_TIMEOUT_S": "300",
        # --- pre-trade risk controls ----------------------------------------
        "MIRROR_MAX_OPEN_POSITIONS": "1",
        "MIRROR_DAILY_LOSS_FRACTION": "0.01",
        "MIRROR_MAX_DRAWDOWN_FRACTION": "0.05",
        "MIRROR_MAX_SPREAD": "5",
        "MIRROR_MAX_SLIPPAGE": "5",
        # --- broker ---------------------------------------------------------
        "TRADELOCKER_ENV": "demo",
        "MIRROR_DESTINATION_SERVER": env("TRADELOCKER_SERVER", "PLEXY"),
        # --- secrets --------------------------------------------------------
        "MIRROR_API_TOKEN": api_token,
        "SUPABASE_URL": env("SUPABASE_URL"),
        "SUPABASE_SERVICE_ROLE_KEY": env("SUPABASE_SERVICE_ROLE_KEY"),
        "TELEGRAM_API_ID": env("TELEGRAM_API_ID"),
        "TELEGRAM_API_HASH": env("TELEGRAM_API_HASH"),
        "TELEGRAM_SESSION": session,
        "TELEGRAM_BOT_TOKEN": env("TELEGRAM_BOT_TOKEN"),
        "TELEGRAM_NOTIFY_CHAT_ID": "8077145837",
        "TRADELOCKER_EMAIL": env("TRADELOCKER_EMAIL"),
        "TRADELOCKER_PASSWORD": env("TRADELOCKER_PASSWORD"),
        "TRADELOCKER_SERVER": env("TRADELOCKER_SERVER"),
        "TRADELOCKER_ACCOUNT_ID": env("TRADELOCKER_ACCOUNT_ID"),
        "MIRROR_DESTINATION_ACCOUNT_ID": env("TRADELOCKER_ACCOUNT_ID"),
    }
    return [{"key": k, "value": v} for k, v in values.items() if v]


def main() -> int:
    token = env("RENDER_API_KEY")
    if not token:
        print("RENDER_API_TOKEN is not set")
        return 2
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json",
               "Content-Type": "application/json"}

    service_fields = {
        "runtime": "python",
        "buildCommand": "pip install -r requirements-mirror.txt",
        "startCommand": "python -m rade.observer.service",
        "healthCheckPath": "/healthz",
        "branch": "main",
        "autoDeploy": "yes",
        "plan": "free",
        "region": "frankfurt",
    }
    r = requests.patch(f"{API}/services/{SERVICE_ID}", headers=headers,
                       json=service_fields, timeout=60)
    print("service patch:", r.status_code)
    if r.status_code >= 400:
        print(r.text[:500])
        return 1

    env_vars = build_env_vars()
    r = requests.put(f"{API}/services/{SERVICE_ID}/env-vars", headers=headers,
                     json=env_vars, timeout=60)
    print("env vars put:", r.status_code, f"({len(env_vars)} keys)")
    if r.status_code >= 400:
        print(r.text[:500])
        return 1

    r = requests.get(f"{API}/services/{SERVICE_ID}", headers=headers, timeout=60)
    s = r.json()
    print("name:", s.get("name"), "| branch:", s.get("branch"),
          "| runtime:", s.get("runtime"), "| plan:", s.get("plan"))
    print("build:", s.get("buildCommand"))
    print("start:", s.get("startCommand"))
    print("health:", s.get("healthCheckPath"), "| autoDeploy:", s.get("autoDeploy"))
    print("url:", f"https://{s.get('defaultUrl')}" if s.get("defaultUrl") else "(pending)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
