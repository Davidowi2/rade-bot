"""Read-only reception accounting for the Telegram observer.

This module is deliberately **pure**: no network, no Telegram client, no
database. It decides what counts as a new message, what counts as a revision,
what counts as a duplicate, and how latency is summarised. The live probe in
``scripts/verify_telegram_reception.py`` is a thin wrapper around it, so the
rules can be tested without touching the network.

The distinction the observer phase depends on:

* **new** - a ``message_id`` not seen before;
* **revision** - a ``message_id`` seen before whose text changed (an edit);
* **duplicate** - a ``message_id`` seen before with byte-identical text, which
  is what a reconnect catch-up produces and must not be recorded twice.

Dedupe keys on ``(message_id, text_hash)``, not on ``message_id`` alone: keying
on the id alone would silently swallow a real edit, and keying on arrival order
would count a redelivery as an edit.
"""
from __future__ import annotations

import hashlib
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timezone

KIND_NEW = "new"
KIND_REVISION = "revision"
KIND_DUPLICATE = "duplicate"


def text_hash(text: str | None) -> str:
    """Stable hash of a message body. ``None`` and ``""`` hash identically."""
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Observation:
    """One arrival of a message body, as the client saw it."""

    channel: str
    message_id: int
    posted_at: datetime
    received_at: datetime
    text: str | None = None
    edited_at: datetime | None = None
    source: str = "live"
    """``live`` (push), ``catchup`` (reconnect fetch) or ``audit`` (end-of-run)."""

    @property
    def latency_ms(self) -> float | None:
        """``received_at - posted_at`` in milliseconds.

        This is the *observed* end-to-end delay: Telegram's own delivery delay
        plus network transit plus our dispatch. It is **not** purely our
        processing time, and it inherits any clock skew between the Telegram
        server and this host. Compare it against the 2s budget in the plan,
        which is stated in the same terms.
        """
        if self.posted_at is None or self.received_at is None:
            return None
        # both sides normalised: a naive datetime is UTC, never local
        posted = _as_utc(self.posted_at)
        received = _as_utc(self.received_at)
        return (received - posted).total_seconds() * 1000.0


@dataclass
class Event:
    """A recorded event, after de-duplication."""

    channel: str
    message_id: int
    revision: int
    kind: str
    posted_at: datetime
    received_at: datetime
    text_hash: str
    text_len: int
    latency_ms: float | None
    edited_at: datetime | None
    source: str

    def to_dict(self) -> dict:
        return {
            "channel": self.channel,
            "message_id": self.message_id,
            "revision": self.revision,
            "kind": self.kind,
            "posted_at": _iso(self.posted_at),
            "received_at": _iso(self.received_at),
            "text_hash": self.text_hash,
            "text_len": self.text_len,
            "latency_ms": None if self.latency_ms is None else round(self.latency_ms, 3),
            "edited_at": _iso(self.edited_at),
            "source": self.source,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Event":
        """Rebuild an event from a ``.jsonl`` line.

        Used to restore de-duplication state after a restart. Older evidence
        files predate some keys, so a missing one is not an error.
        """
        return cls(
            channel=d["channel"],
            message_id=int(d["message_id"]),
            revision=int(d.get("revision", 0)),
            kind=d.get("kind", KIND_NEW),
            posted_at=_parse(d["posted_at"]),
            received_at=_parse(d["received_at"]),
            text_hash=d["text_hash"],
            text_len=int(d.get("text_len", 0)),
            latency_ms=d.get("latency_ms"),
            edited_at=_parse(d.get("edited_at")),
            source=d.get("source", "unknown"),
        )


@dataclass
class Recorder:
    """Accumulates observations, assigning kinds and revisions.

    ``events`` holds only **new** and **revision** records. Duplicates are
    counted but not stored, so the evidence file stays a record of what the
    channel said rather than of how many times we happened to hear it.
    """

    events: list[Event] = field(default_factory=list)
    duplicates: int = 0
    restored_events: int = 0
    """How many prior events were loaded for de-duplication (not counted in ``events``)."""
    _seen: dict[tuple[str, int], str] = field(default_factory=dict)
    """``(channel, message_id) -> last text hash``."""

    _revision: dict[tuple[str, int], int] = field(default_factory=dict)

    def restore(self, prior: list[Event]) -> int:
        """Load prior events so a restart does not re-classify them as new.

        Only the de-duplication state is restored, not the counts: ``events``
        keeps meaning "what this run saw", and ``restored_events`` says how
        much history stood behind it. A restart that replays the same bodies
        must produce duplicates, which is exactly what the reconnect test is
        for; without this, every restart would silently double the record.
        """
        for e in prior:
            key = (e.channel, e.message_id)
            if e.revision >= self._revision.get(key, -1):
                self._seen[key] = e.text_hash
                self._revision[key] = e.revision
        self.restored_events = len(prior)
        return len(self._seen)

    def known_ids(self) -> set[int]:
        """Every message id this recorder has seen, this run or restored."""
        return {e.message_id for e in self.events} | {mid for (_, mid) in self._seen}

    def record(self, obs: Observation) -> Event | None:
        """Classify one observation. Returns the event, or ``None`` for a duplicate."""
        key = (obs.channel, obs.message_id)
        h = text_hash(obs.text)
        previous = self._seen.get(key)

        if previous is None:
            kind = KIND_NEW
            revision = 0
        elif previous == h:
            kind = KIND_DUPLICATE
            revision = self._revision[key]
        else:
            kind = KIND_REVISION
            revision = self._revision[key] + 1

        if kind == KIND_DUPLICATE:
            self.duplicates += 1
            return None

        self._seen[key] = h
        self._revision[key] = revision
        event = Event(
            channel=obs.channel,
            message_id=obs.message_id,
            revision=revision,
            kind=kind,
            posted_at=_as_utc(obs.posted_at),
            received_at=obs.received_at,
            text_hash=h,
            text_len=len(obs.text or ""),
            latency_ms=obs.latency_ms,
            edited_at=obs.edited_at,
            source=obs.source,
        )
        self.events.append(event)
        return event

    @property
    def new_count(self) -> int:
        return sum(1 for e in self.events if e.kind == KIND_NEW)

    @property
    def revision_count(self) -> int:
        return sum(1 for e in self.events if e.kind == KIND_REVISION)

    def ids(self) -> set[int]:
        return {e.message_id for e in self.events}


SKEW_TOLERANCE_MS = 2000.0
"""How far the host clock may drift before latency numbers are meaningless.

The plan's reception budget is p95 < 2s. If the host clock is off by more than
that, the measurement cannot distinguish a fast message from a slow one, and
reporting percentiles would be worse than reporting nothing. Measured skew is
corrected for, but a skew larger than the tolerance is reported as invalid
rather than silently repaired.
"""


def latency_summary(values: list[float], skew_ms: float | None = 0.0) -> dict:
    """Percentiles of the observed latency, with an explicit sample size.

    ``skew_ms`` is ``true_utc - host_utc``: add it to each raw latency to
    correct for a host clock that is not on UTC. ``None`` means the offset
    could not be measured at all, which is **not** the same as zero: an
    unmeasured offset leaves the numbers uncorrected and the percentiles
    unreportable.

    Refuses to invent a number from an empty sample, and refuses to report
    percentiles at all when the clock error is larger than the quantity being
    measured.
    """
    clean = sorted(v + (skew_ms or 0.0) for v in values if v is not None)
    n = len(clean)

    if skew_ms is None:
        return {
            "n": n,
            "p50_ms": None,
            "p95_ms": None,
            "max_ms": None,
            "mean_ms": None,
            "min_ms": None,
            "skew_ms": None,
            "verdict": (
                "UNKNOWN - the clock offset could not be measured, so latency "
                "is neither corrected nor reportable"
            ),
            "uncorrected_latency_ms": [round(v, 1) for v in clean[:20]],
        }

    if abs(skew_ms) > SKEW_TOLERANCE_MS:
        return {
            "n": n,
            "p50_ms": None,
            "p95_ms": None,
            "max_ms": None,
            "mean_ms": None,
            "min_ms": None,
            "skew_ms": round(skew_ms, 1),
            "verdict": (
                f"INVALID - host clock is off by {skew_ms / 1000.0:+.1f}s, "
                f"beyond the {SKEW_TOLERANCE_MS / 1000.0:.0f}s tolerance; "
                f"latency cannot be measured on this host"
            ),
            "corrected_latency_ms": [round(v, 1) for v in clean[:20]],
        }

    if n == 0:
        return {"n": 0, "verdict": "insufficient sample", "p50_ms": None,
                "p95_ms": None, "max_ms": None, "mean_ms": None, "min_ms": None,
                "skew_ms": round(skew_ms, 1)}

    return {
        "n": n,
        "p50_ms": round(_pct(clean, 50), 1),
        "p95_ms": round(_pct(clean, 95), 1),
        "max_ms": round(clean[-1], 1),
        "mean_ms": round(statistics.fmean(clean), 1),
        "min_ms": round(clean[0], 1),
        "skew_ms": round(skew_ms, 1),
        "verdict": "ok" if n >= 20 else f"weak sample (n={n})",
    }


def _pct(sorted_vals: list[float], p: float) -> float:
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    k = (len(sorted_vals) - 1) * p / 100.0
    lo = int(k)
    hi = min(lo + 1, len(sorted_vals) - 1)
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (k - lo)


def catchup_diff(live_ids: set[int], fetched_ids: set[int]) -> dict:
    """Compare what the live stream saw against what a catch-up fetch returns.

    ``missed`` is the number of messages a catch-up would have to recover —
    i.e. what a disconnect would have cost. ``extra`` is what the fetch knows
    about that the live stream never delivered. Both must be zero for the
    catch-up path to be trustworthy.
    """
    missed = fetched_ids - live_ids
    extra = live_ids - fetched_ids
    return {
        "n_live": len(live_ids),
        "n_fetched": len(fetched_ids),
        "missed": len(missed),
        "missed_ids": sorted(missed)[:50],
        "extra": len(extra),
        "extra_ids": sorted(extra)[:50],
        "complete": not missed and not extra,
    }


def _as_utc(dt: datetime) -> datetime:
    """Treat a naive datetime as UTC. Never call ``astimezone()`` on a naive one."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def load_events(path) -> list[Event]:
    """Read a ``.jsonl`` evidence file back into events.

    Blank lines and unparseable lines are skipped rather than raised: this runs
    at startup of a long soak, and one truncated final line (a crash mid-write)
    must not stop the observer from resuming.
    """
    import json
    from pathlib import Path

    events: list[Event] = []
    p = Path(path)
    if not p.exists():
        return events
    with p.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                events.append(Event.from_dict(json.loads(line)))
            except (ValueError, KeyError):
                continue
    return events


def _parse(value) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return _as_utc(value)
    return _as_utc(datetime.fromisoformat(value.replace("Z", "+00:00")))


def _iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    return _as_utc(dt).isoformat()
