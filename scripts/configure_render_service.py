"""Configure the Render service's build/run settings, then its mirror env.

The service fields (build command, start command, health check) are set here.
The environment variables are handled by ``scripts/update_render_env``,
which reads the remote set first and writes the union back.

That split exists because the broker credentials live in Render, not in the
local ``.env``: a script that rebuilt the whole env set from a local file would
overwrite the deployed destination with whatever account happened to be in the
file. The local ``.env`` may hold a different, older account.

    .venv/Scripts/python.exe scripts/configure_render_service.py
"""
from __future__ import annotations

import os
import sys

import requests
from dotenv import load_dotenv

load_dotenv(".env")

SERVICE_ID = os.getenv("RADE_RENDER_SERVICE_ID", "srv-dbsaork9v7es73c6hheg")
API = "https://api.render.com/v1"


def main() -> int:
    token = (os.getenv("RENDER_API_KEY") or "").strip()
    if not token:
        print("RENDER_API_KEY is not set")
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

    # Env vars: delegate, so the remote credentials are read back and kept.
    import importlib.util
    from pathlib import Path

    path = Path(__file__).with_name("update_render_env.py")
    spec = importlib.util.spec_from_file_location("update_render_env", path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module.main()


if __name__ == "__main__":
    sys.exit(main())
