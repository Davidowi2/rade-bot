"""Observer step 1 — verify Telegram reception, catch-up, dedupe and edits.

**Read-only.** This script connects to Telegram with the existing session and
*listens*. It never sends a message, never touches the broker, and never places
an order. It is the read-only probe the redirect plan authorises (§5.1), minus
every row that needs authorisation.

What it measures, and what it cannot
------------------------------------
* **Live push latency** — ``received_at - posted_at`` per message. This needs
  the channel to actually post during the window. A quiet channel is reported
  as an inconclusive probe, never as a fast one.
* **Catch-up completeness** — after the window, every message newer than the
  baseline id is fetched again and diffed against what the live stream saw.
  Anything only the fetch knows about is what a disconnect would have cost.
* **Duplicate handling** — a redelivery of the same body must be counted as a
  duplicate and must not create a second event.
* **Edit detection** — ``MessageEdited`` events are recorded as revisions, and
  a re-fetch of the recent window at the end catches edits that arrived while
  the link was down.

Usage
-----
    python scripts/verify_telegram_reception.py --seconds 300
    python scripts/verify_telegram_reception.py --seconds 180 --reconnect-after 60
    python scripts/verify_telegram_reception.py --audit-only

Evidence is written to ``docs/evidence/telegram-reception-<utc>.json`` and a
line-per-event ``.jsonl`` beside it, flushed as it goes so a crash keeps the
record.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from rade.observer.reception import (  # noqa: E402
    SKEW_TOLERANCE_MS,
    Observation,
    Recorder,
    catchup_diff,
    latency_summary,
)

EVIDENCE = ROOT / "docs" / "evidence"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def measure_clock_skew_ms() -> tuple[float, dict]:
    """Estimate ``true_utc - host_utc`` from an HTTP ``Date`` header.

    Two independent sources disagreed with this host by hours, not seconds, so
    the probe measures the error before it measures anything else. The
    ``Date`` header has one-second granularity and the exchange has a round
    trip, so the estimate is good to roughly +/-1s - which is precise enough to
    detect a gross error (and to refuse to report latency), but **not** precise
    enough to certify a 2s budget on its own. A production host is expected to
    run NTP; this check exists to catch a host that does not.
    """
    import email.utils
    import time

    import requests

    detail: dict = {"source": "https://www.cloudflare.com (HTTP Date header)"}
    try:
        t0 = time.time()
        resp = requests.head("https://www.cloudflare.com", timeout=(10, 20))
        t1 = time.time()
        header = resp.headers.get("Date")
        if not header:
            return 0.0, {**detail, "error": "no Date header"}
        server = email.utils.parsedate_to_datetime(header).timestamp()
        rtt = t1 - t0
        # the Date header is truncated to the whole second, so the true server
        # time at send is somewhere in [server, server + 1)
        true_at_mid = server + 0.5 + rtt / 2.0
        host_at_mid = (t0 + t1) / 2.0
        skew_ms = (true_at_mid - host_at_mid) * 1000.0
        return skew_ms, {
            **detail,
            "date_header": header,
            "rtt_ms": round(rtt * 1000.0, 1),
            "granularity_s": 1.0,
            "accuracy_ms": "~±1000 (header truncation + RTT/2)",
        }
    except Exception as exc:  # pragma: no cover - network dependent
        return 0.0, {**detail, "error": f"{type(exc).__name__}: {exc}"}


class Probe:
    """Owns the client, the recorder and the evidence files."""

    def __init__(self, channel: str, seconds: float, reconnect_after: float | None,
                 out_dir: Path, state_from: list[Path] | None = None,
                 baseline_id: int | None = None):
        self.channel = channel
        self.seconds = seconds
        self.reconnect_after = reconnect_after
        self.recorder = Recorder()
        self.client = None
        self.entity = None
        self.state_from = state_from or []
        self.forced_baseline = baseline_id

        stamp = utcnow().strftime("%Y-%m-%dT%H-%M-%SZ")
        out_dir.mkdir(parents=True, exist_ok=True)
        self.jsonl_path = out_dir / f"telegram-reception-{stamp}.jsonl"
        self.json_path = out_dir / f"telegram-reception-{stamp}.json"
        self._jsonl = self.jsonl_path.open("w", encoding="utf-8")

        self.baseline_id: int | None = None
        self.reconnects: list[dict] = []
        self.errors: list[str] = []
        self.live_window_ids: set[int] = set()
        self._subscribed = False
        self.dispatch_ms: list[float] = []

    def _restore_state(self) -> int:
        """Load earlier evidence so a restart suppresses rather than duplicates."""
        from rade.observer.reception import load_events

        prior = []
        for path in self.state_from:
            got = load_events(path)
            if not got:
                self.errors.append(f"state-from: no events in {path}")
            prior.extend(got)
        if not prior:
            return 0
        keys = self.recorder.restore(prior)
        self._note(f"restored {len(prior)} prior event(s) over {keys} message key(s)")
        return len(prior)

    # -- evidence ---------------------------------------------------------
    def _emit(self, event) -> None:
        self._jsonl.write(json.dumps(event.to_dict(), ensure_ascii=False) + "\n")
        self._jsonl.flush()

    def _note(self, msg: str) -> None:
        print(f"  {msg}", flush=True)

    # -- handlers ---------------------------------------------------------
    async def _on_new(self, event) -> None:
        msg = event.message
        received = utcnow()
        obs = Observation(
            channel=self.channel,
            message_id=msg.id,
            posted_at=msg.date,
            received_at=received,
            text=msg.message,
            edited_at=getattr(msg, "edit_date", None),
            source="live",
        )
        rec = self.recorder.record(obs)
        if rec is not None:
            self.live_window_ids.add(msg.id)
            self._emit(rec)
            # measured on this host's clock alone, so it stays valid even when
            # the clock itself is wrong: this is our share of the budget
            self.dispatch_ms.append((utcnow() - received).total_seconds() * 1000.0)
            lat = rec.latency_ms
            self._note(
                f"NEW  id={msg.id} rev={rec.revision} "
                + (f"latency={lat:.0f}ms" if lat is not None else "latency=n/a")
            )

    async def _on_edit(self, event) -> None:
        msg = event.message
        received = utcnow()
        obs = Observation(
            channel=self.channel,
            message_id=msg.id,
            posted_at=msg.date,
            received_at=received,
            text=msg.message,
            edited_at=getattr(msg, "edit_date", None),
            source="live-edit",
        )
        rec = self.recorder.record(obs)
        if rec is not None:
            self._emit(rec)
            self._note(f"EDIT id={msg.id} rev={rec.revision}")

    # -- phases -----------------------------------------------------------
    async def _connect(self) -> None:
        """Connect, resolve the entity, subscribe, then pull pending updates.

        The order matters: ``get_entity`` needs a live connection, and the
        event handlers need the resolved entity. Subscribing is guarded so a
        reconnect does not register the handlers twice.
        """
        from telethon import events

        if not self.client.is_connected():
            await self.client.connect()
        if not await self.client.is_user_authorized():
            raise RuntimeError(
                "session is not authorised; run the interactive login once "
                "before using this probe"
            )
        if self.entity is None:
            self.entity = await self.client.get_entity(self.channel)

        if not self._subscribed:
            self.client.add_event_handler(self._on_new, events.NewMessage(chats=self.entity))
            self.client.add_event_handler(self._on_edit, events.MessageEdited(chats=self.entity))
            self._subscribed = True

        # pull anything that arrived while we were disconnected
        try:
            await self.client.catch_up()
        except Exception as exc:  # pragma: no cover - network dependent
            self.errors.append(f"catch_up: {type(exc).__name__}: {exc}")

    async def _latest_id(self) -> int | None:
        async for m in self.client.iter_messages(self.entity, limit=1):
            return m.id
        return None

    async def _fetch_since(self, since_id: int) -> set[int]:
        """Fetch every id newer than ``since_id`` and record it as catch-up."""
        ids: set[int] = set()
        async for m in self.client.iter_messages(
            self.entity, min_id=since_id, reverse=True, limit=None
        ):
            ids.add(m.id)
            obs = Observation(
                channel=self.channel,
                message_id=m.id,
                posted_at=m.date,
                received_at=utcnow(),
                text=m.message,
                edited_at=getattr(m, "edit_date", None),
                source="catchup",
            )
            rec = self.recorder.record(obs)
            if rec is not None:
                self._emit(rec)
        return ids

    async def run(self) -> dict:
        from rade.ingestion.telegram.client import make_client

        started = utcnow()
        skew_ms, skew_detail = measure_clock_skew_ms()
        self._note(f"host clock skew: {skew_ms / 1000.0:+.1f}s vs true UTC")
        restored = self._restore_state()
        self.client = make_client()

        await self._connect()
        if self.forced_baseline is not None:
            self.baseline_id = self.forced_baseline
            self._note(f"forced baseline_id={self.baseline_id} (replay of a known window)")
        else:
            self.baseline_id = await self._latest_id()
        self._note(f"connected; channel={self.channel} baseline_id={self.baseline_id}")
        self._note(f"listening for {self.seconds:.0f}s "
                   f"(read-only: no send, no broker, no orders)")

        deadline = started + timedelta(seconds=self.seconds)
        reconnect_at = (
            started + timedelta(seconds=self.reconnect_after)
            if self.reconnect_after is not None else None
        )
        did_reconnect = False

        while utcnow() < deadline:
            await asyncio.sleep(0.25)
            if reconnect_at and not did_reconnect and utcnow() >= reconnect_at:
                did_reconnect = True
                await self._reconnect()

        live_ids = set(self.recorder.ids())

        # --- catch-up audit: would a reconnect have recovered everything? ---
        fetched_ids: set[int] = set()
        if self.baseline_id is not None:
            try:
                fetched_ids = await self._fetch_since(self.baseline_id)
            except Exception as exc:  # pragma: no cover - network dependent
                self.errors.append(f"catchup fetch: {type(exc).__name__}: {exc}")

        # --- edit audit: re-fetch the recent window and diff bodies --------
        missed_edits = []
        if fetched_ids:
            recent = sorted(fetched_ids)[-50:]
            async for m in self.client.iter_messages(self.entity, ids=recent):
                if m is None:
                    continue
                known = self.recorder._seen.get((self.channel, m.id))
                from rade.observer.reception import text_hash
                if known is not None and known != text_hash(m.message):
                    missed_edits.append(m.id)

        try:
            await self.client.disconnect()
        except Exception:
            pass
        self._jsonl.close()

        floor = self.baseline_id or 0
        # what we already had before the fetch: this run's push stream plus any
        # state restored from earlier evidence. A restart must not turn a
        # message we already hold into a "missed" one.
        known_before = {i for i in self.recorder.known_ids() if i > floor}
        live_only = {i for i in live_ids if i > floor}
        diff = catchup_diff(known_before, fetched_ids)
        lat = latency_summary(
            [e.latency_ms for e in self.recorder.events
             if e.source in ("live", "live-edit")],
            skew_ms=skew_ms,
        )
        # our own handling cost, on one clock: unaffected by the skew above
        dispatch = latency_summary(self.dispatch_ms, skew_ms=0.0)

        skew_end_ms, skew_end_detail = measure_clock_skew_ms()
        drift_ms = skew_end_ms - skew_ms
        certifiable = abs(skew_ms) <= SKEW_TOLERANCE_MS and abs(drift_ms) <= SKEW_TOLERANCE_MS

        summary = {
            "probe": "telegram reception (read-only)",
            "read_only": True,
            "started_at": started.isoformat(),
            "finished_at": utcnow().isoformat(),
            "duration_s": round((utcnow() - started).total_seconds(), 1),
            "channel": self.channel,
            "clock_skew": {
                "skew_ms": round(skew_ms, 1),
                **skew_detail,
                "skew_end_ms": round(skew_end_ms, 1),
                "skew_end_date_header": skew_end_detail.get("date_header"),
                "drift_ms": round(drift_ms, 1),
                "certifiable": certifiable,
            },
            "baseline_message_id": self.baseline_id,
            "baseline_forced": self.forced_baseline is not None,
            "restored_events": restored,
            "counts": {
                "new": self.recorder.new_count,
                "revisions": self.recorder.revision_count,
                "duplicates": self.recorder.duplicates,
                "events_recorded": len(self.recorder.events),
                "live_in_window": len(live_only),
            },
            "latency": lat,
            "dispatch": dispatch,
            "catchup": diff,
            "reconnects": self.reconnects,
            "missed_edits_on_refetch": missed_edits,
            "errors": self.errors,
            "verdict": _verdict(lat, diff, self.recorder, missed_edits, self.errors,
                                certifiable=certifiable),
        }
        self.json_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
        return summary

    async def _reconnect(self) -> None:
        """Deliberately drop the link, then let catch-up recover the gap."""
        before = set(self.recorder.ids())
        self._note("disconnecting (reconnect test)...")
        t0 = utcnow()
        try:
            await self.client.disconnect()
        except Exception as exc:
            self.errors.append(f"disconnect: {type(exc).__name__}: {exc}")
        await asyncio.sleep(3.0)
        try:
            await self._connect()
        except Exception as exc:
            self.errors.append(f"reconnect: {type(exc).__name__}: {exc}")
            return
        gap = (utcnow() - t0).total_seconds()
        after = set(self.recorder.ids())
        self.reconnects.append({
            "at": t0.isoformat(),
            "down_s": round(gap, 2),
            "ids_before": len(before),
            "ids_after": len(after),
            "recovered": sorted(after - before),
        })
        self._note(f"reconnected after {gap:.1f}s; recovered "
                   f"{len(after - before)} message(s) via catch_up()")


def _reception_verdict(lat: dict) -> str:
    """Never print a percentile the clock cannot support."""
    if str(lat.get("verdict", "")).startswith("INVALID"):
        return lat["verdict"]
    if lat["n"] == 0:
        return "inconclusive - channel was quiet, no latency sample"
    p95 = lat["p95_ms"]
    return (f"p95={p95}ms over n={lat['n']} "
            f"({'PASS' if p95 < 2000 else 'FAIL'} vs 2s budget)")


def _verdict(lat: dict, diff: dict, rec: Recorder, missed_edits: list, errors: list,
             certifiable: bool = True) -> dict:
    """Turn the measurements into explicit pass/fail/inconclusive lines.

    An empty window must never report PASS. "Zero missed out of zero" is not
    evidence that catch-up works, it is evidence that nothing happened - the
    same distinction the replay report had to make between a measured zero and
    an unmeasured one.

    ``certifiable`` is false when the host clock is too wrong to time a
    message. That does not stop the probe from testing catch-up, dedupe or
    edits - those are set comparisons - but it does stop it from claiming the
    2s reception budget was met.
    """
    empty = diff["n_live"] == 0 and diff["n_fetched"] == 0
    return {
        "reception": _reception_verdict(lat),
        "clock": (
            "PASS - host clock within tolerance of true UTC"
            if certifiable else
            "BLOCKED - host clock is not on UTC (Windows Time service is stopped "
            "and cannot be started without elevation); end-to-end latency cannot "
            "be certified on this host, whatever the sample shows"
        ),
        "catchup": (
            "inconclusive - no messages in the window, nothing to reconcile"
            if empty else
            "PASS - catch-up recovered exactly what the stream saw"
            if diff["complete"] else
            f"FAIL - missed {diff['missed']}, extra {diff['extra']}"
        ),
        "duplicates": (
            "inconclusive - no messages in the window"
            if empty and rec.duplicates == 0 else
            "PASS - no duplicates" if rec.duplicates == 0 else
            f"{rec.duplicates} duplicate(s) seen and suppressed"
        ),
        "edits": (
            "inconclusive - no messages in the window to refetch"
            if empty else
            "PASS - no edit missed on refetch" if not missed_edits else
            f"FAIL - {len(missed_edits)} edit(s) not seen live: {missed_edits}"
        ),
        "errors": "none" if not errors else f"{len(errors)} error(s)",
    }


def audit_only(channel: str) -> dict:
    """Static audit of what the existing code does and does not guarantee."""
    from rade.storage.models import RawTelegramMessage

    cols = set(RawTelegramMessage.__table__.columns.keys())
    return {
        "probe": "static audit (no network)",
        "channel": channel,
        "listener_exists": False,
        "detail": (
            "src/rade/ingestion/telegram/ has client.py, parser.py and backfill.py; "
            "scripts/shadow_collect.py polls with iter_messages(min_id=...) rather "
            "than subscribing to events. There is no push listener, so live latency "
            "has never been measured."
        ),
        "raw_message_columns": sorted(cols),
        "has_received_at": "received_at" in cols,
        "has_revision": "revision" in cols,
        "has_edited_at": "edited_at" in cols,
        "gaps": [
            "no push listener: latency cannot be measured from stored data",
            "no received_at column: ingested_at is set on first insert and is "
            "poll-time, not receipt-time",
            "no revision column: backfill upserts in place, so an edit destroys "
            "the previous body",
            "edit detection is bounded to the last 50 ids (shadow_collect.py), "
            "so older edits are invisible - the frozen spec says so explicitly",
        ],
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--channel", default=None)
    ap.add_argument("--seconds", type=float, default=300.0)
    ap.add_argument("--reconnect-after", type=float, default=None)
    ap.add_argument("--state-from", action="append", default=[],
                    metavar="JSONL",
                    help="prior evidence file(s) whose messages must count as "
                         "already seen (repeatable). A restart then reports "
                         "redeliveries as duplicates instead of new.")
    ap.add_argument("--baseline-id", type=int, default=None,
                    help="force the catch-up floor to a known message id and "
                         "replay that window through the fetch path. Use with "
                         "--state-from to test dedupe against real history "
                         "without waiting for the channel to speak.")
    ap.add_argument("--audit-only", action="store_true")
    ap.add_argument("--out-dir", default=str(EVIDENCE))
    args = ap.parse_args()

    from dotenv import load_dotenv
    import os
    load_dotenv(ROOT / ".env")
    channel = args.channel or os.getenv("TELEGRAM_CHANNEL", "@XAUUSD_FOREX4")

    if args.audit_only:
        result = audit_only(channel)
        EVIDENCE.mkdir(parents=True, exist_ok=True)
        stamp = utcnow().strftime("%Y-%m-%dT%H-%M-%SZ")
        path = EVIDENCE / f"telegram-audit-{stamp}.json"
        path.write_text(json.dumps(result, indent=2), encoding="utf-8")
        print(json.dumps(result, indent=2))
        print(f"\nwrote {path.relative_to(ROOT)}")
        return 0

    probe = Probe(channel, args.seconds, args.reconnect_after, Path(args.out_dir),
                  state_from=[Path(p) for p in args.state_from],
                  baseline_id=args.baseline_id)
    print("=" * 70)
    print("TELEGRAM RECEPTION PROBE — READ-ONLY (no send, no broker, no orders)")
    print("=" * 70)
    summary = asyncio.run(probe.run())

    print("\n--- verdict ---")
    for k, v in summary["verdict"].items():
        print(f"  {k:12s}: {v}")
    print(f"\nlatency   : {json.dumps(summary['latency'])}")
    print(f"dispatch  : {json.dumps(summary['dispatch'])}")
    print(f"catch-up  : {json.dumps(summary['catchup'])}")
    print(f"counts    : {json.dumps(summary['counts'])}")
    print(f"\nwrote {probe.json_path.relative_to(ROOT)}")
    print(f"wrote {probe.jsonl_path.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
