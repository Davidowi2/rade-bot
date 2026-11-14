"""Push main using the credential Git Credential Manager already holds.

The manager has the credential (the account's own push stored it), but git's
``helper-selector`` wrapper does not resolve it in a non-interactive shell, so
``git push`` falls through to a TTY prompt that cannot be answered here. This
reads the credential from the manager and uses it once, without printing it.

    .venv/Scripts/python.exe scripts/push_with_manager.py
"""
from __future__ import annotations

import subprocess
import sys

REPO = "https://github.com/Davidowi2/rade-bot.git"


def credential() -> tuple[str, str]:
    proc = subprocess.run(
        ["git", "credential-manager", "get"],
        input="protocol=https\nhost=github.com\n\n",
        capture_output=True, text=True, timeout=60,
    )
    fields: dict[str, str] = {}
    for line in proc.stdout.splitlines():
        if "=" in line:
            key, _, value = line.partition("=")
            fields[key.strip()] = value.strip()
    user = fields.get("username", "")
    password = fields.get("password", "")
    if not password:
        print("no credential returned by git credential-manager")
        sys.exit(2)
    return user, password


def main() -> int:
    user, password = credential()
    url = REPO.replace("https://", f"https://{user}:{password}@")
    proc = subprocess.run(
        ["git", "-c", "credential.helper=", "push", url, "main"],
        capture_output=True, text=True, timeout=300,
    )
    # Never echo the URL: it carries the credential.
    out = (proc.stdout + proc.stderr).replace(password, "***").replace(user, user)
    print(out.strip() or f"(no output, rc={proc.returncode})")
    print("rc:", proc.returncode)
    return proc.returncode


if __name__ == "__main__":
    sys.exit(main())
