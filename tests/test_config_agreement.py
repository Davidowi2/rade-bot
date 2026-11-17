"""The deployment is described twice; keep the two descriptions equal.

``render.yaml`` is the blueprint Render applies on deploy. ``scripts/
update_render_env.py`` is the API path that does the same job by hand. They
drifted, and nothing compared them:

* the blueprint moved the per-trade ceiling to $30 on 2026-11-16 while the
  script still pushed $25, so "fix the config" and "deploy the config" produced
  different risk limits;
* ``MIRROR_EQUITY`` was declared twice in the blueprint, once as ``sync: false``
  and once as a literal, leaving the effective value up to whichever entry
  Render read last;
* the script set ``MIRROR_EXECUTE=1`` while the blueprint said ``0``, so running
  a configuration script could turn real trading on as a side effect.

None of those files was wrong on its own. These tests are the comparison.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RENDER_YAML = ROOT / "render.yaml"
ENV_EXAMPLE = ROOT / ".env.example"
CONFIG_SCRIPT = ROOT / "scripts" / "update_render_env.py"

KEY_LINE = re.compile(r"\s*-\s*key:\s*([A-Z_0-9]+)")
VALUE_LINE = re.compile(r'\s*value:\s*"?(.*?)"?\s*$')
SYNC_LINE = re.compile(r"\s*sync:\s*false")


def blueprint_keys() -> list[str]:
    """Every declared key, in order, duplicates included."""
    return [m.group(1) for m in
            (KEY_LINE.match(line) for line in
             RENDER_YAML.read_text(encoding="utf-8").splitlines()) if m]


def blueprint_values() -> dict[str, str | None]:
    """Each key with its literal value, or ``None`` when it is dashboard-set."""
    out: dict[str, str | None] = {}
    current: str | None = None
    for line in RENDER_YAML.read_text(encoding="utf-8").splitlines():
        m = KEY_LINE.match(line)
        if m:
            current = m.group(1)
            out[current] = None
            continue
        if current is None:
            continue
        v = VALUE_LINE.match(line)
        if v:
            out[current] = v.group(1)
        elif SYNC_LINE.match(line):
            out[current] = None
    return out


def env_example_keys() -> list[str]:
    keys = []
    for line in ENV_EXAMPLE.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        keys.append(stripped.split("=", 1)[0].strip())
    return keys


def script_desired() -> dict[str, str]:
    """DESIRED, read without importing the module (no requests, no network)."""
    tree = ast.parse(CONFIG_SCRIPT.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.AnnAssign):
            target = getattr(node.target, "id", None)
            if target == "DESIRED":
                return ast.literal_eval(node.value)
    raise AssertionError("DESIRED is not declared in update_render_env.py")


def test_the_blueprint_declares_no_key_twice():
    keys = blueprint_keys()
    duplicates = {k for k in keys if keys.count(k) > 1}
    assert not duplicates, (
        f"declared more than once in render.yaml: {sorted(duplicates)} - the "
        "effective value then depends on which entry Render reads last"
    )


def test_the_env_example_declares_no_key_twice():
    keys = env_example_keys()
    duplicates = {k for k in keys if keys.count(k) > 1}
    assert not duplicates, (
        f"declared more than once in .env.example: {sorted(duplicates)} - the "
        "last one wins when the file is loaded, so the first is a decoy"
    )


def test_the_blueprint_and_the_script_agree_on_every_shared_value():
    """Two descriptions of one deployment must not disagree."""
    blueprint = blueprint_values()
    desired = script_desired()
    conflicts = {
        key: (blueprint[key], value)
        for key, value in desired.items()
        if blueprint.get(key) is not None and blueprint[key] != value
    }
    assert not conflicts, (
        "render.yaml and scripts/update_render_env.py disagree (blueprint, "
        f"script): {conflicts}"
    )


def test_the_config_script_cannot_switch_trading_on():
    """Whether real orders are placed is a decision, not a setting."""
    assert "MIRROR_EXECUTE" not in script_desired(), (
        "update_render_env.py must not write MIRROR_EXECUTE: running a config "
        "script would then start placing real orders as a side effect"
    )


def test_the_blueprint_does_not_sync_the_trading_switch():
    """A blueprint ``value:`` is re-applied on deploy; that must not be this key.

    Declaring ``MIRROR_EXECUTE`` with a literal would switch trading off at the
    next push. Declaring it ``sync: false`` keeps the blueprint from changing
    whether orders are placed in either direction.
    """
    assert "MIRROR_EXECUTE" in blueprint_keys(), (
        "MIRROR_EXECUTE should still be declared so the key is visible"
    )
    assert blueprint_values()["MIRROR_EXECUTE"] is None, (
        "render.yaml must declare MIRROR_EXECUTE with `sync: false`; a synced "
        "value would change whether orders are placed on every deploy"
    )


def test_the_config_script_still_declares_the_settings_it_owns():
    """The guard above must not be satisfied by an empty table."""
    desired = script_desired()
    for key in ("MIRROR_FIXED_LOTS", "MIRROR_MAX_TRADE_RISK_USD",
                "MIRROR_CONTRACT_UNITS_PER_LOT", "MIRROR_QUOTE_PROVIDER"):
        assert key in desired, f"{key} disappeared from DESIRED"
