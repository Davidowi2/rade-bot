"""Fail-closed Telegram-to-paper-intent pipeline.

This module consumes Telegram message envelopes, validates the parsed trade,
sizes a *paper intent* from explicit broker/account parameters, and records it
idempotently. It never calls a broker and never sends a notification.

Design rules, each of which exists because the opposite behaviour would be
indistinguishable from a working system:

* **Nothing is filled.** A market call produces ``would_fill_market``; a limit
  call produces ``pending_limit``. ``filled`` is always false. There is no
  code path in this module that can produce a fill.
* **Missing inputs block.** No equity, no contract specification, no quote, a
  stale quote, or a quote that has already crossed the stop all produce
  ``blocked`` with no intent. Nothing is estimated from a default.
* **Ambiguity goes to a human.** An edited call, a multi-entry message, a
  target on the wrong side, duplicate targets, or a stop/target line carrying
  more than one number produces ``manual_review`` with no intent.
* **Idempotency is the ledger's job.** ``process_once`` asks the ledger to
  claim an event; a redelivery is a duplicate and a same-revision message with
  a different body is a conflict, not an update.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

# ---------------------------------------------------------------------------
# dispositions
# ---------------------------------------------------------------------------

STATUS_PAPER_INTENT = "paper_intent"
STATUS_IGNORED = "ignored"
STATUS_MANUAL_REVIEW = "manual_review"
STATUS_BLOCKED = "blocked"
STATUS_SKIPPED_MIN_LOT = "skipped_minimum_lot"
STATUS_DUPLICATE = "duplicate"
STATUS_CONFLICT = "conflict"

CLAIM_INSERTED = "inserted"
CLAIM_DUPLICATE = "duplicate"
CLAIM_CONFLICT = "conflict"

#: The agreed per-trade risk cap. Not a default that may be raised by config.
MAX_RISK_FRACTION = 0.005

# ---------------------------------------------------------------------------
# parsing
# ---------------------------------------------------------------------------

# The channel's entry grammar is explicit: symbol, side, mode, price, on one
# line. Anchoring at line start is what stops a recap ("we closed XAUUSD BUY
# from 3915, +120 pips") from being read as a new call.
ENTRY_LINE_RE = re.compile(
    r"^[ \t]*(?P<symbol>XAUUSD|XAU/USD|GOLD)[ \t]+"
    r"(?P<side>BUY|SELL)[ \t]+"
    r"(?P<mode>NOW|MARKET|LIMIT)[ \t]*[:@]?[ \t]*"
    r"(?P<price>\d{3,5}(?:\.\d+)?)\b",
    re.IGNORECASE | re.MULTILINE,
)
STOP_LINE_RE = re.compile(
    r"^[ \t]*(?:SL|STOP(?:[ \t]+LOSS)?)[ \t]*[:=]?[ \t]*(?P<body>[^\n]+)$",
    re.IGNORECASE | re.MULTILINE,
)
TP_LINE_RE = re.compile(
    r"^[ \t]*(?:TP[ \t]*\d*|TARGET[ \t]*\d*|TAKE[ \t]*PROFIT)[ \t]*[:=]?[ \t]*(?P<body>[^\n]+)$",
    re.IGNORECASE | re.MULTILINE,
)
NUMBER_RE = re.compile(r"\d{3,5}(?:\.\d+)?")

# A recap or result post is not a call. These markers are only consulted when
# the message has no entry line, so they can never suppress a real signal.
_RECAP_MARKERS = re.compile(
    r"\b(closed|booked|pips?|profit|hit\s+tp|tp\s*\d*\s*hit|result|recap|running)\b",
    re.IGNORECASE,
)


def count_entry_lines(text: str | None) -> int:
    return len(list(ENTRY_LINE_RE.finditer(text or "")))


def parse_message_signal(text: str | None) -> dict[str, Any] | None:
    """Parse the channel's explicit XAUUSD entry grammar.

    Returns ``None`` unless the message contains exactly one entry line.
    Non-fatal ambiguities (a stop line with two numbers, a target ladder that
    is not nearest-first) are returned in ``issues`` so the caller can route
    the message to a human instead of guessing.
    """
    body = text or ""
    entries = list(ENTRY_LINE_RE.finditer(body))
    if len(entries) != 1:
        return None

    entry = entries[0]
    issues: list[str] = []

    stop_values: list[float] = []
    for match in STOP_LINE_RE.finditer(body):
        numbers = NUMBER_RE.findall(match.group("body"))
        if len(numbers) != 1:
            issues.append(f"stop line carries {len(numbers)} numbers")
            continue
        stop_values.append(float(numbers[0]))

    target_values: list[float] = []
    for match in TP_LINE_RE.finditer(body):
        numbers = NUMBER_RE.findall(match.group("body"))
        if len(numbers) != 1:
            issues.append(f"target line carries {len(numbers)} numbers")
            continue
        target_values.append(float(numbers[0]))

    if len(stop_values) > 1:
        issues.append(f"{len(stop_values)} stop lines")
    if len(set(target_values)) != len(target_values):
        issues.append("duplicate target prices")

    symbol = entry.group("symbol").upper()
    side = entry.group("side").lower()
    mode = entry.group("mode").upper()
    price = float(entry.group("price"))

    # The ladder is defined nearest-first. The legacy replay engine never
    # normalised it and produced a single -36.78 R leg on one setup, so the
    # normalisation happens here, once, and is recorded.
    ordered = sorted(target_values, reverse=(side == "sell"))
    ladder_normalised = ordered != target_values

    return {
        "instrument": "XAUUSD" if symbol in {"GOLD", "XAU/USD"} else symbol,
        "direction": side,
        "entry_type": "market" if mode in {"NOW", "MARKET"} else "limit",
        "entry_price": price,
        "stop_loss": stop_values[0] if len(stop_values) == 1 else None,
        "take_profits": ordered,
        "ladder_normalised": ladder_normalised,
        "issues": issues,
    }


# ---------------------------------------------------------------------------
# envelopes and inputs
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MessageEnvelope:
    channel: str
    message_id: int
    revision: int
    posted_at: datetime
    received_at: datetime
    text: str
    source: str = "telegram"
    edited_at: datetime | None = None


@dataclass(frozen=True)
class InstrumentSpec:
    """Contract terms that must come from the broker, never from a guess.

    ``units_confirmed`` is the guard against a *plausible but unverified* number.
    A contract size read once from a broker response is not evidence: until a
    human has checked it against the broker's own specification, any size
    computed from it is fiction. The default is therefore ``False`` - an
    unconfirmed spec produces no size at all.
    """

    symbol: str
    contract_units_per_lot: float
    lot_step: float
    min_lot: float
    max_lot: float
    units_confirmed: bool = False


@dataclass(frozen=True)
class MarketQuote:
    bid: float
    ask: float
    quoted_at: datetime


@dataclass(frozen=True)
class RiskConfig:
    equity: float
    risk_fraction: float
    quote_max_age_seconds: float


@dataclass(frozen=True)
class PipelineResult:
    event_key: str
    status: str
    reason: str
    signal: dict[str, Any] | None
    order_intent: dict[str, Any] | None
    notification_preview: str
    duplicate: bool = False

    @property
    def requires_review(self) -> bool:
        return self.status in {STATUS_MANUAL_REVIEW, STATUS_CONFLICT}

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["requires_review"] = self.requires_review
        return payload


def text_hash(text: str | None) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def event_key(message: MessageEnvelope) -> str:
    raw = f"{message.channel}\0{message.message_id}\0{message.revision}".encode()
    return hashlib.sha256(raw).hexdigest()


def envelope_from_telegram(message: Any, *, channel: str, revision: int = 0,
                           received_at: datetime | None = None,
                           source: str = "telegram_live") -> MessageEnvelope:
    """Adapt a Telethon message/update into the pipeline's stable envelope."""
    posted_at = getattr(message, "date", None)
    if posted_at is None:
        raise ValueError("Telegram message has no posted timestamp")
    text = getattr(message, "message", None)
    if text is None:
        text = getattr(message, "text", None)
    return MessageEnvelope(
        channel=channel,
        message_id=int(message.id),
        revision=revision,
        posted_at=posted_at,
        received_at=received_at or datetime.now(timezone.utc),
        text=text or "",
        source=source,
        edited_at=getattr(message, "edit_date", None),
    )


def as_utc(dt: datetime) -> datetime:
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def _result(key: str, status: str, reason: str,
            signal: dict[str, Any] | None = None,
            intent: dict[str, Any] | None = None,
            note: str | None = None) -> PipelineResult:
    return PipelineResult(key, status, reason, signal, intent,
                          note or f"No order: {reason}")


# ---------------------------------------------------------------------------
# evaluation
# ---------------------------------------------------------------------------


def evaluate_message(
    message: MessageEnvelope,
    *,
    instrument: InstrumentSpec,
    risk: RiskConfig,
    quote: MarketQuote | None = None,
) -> PipelineResult:
    """Parse and validate one message, returning a paper intent or a no-trade."""
    key = event_key(message)
    text = message.text or ""

    entries = count_entry_lines(text)
    if entries == 0:
        if _RECAP_MARKERS.search(text):
            return _result(key, STATUS_IGNORED, "recap/result post, not a trade call")
        return _result(key, STATUS_IGNORED, "message is not a recognised signal")
    if entries > 1:
        return _result(key, STATUS_MANUAL_REVIEW,
                       f"message contains {entries} entry lines; manual review required")

    parsed = parse_message_signal(text)
    if parsed is None:  # pragma: no cover - count_entry_lines guarantees this
        return _result(key, STATUS_IGNORED, "message is not a recognised signal")

    if parsed["issues"]:
        return _result(key, STATUS_MANUAL_REVIEW,
                       "; ".join(parsed["issues"]), signal=parsed)

    if parsed.get("instrument") != instrument.symbol:
        return _result(key, STATUS_MANUAL_REVIEW,
                       f"instrument {parsed.get('instrument')} is not {instrument.symbol}",
                       signal=parsed)

    if parsed.get("entry_price") is None or parsed.get("stop_loss") is None or not parsed["take_profits"]:
        return _result(key, STATUS_MANUAL_REVIEW,
                       "entry, stop, and at least one target are required", signal=parsed)

    entry = float(parsed["entry_price"])
    stop = float(parsed["stop_loss"])
    targets: list[float] = list(parsed["take_profits"])
    side = parsed["direction"]
    is_buy = side == "buy"

    if any(not math.isfinite(x) or x <= 0 for x in [entry, stop, *targets]):
        return _result(key, STATUS_MANUAL_REVIEW,
                       "prices must be finite positive numbers", signal=parsed)
    if (is_buy and stop >= entry) or (not is_buy and stop <= entry):
        return _result(key, STATUS_MANUAL_REVIEW,
                       "stop is on the wrong side of entry", signal=parsed)
    if any((is_buy and t <= entry) or (not is_buy and t >= entry) for t in targets):
        return _result(key, STATUS_MANUAL_REVIEW,
                       "one or more targets are on the wrong side of entry", signal=parsed)

    # An edit is never applied automatically: it may be a stop move, a partial
    # close instruction, or a correction, and the exit policy is unresolved.
    if message.revision > 0:
        changed = parsed["entry_price"] != entry or parsed["stop_loss"] != stop
        return _result(
            key, STATUS_MANUAL_REVIEW,
            "edited signal; never amend an existing intent automatically"
            + (" (entry or stop changed)" if changed else ""),
            signal=parsed,
            note=f"Edited signal {message.message_id} rev {message.revision}: review before any action.",
        )

    if risk.equity <= 0 or not math.isfinite(risk.equity):
        return _result(key, STATUS_BLOCKED, "account equity is missing or invalid", signal=parsed)
    if not 0 < risk.risk_fraction <= MAX_RISK_FRACTION:
        return _result(
            key, STATUS_BLOCKED,
            f"risk configuration is invalid (per-trade risk must be in (0, {MAX_RISK_FRACTION:.1%}])",
            signal=parsed,
        )
    if not instrument.units_confirmed:
        # The contract size was never checked against the broker's own
        # specification. Any lot count derived from it would look right and be
        # wrong, so nothing is sized.
        return _result(
            key, STATUS_BLOCKED,
            "contract units per lot are not confirmed against the broker "
            "specification; no size is computed",
            signal=parsed,
        )
    if (instrument.contract_units_per_lot <= 0 or instrument.lot_step <= 0
            or instrument.min_lot <= 0 or instrument.max_lot < instrument.min_lot):
        return _result(key, STATUS_BLOCKED,
                       "broker contract/lot specification is incomplete", signal=parsed)

    quote_block: dict[str, Any] | None = None
    if parsed["entry_type"] == "market":
        if quote is None:
            return _result(key, STATUS_BLOCKED,
                           "market signal needs a fresh broker quote", signal=parsed)
        if not (math.isfinite(quote.bid) and math.isfinite(quote.ask)
                and quote.bid > 0 and quote.ask > 0 and quote.ask >= quote.bid):
            return _result(key, STATUS_BLOCKED, "broker quote is invalid", signal=parsed)

        # A quote is stale when it is older than the budget *before* the
        # message arrived. The listener fetches the quote after receipt, so a
        # negative age is normal and must not be read as a stale price.
        max_age = float(risk.quote_max_age_seconds)
        age = (as_utc(message.received_at) - as_utc(quote.quoted_at)).total_seconds()
        if age < -max_age:
            return _result(key, STATUS_BLOCKED,
                           "quote timestamp is in the future beyond tolerance", signal=parsed)
        if age > max_age:
            return _result(key, STATUS_BLOCKED, "broker quote is stale", signal=parsed)

        effective_entry = quote.ask if is_buy else quote.bid
        if (is_buy and stop >= effective_entry) or (not is_buy and stop <= effective_entry):
            return _result(key, STATUS_BLOCKED,
                           "market has already crossed the published stop", signal=parsed)
        if any((is_buy and t <= effective_entry) or (not is_buy and t >= effective_entry) for t in targets):
            return _result(key, STATUS_MANUAL_REVIEW,
                           "market has already crossed a target; review required", signal=parsed)
        order_type = "market"
        order_price = None
        quote_block = {
            "bid": quote.bid,
            "ask": quote.ask,
            "quoted_at": as_utc(quote.quoted_at).isoformat(),
            "age_ms": round(age * 1000.0, 1),
        }
    else:
        effective_entry = entry
        order_type = "limit"
        order_price = entry

    risk_budget = risk.equity * risk.risk_fraction
    risk_per_lot = abs(effective_entry - stop) * instrument.contract_units_per_lot
    if risk_per_lot <= 0:
        return _result(key, STATUS_BLOCKED, "stop distance is zero", signal=parsed)
    raw_lots = risk_budget / risk_per_lot
    lots = math.floor((raw_lots + 1e-12) / instrument.lot_step) * instrument.lot_step
    lots = round(lots, 8)
    if lots < instrument.min_lot:
        return _result(
            key, STATUS_SKIPPED_MIN_LOT,
            f"minimum lot {instrument.min_lot:g} would exceed the risk budget; skip",
            signal=parsed,
        )
    if lots > instrument.max_lot:
        lots = instrument.max_lot
    estimated_stop_risk = lots * risk_per_lot

    signal = {
        "instrument": instrument.symbol,
        "side": side,
        "entry_type": parsed["entry_type"],
        "published_entry": entry,
        "stop_loss": stop,
        "take_profits": targets,
        "ladder_normalised": parsed["ladder_normalised"],
        "posted_at": as_utc(message.posted_at).isoformat(),
        "message_id": message.message_id,
        "revision": message.revision,
    }
    intent = {
        "mode": "paper_only",
        "broker_write": False,
        # A limit call is not a fill, and a market call is not a fill either:
        # nothing is executed in this phase.
        "filled": False,
        "order_state": "pending_limit" if order_type == "limit" else "would_fill_market",
        "symbol": instrument.symbol,
        "side": side,
        "type": order_type,
        "price": order_price,
        "effective_entry_for_sizing": effective_entry,
        "size_lots": lots,
        "stop_loss": stop,
        "take_profits": targets,
        "risk_fraction": risk.risk_fraction,
        "risk_budget": round(risk_budget, 2),
        "estimated_stop_risk_before_slippage_and_fees": round(estimated_stop_risk, 2),
        "quote": quote_block,
        "idempotency_key": key,
    }
    note = (
        f"PAPER ONLY — {side.upper()} {instrument.symbol} {order_type.upper()} "
        f"{lots:g} lot; SL {stop:g}; TPs {', '.join(f'{x:g}' for x in targets)}. "
        f"Risk budget ${risk_budget:.2f}; no broker order sent."
    )
    return PipelineResult(key, STATUS_PAPER_INTENT, "validated; simulation only",
                          signal, intent, note)


# ---------------------------------------------------------------------------
# ledger
# ---------------------------------------------------------------------------


class Ledger(Protocol):
    """Storage contract. Idempotency is enforced by the store, not by callers."""

    def claim(self, message: MessageEnvelope, result: PipelineResult) -> str:
        """Insert the event once and report ``inserted``/``duplicate``/``conflict``."""
        ...

    def revision_for(self, channel: str, message_id: int) -> int:
        """Highest revision already stored for a message (0 when unknown)."""
        ...

    def counts(self) -> dict[str, int]:
        ...


_SCHEMA = """
CREATE TABLE IF NOT EXISTS mirror_events (
    event_key     TEXT PRIMARY KEY,
    channel       TEXT NOT NULL,
    message_id    INTEGER NOT NULL,
    revision      INTEGER NOT NULL,
    posted_at     TEXT NOT NULL,
    received_at   TEXT NOT NULL,
    text_sha256   TEXT NOT NULL,
    raw_text      TEXT NOT NULL,
    status        TEXT NOT NULL,
    reason        TEXT NOT NULL,
    signal_json   TEXT,
    intent_json   TEXT,
    source        TEXT NOT NULL,
    UNIQUE (channel, message_id, revision)
);
CREATE INDEX IF NOT EXISTS ix_mirror_events_status ON mirror_events(status);
CREATE INDEX IF NOT EXISTS ix_mirror_events_received ON mirror_events(received_at);
CREATE TABLE IF NOT EXISTS notification_log (
    event_key TEXT NOT NULL,
    kind      TEXT NOT NULL,
    status    TEXT NOT NULL DEFAULT 'pending',
    ok        INTEGER NOT NULL DEFAULT 0,
    attempts  INTEGER NOT NULL DEFAULT 0,
    detail    TEXT,
    sent_at   TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (event_key, kind)
);
CREATE TABLE IF NOT EXISTS mirror_cursor (
    channel         TEXT PRIMARY KEY,
    last_message_id INTEGER NOT NULL,
    start_source    TEXT NOT NULL DEFAULT '',
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


class SQLiteIntentLedger:
    """Local ledger for development. Supabase is used in deployment.

    The unique constraint on ``(channel, message_id, revision)`` is what makes
    reprocessing safe: it is enforced by the database, not by a Python set that
    a restart would empty.
    """

    def __init__(self, path: str | Path):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as con:
            con.executescript(_SCHEMA)
            self._migrate(con)

    @contextmanager
    def _connect(self):
        """Open, commit and **close** a connection.

        ``with sqlite3.connect(...)`` commits but does not close, so every call
        used to leak a handle. On Windows that keeps the database file locked
        and blocks deletion; in a long-running listener it leaks until GC.
        """
        con = sqlite3.connect(self.path)
        try:
            with con:
                yield con
        finally:
            con.close()

    @staticmethod
    def _migrate(con: sqlite3.Connection) -> None:
        """Add columns introduced after the first local ledger was written."""
        have = {row[1] for row in con.execute("PRAGMA table_info(mirror_events)")}
        additions = {
            "posted_at": "TEXT NOT NULL DEFAULT ''",
            "text_sha256": "TEXT NOT NULL DEFAULT ''",
            "reason": "TEXT NOT NULL DEFAULT ''",
            "signal_json": "TEXT",
            "intent_json": "TEXT",
            "source": "TEXT NOT NULL DEFAULT 'unknown'",
        }
        for column, decl in additions.items():
            if column not in have:
                con.execute(f"ALTER TABLE mirror_events ADD COLUMN {column} {decl}")

        # The notification table used to hold a single "ok" flag with the slot
        # claimed *before* the send. That made a failed send indistinguishable
        # from a delivered one and left nothing to retry. The status column
        # carries the real outcome.
        have_notif = {row[1] for row in con.execute("PRAGMA table_info(notification_log)")}
        for column, decl in (
            ("status", "TEXT NOT NULL DEFAULT 'pending'"),
            ("attempts", "INTEGER NOT NULL DEFAULT 0"),
        ):
            if column not in have_notif:
                con.execute(f"ALTER TABLE notification_log ADD COLUMN {column} {decl}")

    def claim(self, message: MessageEnvelope, result: PipelineResult) -> str:
        digest = text_hash(message.text)
        with self._connect() as con:
            cur = con.execute(
                """INSERT OR IGNORE INTO mirror_events
                   (event_key, channel, message_id, revision, posted_at, received_at,
                    text_sha256, raw_text, status, reason, signal_json, intent_json, source)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (result.event_key, message.channel, message.message_id, message.revision,
                 as_utc(message.posted_at).isoformat(), as_utc(message.received_at).isoformat(),
                 digest, message.text, result.status, result.reason,
                 json.dumps(result.signal, sort_keys=True) if result.signal else None,
                 json.dumps(result.order_intent, sort_keys=True) if result.order_intent else None,
                 message.source),
            )
            if cur.rowcount == 1:
                return CLAIM_INSERTED
            row = con.execute(
                "SELECT text_sha256 FROM mirror_events WHERE event_key=?",
                (result.event_key,),
            ).fetchone()
        if row and row[0] and row[0] != digest:
            return CLAIM_CONFLICT
        return CLAIM_DUPLICATE

    def revision_for(self, channel: str, message_id: int) -> int:
        with self._connect() as con:
            row = con.execute(
                "SELECT MAX(revision) FROM mirror_events WHERE channel=? AND message_id=?",
                (channel, message_id),
            ).fetchone()
        return int(row[0]) if row and row[0] is not None else 0

    def last_message_id(self) -> int:
        """Highest message id ever stored. The restart cursor, from the store."""
        with self._connect() as con:
            row = con.execute("SELECT MAX(message_id) FROM mirror_events").fetchone()
        return int(row[0]) if row and row[0] is not None else 0

    def counts(self) -> dict[str, int]:
        with self._connect() as con:
            return {status: n for status, n in con.execute(
                "SELECT status, COUNT(*) FROM mirror_events GROUP BY status")}

    # -- notifications ----------------------------------------------------
    #
    # The notification slot has a real state machine, because a boolean "ok"
    # that is written before the send cannot distinguish "delivered" from
    # "failed" and leaves nothing to retry:
    #
    #   begin_notification   -> status 'pending', attempts += 1
    #   complete_notification-> status 'sent' or 'failed', with the detail
    #
    # A 'pending' row means the process died between the two calls. It is
    # retried: at-least-once delivery beats silently losing a notification,
    # and a duplicate phone message is the cheaper failure.

    def notification_status(self, event_key: str, kind: str) -> str | None:
        """'sent', 'failed', 'pending', or None when the slot is unused."""
        with self._connect() as con:
            row = con.execute(
                "SELECT status FROM notification_log WHERE event_key=? AND kind=?",
                (event_key, kind),
            ).fetchone()
        return str(row[0]) if row else None

    def begin_notification(self, event_key: str, kind: str) -> int:
        """Claim the slot for an attempt and count it. Returns the attempt number."""
        with self._connect() as con:
            row = con.execute(
                "SELECT attempts FROM notification_log WHERE event_key=? AND kind=?",
                (event_key, kind),
            ).fetchone()
            attempts = (int(row[0]) if row and row[0] is not None else 0) + 1
            con.execute(
                "INSERT INTO notification_log "
                "(event_key, kind, status, ok, attempts, detail, sent_at) "
                "VALUES (?, ?, 'pending', 0, ?, NULL, datetime('now')) "
                "ON CONFLICT(event_key, kind) DO UPDATE SET "
                "status='pending', ok=0, attempts=excluded.attempts, "
                "sent_at=datetime('now')",
                (event_key, kind, attempts),
            )
        return attempts

    def complete_notification(self, event_key: str, kind: str, ok: bool,
                              detail: str | None = None) -> bool:
        """Record the real outcome of the attempt."""
        with self._connect() as con:
            cur = con.execute(
                "UPDATE notification_log SET status=?, ok=?, detail=?, "
                "sent_at=datetime('now') WHERE event_key=? AND kind=?",
                ("sent" if ok else "failed", 1 if ok else 0, detail, event_key, kind),
            )
            return cur.rowcount > 0

    def record_notification(self, event_key: str, kind: str, ok: bool,
                            detail: str | None = None) -> bool:
        """Claim the slot and record the outcome in one step.

        False means the slot was already delivered, so nothing was sent.
        """
        if self.notification_status(event_key, kind) == "sent":
            return False
        self.begin_notification(event_key, kind)
        self.complete_notification(event_key, kind, ok, detail)
        return True

    def failed_notifications(self, *, limit: int = 20) -> list[dict]:
        """Notifications whose last attempt failed, oldest first."""
        with self._connect() as con:
            rows = con.execute(
                "SELECT n.event_key, n.kind, n.detail, n.attempts, "
                "e.status AS event_status, e.reason AS event_reason, "
                "e.signal_json, e.intent_json, e.channel, e.message_id "
                "FROM notification_log n "
                "LEFT JOIN mirror_events e ON e.event_key = n.event_key "
                "WHERE n.status='failed' ORDER BY n.sent_at ASC LIMIT ?",
                (int(limit),),
            ).fetchall()
        keys = ("event_key", "kind", "detail", "attempts", "event_status",
                "event_reason", "signal_json", "intent_json", "channel", "message_id")
        out = []
        for row in rows:
            item = dict(zip(keys, row))
            for field_name in ("signal_json", "intent_json"):
                if isinstance(item[field_name], str) and item[field_name]:
                    try:
                        item[field_name] = json.loads(item[field_name])
                    except ValueError:
                        item[field_name] = None
            out.append(item)
        return out

    # -- cursor -----------------------------------------------------------

    def load_cursor(self, channel: str) -> int:
        """The persisted 'start from here' floor for this channel."""
        with self._connect() as con:
            row = con.execute(
                "SELECT last_message_id FROM mirror_cursor WHERE channel=?", (channel,)
            ).fetchone()
        return int(row[0]) if row and row[0] is not None else 0

    def save_cursor(self, channel: str, message_id: int, *, source: str = "") -> bool:
        """Persist the floor, so an ephemeral disk does not lose it."""
        with self._connect() as con:
            con.execute(
                "INSERT INTO mirror_cursor (channel, last_message_id, start_source, updated_at) "
                "VALUES (?, ?, ?, datetime('now')) "
                "ON CONFLICT(channel) DO UPDATE SET "
                "last_message_id=excluded.last_message_id, "
                "start_source=excluded.start_source, updated_at=datetime('now')",
                (channel, int(message_id), source or ""),
            )
        return True

    def recent(self, *, limit: int = 50, statuses: list[str] | None = None) -> list[dict]:
        """Newest events first, shaped like the Supabase adapter's rows."""
        sql = ("SELECT event_key, channel, message_id, revision, posted_at, received_at, "
               "status, reason, signal_json, intent_json, source FROM mirror_events")
        params: list[Any] = []
        if statuses:
            sql += f" WHERE status IN ({','.join('?' * len(statuses))})"
            params.extend(statuses)
        sql += " ORDER BY received_at DESC LIMIT ?"
        params.append(int(limit))
        with self._connect() as con:
            rows = con.execute(sql, params).fetchall()
        keys = ("event_key", "channel", "message_id", "revision", "posted_at", "received_at",
                "status", "reason", "signal_json", "intent_json", "source")
        out = []
        for row in rows:
            item = dict(zip(keys, row))
            for field_name in ("signal_json", "intent_json"):
                if item[field_name]:
                    item[field_name] = json.loads(item[field_name])
            out.append(item)
        return out


def process_once(
    message: MessageEnvelope,
    *,
    ledger: Ledger,
    instrument: InstrumentSpec,
    risk: RiskConfig,
    quote: MarketQuote | None = None,
) -> PipelineResult:
    """Evaluate a message and record it exactly once."""
    result = evaluate_message(message, instrument=instrument, risk=risk, quote=quote)
    outcome = ledger.claim(message, result)

    if outcome == CLAIM_INSERTED:
        return result

    if outcome == CLAIM_CONFLICT:
        return PipelineResult(
            result.event_key, STATUS_CONFLICT,
            "same message id and revision arrived with different text; review required",
            None, None,
            f"Conflicting body for message {message.message_id} rev {message.revision}: "
            "stored copy kept, new copy not applied.",
            duplicate=False,
        )

    return PipelineResult(
        result.event_key, STATUS_DUPLICATE, "event already processed", None, None,
        f"Duplicate message {message.message_id} revision {message.revision}; no second intent.",
        duplicate=True,
    )
