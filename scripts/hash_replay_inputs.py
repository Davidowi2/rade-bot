"""Hash every input and output of the replay, so a reviewer can prove parity.

The 1-minute bar database and the replay JSONs are gitignored (the database is
173 MB and carries a live Telegram session elsewhere in the tree), so without
this file a reviewer has no way to confirm they re-ran the replay on the same
inputs. This writes a committed manifest of:

* the bar database's SHA-256, row counts per timeframe and coverage window;
* the cohort files' SHA-256 (cross-checked against ``cohort/v1/MANIFEST.json``);
* the replay output JSONs' SHA-256 and the headline numbers inside them;
* the git commit the manifest was produced at.

Run:  python scripts/hash_replay_inputs.py
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "data" / "rade.db"
COHORT = ROOT / "cohort" / "v1"
OUT = ROOT / "docs" / "evidence" / "replay-inputs.json"

REPLAY_JSONS = [
    "data/replay_1m_train.json",
    "data/replay_1m_val.json",
    "data/replay_1m_test.json",
    "data/replay_1m_all.json",
    "data/replay_30m_train.json",
    "data/replay_30m_val.json",
    "data/replay_30m_test.json",
    "data/replay_30m_all.json",
    "data/gap_decomposition.json",
]


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def git(*args: str) -> str:
    try:
        return subprocess.run(
            ["git", *args], cwd=ROOT, capture_output=True, text=True, check=True
        ).stdout.strip()
    except Exception:
        return ""


def db_fingerprint(path: Path) -> dict:
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    out: dict = {"path": str(path.relative_to(ROOT)), "sha256": sha256(path),
                 "size_bytes": path.stat().st_size}
    try:
        rows = con.execute(
            "SELECT timeframe, COUNT(*), MIN(ts), MAX(ts) FROM bars GROUP BY timeframe"
        ).fetchall()
        out["bars"] = {
            tf: {"n": n, "min_ts": str(lo), "max_ts": str(hi)} for tf, n, lo, hi in rows
        }
        out["labeled_setups"] = con.execute("SELECT COUNT(*) FROM labeled_setups").fetchone()[0]
        out["raw_telegram_messages"] = con.execute(
            "SELECT COUNT(*) FROM raw_telegram_messages"
        ).fetchone()[0]
    finally:
        con.close()
    return out


def cohort_fingerprint() -> dict:
    manifest = json.loads((COHORT / "MANIFEST.json").read_text(encoding="utf-8"))
    out: dict = {}
    for name, meta in manifest["files"].items():
        p = COHORT / name
        actual = sha256(p)
        out[name] = {
            "sha256": actual,
            "matches_manifest": actual == meta["sha256"],
            "size_bytes": p.stat().st_size,
        }
    return out


def replay_fingerprint() -> dict:
    out: dict = {}
    for rel in REPLAY_JSONS:
        p = ROOT / rel
        if not p.exists():
            out[rel] = {"present": False}
            continue
        payload = json.loads(p.read_text(encoding="utf-8"))
        agg = payload.get("aggregate", {})
        out[rel] = {
            "present": True,
            "sha256": sha256(p),
            "size_bytes": p.stat().st_size,
            "resolution": payload.get("resolution"),
            "split": payload.get("split"),
            "assumptions": payload.get("assumptions", {}),
            "mean_net_R_by_arm": {
                k: v.get("mean_net_R") for k, v in agg.items() if isinstance(v, dict)
            },
        }
    return out


def main() -> int:
    if not DB.exists():
        print(f"missing {DB}", file=sys.stderr)
        return 1

    payload = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "git_commit": git("rev-parse", "HEAD"),
        "git_branch": git("rev-parse", "--abbrev-ref", "HEAD"),
        "git_dirty": bool(git("status", "--porcelain")),
        "why": (
            "data/rade.db and the replay JSONs are gitignored, so this file is the "
            "only committed proof of which inputs a reported result used. A reviewer "
            "can hash their own copy and compare."
        ),
        "commit_semantics": (
            "git_commit is the HEAD at the moment the hashes were computed. Because "
            "this file is itself committed afterwards, the manifest in commit N "
            "normally records commit N-1; that is expected. Re-run this script at "
            "any time to refresh it."
        ),
        "database": db_fingerprint(DB),
        "cohort": cohort_fingerprint(),
        "replay_outputs": replay_fingerprint(),
    }

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    print(f"wrote {OUT.relative_to(ROOT)}")
    print(f"  commit        : {payload['git_commit'][:12] or 'unknown'}")
    print(f"  db sha256     : {payload['database']['sha256']}")
    for tf, meta in payload["database"]["bars"].items():
        print(f"    {tf:>4} bars : {meta['n']:>9,}  {meta['min_ts'][:19]} -> {meta['max_ts'][:19]}")
    bad = [k for k, v in payload["cohort"].items() if not v["matches_manifest"]]
    print(f"  cohort files  : {len(payload['cohort'])} hashed, "
          f"{'all match the manifest' if not bad else f'MISMATCH: {bad}'}")
    present = sum(1 for v in payload["replay_outputs"].values() if v.get("present"))
    print(f"  replay outputs: {present}/{len(REPLAY_JSONS)} present and hashed")
    return 0 if not bad else 2


if __name__ == "__main__":
    raise SystemExit(main())
