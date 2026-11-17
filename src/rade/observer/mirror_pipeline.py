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
from typing import Any, Callable, Protocol

from rade.execution.sizing import LotSpec, fit_lot_to_risk

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
#: A NOW signal that was posted too long before it reached the pipeline.
#: Distinct from "blocked" because the cause is the clock, not the market, and
#: because "how many signals arrive too late" is a question worth being able to
#: count. It is never executed: the engine only acts on ``paper_intent``.
STATUS_STALE = "stale"

#: How old a NOW signal may be when it reaches the pipeline, in seconds.
#:
#: In normal operation this limit is inert: the listener polls every 15 s, so a
#: signal reaches the pipeline 15-20 s after it was posted. The limit only
#: decides anything after the service was *not running* - a spin-down on a free
#: plan, a dropped connection - and there the alternative to a generous limit is
#: losing the signal entirely. 900 s is what the deployment has always used; the
#: code default now matches it so a missing environment variable cannot silently
#: tighten the window back to two minutes.
#:
#: This is not the guard that makes a late signal safe. The slippage guard
#: (``max_slippage``, measured from the *published* entry) and the per-trade
#: risk ceiling are what refuse a call the market has already left behind. The
#: age limit only rejects an instruction that is old in absolute terms.
DEFAULT_SIGNAL_MAX_AGE_S = 900.0

#: How long the *read* of a quote may take before the price is unusable.
#:
#: This is a latency budget, not a freshness budget, and the two were confused:
#: a quote is stamped when it is fetched, which is *after* the message arrived,
#: so the read duration is what a negative age actually measures. Refusing on
#: that number is what produced "quote timestamp is in the future beyond
#: tolerance" for an ordinary 2.5 s broker round-trip. A read this slow means the
#: connection is broken, not that the price is wrong.
HUNG_QUOTE_READ_S = 15.0

#: How far a posted timestamp may sit ahead of receipt before it stops being a
#: clock disagreement and becomes an unusable timestamp.
SIGNAL_FUTURE_TOLERANCE_S = 120.0

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
    """How a signal is sized.

    ``equity`` is the number used for sizing. When ``equity_provider`` is set
    it is called for every signal and its answer wins, so a long-running
    process sizes against the account as it is now rather than as it was at
    startup. The provider is expected to raise when the equity cannot be read;
    a stale number is never substituted for a fresh one.

    ``quote_max_age_seconds`` and ``signal_max_age_seconds`` are different
    questions and both are asked. The first says "is the price I am about to
    trade still current"; the second says "is the instruction I am about to
    follow still current". A fresh quote proves only the first.

    ``fixed_lots`` selects the confirmed sizing mode. When it is set the size
    is exactly that number or the signal is refused; ``risk_fraction`` is then
    ignored, and ``max_trade_risk_usd`` becomes mandatory, because a fixed lot
    is not a risk limit on its own. Leaving ``fixed_lots`` unset keeps the
    earlier risk-fraction behaviour.
    """

    equity: float
    risk_fraction: float
    quote_max_age_seconds: float
    equity_provider: Callable[[], float] | None = None
    #: How old a NOW signal may be when it reaches the pipeline. ``0`` disables
    #: the limit, but never the timestamp sanity checks.
    signal_max_age_seconds: float = DEFAULT_SIGNAL_MAX_AGE_S
    #: The confirmed size, or ``None`` for risk-fraction sizing.
    fixed_lots: float | None = None
    #: The per-trade USD ceiling a fixed lot must stay inside. Required
    #: whenever ``fixed_lots`` is set; a missing ceiling refuses every signal.
    max_trade_risk_usd: float | None = None

    @property
    def sizing_mode(self) -> str:
        return "risk_fraction" if self.fixed_lots is None else "fixed_lots"

    def current_equity(self) -> float:
        if self.equity_provider is None:
            return float(self.equity)
        return float(self.equity_provider())


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


def format_age(seconds: float) -> str:
    """A duration a human can read at a glance."""
    seconds = abs(float(seconds))
    if seconds < 90:
        return f"{seconds:.0f}s"
    minutes = seconds / 60.0
    if minutes < 90:
        return f"{minutes:.0f}m"
    hours = minutes / 60.0
    if hours < 48:
        return f"{hours:.1f}h"
    return f"{hours / 24.0:.1f}d"


def signal_age_seconds(message: MessageEnvelope, *,
                       now: datetime | None = None) -> float | None:
    """How long the signal sat between being posted and being acted on.

    The reference is the moment the message entered the pipeline
    (``received_at``), not the wall clock at the moment of evaluation. That
    matters for one reason: it makes the answer deterministic, so the same
    message always gets the same verdict and a test does not depend on when it
    runs.

    It is also the right question. On a poll, a catch-up after a reconnect, or
    a restart, the listener rebuilds the envelope at fetch time, so
    ``received_at`` is when the signal was *recovered* and the difference from
    ``posted_at`` is exactly the delay that makes a signal unusable. A caller
    that replays a stored envelope - where ``received_at`` is the original
    receipt - must pass ``now`` to judge it against a later moment.

    Returns ``None`` when the message carries no posted timestamp.
    """
    if message.posted_at is None:
        return None
    reference = as_utc(now) if now is not None else as_utc(message.received_at)
    return (reference - as_utc(message.posted_at)).total_seconds()


def stale_reason(message: MessageEnvelope, risk: RiskConfig, *,
                 now: datetime | None = None) -> str | None:
    """Why this signal is too old to act on, or ``None`` when it is current.

    Separate from the quote check on purpose. The quote check answers "is the
    price current"; this answers "is the instruction current". A fresh quote
    says nothing about a signal that was recovered twenty minutes after it was
    posted, and that is exactly the case a restart or a reconnect produces.

    One limit applies to every source. There used to be a lenient branch for
    catch-up, on the theory that a signal recovered after a spin-down deserved
    more slack; it was a no-op at the deployed value and it made the rule depend
    on which internal string the listener happened to pass. The limit itself is
    the allowance for a service that was not running.
    """
    if message.posted_at is None:
        return ("the signal carries no posted timestamp, so its age cannot be "
                "established; a signal whose age is unknown is not traded")

    age = signal_age_seconds(message, now=now)
    if age is None:  # pragma: no cover - guarded by the check above
        return "the signal's age could not be established"

    if age < -SIGNAL_FUTURE_TOLERANCE_S:
        return (f"the signal is stamped {format_age(-age)} in the future, which "
                "is not a clock disagreement; its timestamp cannot be trusted")

    limit = float(risk.signal_max_age_seconds)
    if limit > 0 and age > limit:
        return (f"the signal is stale: it was posted {format_age(age)} before it "
                f"reached the pipeline, beyond the {format_age(limit)} limit "
                "(MIRROR_MAX_SIGNAL_AGE_S); a late signal is not the trade that "
                "was published")
    return None


def _opt_float(value: Any) -> float | None:
    """A float, or ``None`` when the value is absent or not a number."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _opt_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _trade_row_to_dict(row: dict) -> dict:
    """Normalise a stored trade row into the shape the engine reads.

    The targets and the plan are stored as JSON text and come back as
    objects, so the engine does not have to know which store it is talking
    to.
    """
    item = dict(row)
    targets = item.get("targets_json")
    if isinstance(targets, str):
        try:
            item["targets"] = json.loads(targets)
        except ValueError:
            item["targets"] = []
    elif targets is not None:
        item["targets"] = targets
    else:
        item["targets"] = []
    plan = item.get("plan_json")
    if isinstance(plan, str):
        try:
            item["plan"] = json.loads(plan)
        except ValueError:
            item["plan"] = {}
    elif plan is not None:
        item["plan"] = plan
    else:
        item["plan"] = {}
    item["management_stopped"] = bool(item.get("management_stopped"))
    return item


def _result(key: str, status: str, reason: str,
            signal: dict[str, Any] | None = None,
            intent: dict[str, Any] | None = None,
            note: str | None = None) -> PipelineResult:
    return PipelineResult(key, status, reason, signal, intent,
                          note or f"No order: {reason}")


#: The fields an edit can change that would alter a position already open. Text
#: is deliberately absent: the channel adds a tick mark seconds after posting,
#: and that changes the wording without changing the trade.
TRADE_DEFINING_FIELDS = ("direction", "entry_price", "stop_loss", "take_profits")


def _summarise_revisions(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Reduce every stored revision of one message to what the rule needs.

    The question is "is there a position open for this message", and it is not
    answered by the highest revision alone. A tick mark files revision 1 as
    ``duplicate``, which opened nothing - so a third revision that looked only
    at revision 1 would conclude nothing was ever opened and buy again, on top
    of a live position. One revision can open a position and the next can
    decline to; the answer has to be "any of them", not "the last one".

    ``rows`` must be ordered newest first.
    """
    if not rows:
        return None
    opening = [r for r in rows if r.get("status") == STATUS_PAPER_INTENT]
    # Compare against what is actually live: the newest intent. Falls back to
    # the newest revision when nothing opened, purely so the caller can log it.
    reference = opening[0] if opening else rows[0]
    return {"status": rows[0].get("status"),
            "signal": reference.get("signal") or {},
            "opened": bool(opening),
            "revisions": len(rows)}


def earlier_revision_opened_a_position(previous: dict[str, Any] | None) -> bool | None:
    """Did any earlier revision put money at risk? ``None`` means unknown.

    Only an intent can have opened a position. Every other disposition -
    blocked, stale, ignored, duplicate, review - means the pipeline declined to
    act, so there is nothing an edit could damage and no reason to refuse it.

    ``None`` is a third answer on purpose. "I could not read the earlier
    revision" is not "there is nothing to protect", and collapsing the two is
    how a fail-open guard gets written by accident. A summary that does not say
    is treated the same as one that could not be read.
    """
    if not previous:
        return None
    opened = previous.get("opened")
    if opened is None:
        return None
    return bool(opened)


def _same_price(a: Any, b: Any) -> bool:
    try:
        return math.isclose(float(a), float(b), rel_tol=1e-9, abs_tol=1e-9)
    except (TypeError, ValueError):
        return a == b


def _same_prices(a: Any, b: Any) -> bool:
    try:
        left, right = list(a or []), list(b or [])
    except TypeError:
        return False
    if len(left) != len(right):
        return False
    return all(_same_price(x, y) for x, y in zip(left, right))


def _trade_view(raw: dict[str, Any]) -> dict[str, Any]:
    """The trade-defining fields, from either shape this codebase produces.

    Two dicts describe the same call: the parser emits ``direction`` and
    ``entry_price``, the recorded signal emits ``side`` and
    ``published_entry``. Comparing them without normalising makes every
    cosmetic edit look like a total rewrite, which is the bug this whole rule
    exists to avoid.
    """
    entry = raw.get("entry_price")
    if entry is None:
        entry = raw.get("published_entry")
    return {
        "direction": raw.get("direction") or raw.get("side"),
        "entry_price": entry,
        "stop_loss": raw.get("stop_loss"),
        "take_profits": raw.get("take_profits"),
    }


def changed_trade_fields(previous: dict[str, Any],
                         current: dict[str, Any]) -> list[str]:
    """Which trade-defining fields differ between two revisions.

    Compared on values, not text. An earlier signal that is missing a field
    reports that field as changed, which sends the edit to a human rather than
    quietly calling it cosmetic - the failure here must be a needless review,
    never a missed amendment.
    """
    if not previous:
        return list(TRADE_DEFINING_FIELDS)
    before, after = _trade_view(previous), _trade_view(current)
    changed: list[str] = []
    if before["direction"] != after["direction"]:
        changed.append("direction")
    for field in ("entry_price", "stop_loss"):
        if not _same_price(before[field], after[field]):
            changed.append(field)
    if not _same_prices(before["take_profits"], after["take_profits"]):
        changed.append("take_profits")
    return changed


# ---------------------------------------------------------------------------
# evaluation
# ---------------------------------------------------------------------------


def evaluate_message(
    message: MessageEnvelope,
    *,
    instrument: InstrumentSpec,
    risk: RiskConfig,
    quote: MarketQuote | None = None,
    quote_error: str | None = None,
    now: datetime | None = None,
    previous: dict[str, Any] | None = None,
) -> PipelineResult:
    """Parse and validate one message, returning a paper intent or a no-trade.

    ``now`` judges the signal's age against a later moment than its receipt;
    leave it unset to use the receipt time, which is what a live listener
    wants.

    ``previous`` is the status and parsed signal of the highest revision already
    stored for this message, or ``None``. It is only consulted when
    ``message.revision > 0``, and leaving it unset is safe: an unknown earlier
    revision goes to a human rather than being assumed harmless.
    """
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

    # An edit is expected on this channel. It is run by a bot that amends its own
    # posts within a couple of seconds, and this copier polls every fifteen: the
    # text it can read is always the amended text. The amended text is the
    # trader's final published instruction - the original was live for ~2s, too
    # short for anyone to act on - so a message first seen already edited is
    # traded as it stands.
    #
    # A *later* revision is a different question, and the rule is about money,
    # not about editing. An edit can only do damage if the earlier revision
    # opened a position; if it did not, the edit is the best version of a call
    # nobody acted on. If it did, the edit is compared against what is open:
    # identical trade -> the tick mark the channel adds, filed as cosmetic;
    # different trade -> a human, because moving a live stop is not a decision
    # this pipeline makes on its own.
    #
    # The provenance is recorded on the signal either way, so the evidence trail
    # says the original wording was never seen rather than silently looking like
    # an unedited call.
    if message.edited_at is not None:
        parsed["first_seen_edited"] = True
        parsed["first_seen_edited_at"] = as_utc(message.edited_at).isoformat()

    if message.revision > 0:
        # A later revision of a message this pipeline has already stored. What
        # decides the outcome is not that it was edited, but whether the earlier
        # revision opened a position - the only thing an edit can damage.
        held = earlier_revision_opened_a_position(previous)

        if held is None:
            # The earlier revision could not be read. Whether money is at risk
            # is unknown, so a human looks. This is the behaviour that shipped
            # before, kept for the one case where guessing is the wrong move.
            note = (f"Edited signal {message.message_id} rev {message.revision}: "
                    "review before any action.")
            return _result(key, STATUS_MANUAL_REVIEW,
                           "edited signal; the earlier revision could not be read, "
                           "so whether it opened a position is unknown",
                           signal=parsed, note=note)

        if not held:
            # Nothing was opened for the earlier revision: it was blocked,
            # stale, ignored, or never a signal at all. There is no position to
            # protect, so this edit is simply the best version of a call the
            # pipeline has not acted on. It is judged like any other signal,
            # age included - the age is measured from receipt, so a long gap
            # still refuses it.
            parsed["supersedes_revision"] = message.revision
        else:
            previous_signal = (previous or {}).get("signal") or {}
            changed = changed_trade_fields(previous_signal, parsed)
            if not changed:
                # The wording moved, the trade did not: this is the tick mark
                # the channel adds seconds after posting. Nothing a position can
                # be affected by has changed, so it is not a decision for a
                # human and must not be filed as one. Duplicate rather than
                # merely ignored, so it cannot reach the executor by any path.
                note = (f"Edited signal {message.message_id} rev {message.revision}: "
                        "wording only; entry, stop and targets are unchanged, so "
                        "no second order is placed.")
                return PipelineResult(
                    key, STATUS_DUPLICATE,
                    "cosmetic edit; entry, stop and targets unchanged",
                    parsed, None, note, duplicate=True)
            reason = ("edited signal; an amendment to an intent already recorded "
                      "is never applied automatically "
                      f"({', '.join(changed)} changed)")
            note = (f"Edited signal {message.message_id} rev {message.revision}: "
                    "review before any action.")
            return _result(key, STATUS_MANUAL_REVIEW, reason, signal=parsed,
                           note=note)

    # Age is checked before anything is sized or priced. A signal that is too
    # old is not made acceptable by a fresh quote, a wide stop, or a quiet
    # market: the instruction itself has expired. This runs for market signals
    # only - a LIMIT call is never copied, and labelling it stale would hide
    # the reason it was not copied.
    if parsed["entry_type"] == "market":
        stale = stale_reason(message, risk, now=now)
        if stale is not None:
            return _result(key, STATUS_STALE, stale, signal=parsed)

    if not instrument.units_confirmed:
        # The contract size was never checked against the broker's own
        # specification. Any lot count derived from it would look right and be
        # wrong, so nothing is sized. This is checked first, because it is the
        # reason a message is blocked even when the equity is also missing.
        return _result(
            key, STATUS_BLOCKED,
            "contract units per lot are not confirmed against the broker "
            "specification; no size is computed",
            signal=parsed,
        )
    # The equity is read here, per signal, so a process that has been up for
    # days does not size against the balance it started with. A provider that
    # cannot answer blocks the signal rather than falling back to the startup
    # number, because a stale balance silently changes the risk taken.
    try:
        equity = risk.current_equity()
    except Exception as exc:
        return _result(
            key, STATUS_BLOCKED,
            f"the account equity could not be read at sizing time: "
            f"{type(exc).__name__}: {exc}", signal=parsed)
    if equity <= 0 or not math.isfinite(equity):
        return _result(key, STATUS_BLOCKED, "account equity is missing or invalid", signal=parsed)
    # The fraction is only a sizing input in risk-fraction mode. In fixed-lot
    # mode the size is a constant and the ceiling is what constrains it, so
    # validating an unused fraction here would refuse signals for a setting
    # that has no effect on them.
    if risk.fixed_lots is None and not 0 < risk.risk_fraction <= MAX_RISK_FRACTION:
        return _result(
            key, STATUS_BLOCKED,
            f"risk configuration is invalid (per-trade risk must be in (0, {MAX_RISK_FRACTION:.1%}])",
            signal=parsed,
        )
    if (instrument.contract_units_per_lot <= 0 or instrument.lot_step <= 0
            or instrument.min_lot <= 0 or instrument.max_lot < instrument.min_lot):
        return _result(key, STATUS_BLOCKED,
                       "broker contract/lot specification is incomplete", signal=parsed)

    quote_block: dict[str, Any] | None = None
    if parsed["entry_type"] == "market":
        if quote is None:
            # Name the cause when the provider said why. Only the exception
            # *type* goes into the reason, so the column stays groupable; the
            # full text is logged by the listener, which is where the detail
            # belongs. Before this, "no provider configured" and "the session
            # expired" were recorded as the same sentence.
            cause = ""
            if quote_error:
                kind = str(quote_error).split(":", 1)[0].strip()
                if kind:
                    cause = f" ({kind})"
            return _result(key, STATUS_BLOCKED,
                           f"market signal needs a fresh broker quote{cause}",
                           signal=parsed)
        if not (math.isfinite(quote.bid) and math.isfinite(quote.ask)
                and quote.bid > 0 and quote.ask > 0 and quote.ask >= quote.bid):
            # The numbers are in the reason on purpose: "invalid quote" with no
            # values cannot be diagnosed after the fact, and a crossed book on
            # the demo feed is a real, transient thing that looks like a bug.
            return _result(
                key, STATUS_BLOCKED,
                f"broker quote is invalid (bid={quote.bid!r}, ask={quote.ask!r})",
                signal=parsed)

        # Two different questions, deliberately separated.
        #
        # ``read_s`` is how long the broker took to answer. The quote is stamped
        # when it is fetched, which happens *after* the message arrived, so this
        # is normally a small positive number. A slow read is latency: it says
        # nothing about whether the price is any good, and refusing on it used to
        # be reported as "the timestamp is in the future" - a clock fault that
        # did not exist. Only a read long enough to mean a broken connection is
        # refused here.
        #
        # ``age`` is how old the price was when the message arrived. That is the
        # freshness question, and it still refuses: a provider handing back a
        # cached tick is caught by it.
        max_age = float(risk.quote_max_age_seconds)
        read_s = (as_utc(quote.quoted_at) - as_utc(message.received_at)).total_seconds()
        if read_s > HUNG_QUOTE_READ_S:
            return _result(
                key, STATUS_BLOCKED,
                f"reading the broker quote took {format_age(read_s)}, beyond the "
                f"{format_age(HUNG_QUOTE_READ_S)} ceiling; the connection is not "
                "answering, so no price can be trusted", signal=parsed)
        age = (as_utc(message.received_at) - as_utc(quote.quoted_at)).total_seconds()
        if age > max_age:
            return _result(key, STATUS_BLOCKED, "broker quote is stale", signal=parsed)

        effective_entry = quote.ask if is_buy else quote.bid
        if (is_buy and stop >= effective_entry) or (not is_buy and stop <= effective_entry):
            return _result(key, STATUS_BLOCKED,
                           "market has already crossed the published stop", signal=parsed)
        # Only the *final* target ends the trade. The position is now held
        # through the intermediate targets, so a market already past the first
        # one is still tradeable: the stop move fires on the first observation
        # instead of waiting for a level that has already gone by. What cannot
        # be copied is a market that is already at the target the trade exists
        # to reach.
        if targets:
            final_target = max(targets) if is_buy else min(targets)
            if ((is_buy and effective_entry >= final_target)
                    or (not is_buy and effective_entry <= final_target)):
                return _result(
                    key, STATUS_MANUAL_REVIEW,
                    f"the market is already at or beyond the final target "
                    f"{final_target:g} (entry {effective_entry:g}); there is no "
                    "move left to copy", signal=parsed)
        # The first target is a different question from the last one, and the
        # gap between them is where a copied call goes wrong. The channel posts
        # "SELL NOW 3870, TP 3863/3860/3855"; if the market is already through
        # 3863 by the time the message is read, the move the signal existed for
        # has happened. Entering now means risking the full stop to reach only
        # what is left - the trader is already in profit, the copier is not.
        # That is not a budget problem and resizing cannot fix it, so it is
        # refused rather than shrunk.
        if targets:
            first_target = min(targets) if is_buy else max(targets)
            if ((is_buy and effective_entry >= first_target)
                    or (not is_buy and effective_entry <= first_target)):
                # Say where the market is *relative to the published entry*, not
                # just that a level was passed. "the market has already reached
                # the first target 3863 (entry 3861.16)" reads like the bot
                # measured something wrong; "it published 3870 and the market
                # was already 8.8 points past it" reads like what it is, which
                # is a call posted after the move. Measured over the archive,
                # 87% of this channel's calls arrive past their own entry.
                drift = (entry - effective_entry) if not is_buy else (
                    effective_entry - entry)
                return _result(
                    key, STATUS_BLOCKED,
                    f"the call was posted after the move: it published entry "
                    f"{entry:g}, the market was already {effective_entry:g} "
                    f"({drift:.1f} points past it) and through the first target "
                    f"{first_target:g}; the move this signal was for has already "
                    "happened", signal=parsed)
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

    # Risk is measured from the price the order will actually get, not from the
    # published entry. Those are not the same number: the channel writes "SELL
    # 3870" and by the time the message is read the market is at 3860.62, so the
    # stop is 19.3 points away rather than 10. Sizing from the published entry
    # computed $20 of risk for an order that would actually have risked $38.66 -
    # and the engine, which measures from the real price, then refused it. Two
    # components, two numbers, one trade. This is the single number they share.
    sizing_entry = effective_entry
    stop_distance = abs(sizing_entry - stop)
    risk_per_lot = stop_distance * instrument.contract_units_per_lot
    if risk_per_lot <= 0:
        return _result(key, STATUS_BLOCKED, "stop distance is zero", signal=parsed)

    if risk.fixed_lots is not None:
        # The configured lot is a ceiling on size, not the size itself. The
        # largest lot that fits the ceiling is used, and the signal is refused
        # only when even the broker's minimum lot would break it. Rounding is
        # down, so the ceiling is never crossed.
        lots, refusal = fit_lot_to_risk(
            cap_lots=float(risk.fixed_lots),
            spec=LotSpec(
                contract_units_per_lot=instrument.contract_units_per_lot,
                lot_step=instrument.lot_step,
                min_lot=instrument.min_lot,
                max_lot=instrument.max_lot,
                verified=bool(instrument.units_confirmed),
            ),
            stop_distance=stop_distance,
            max_risk_usd=risk.max_trade_risk_usd,
        )
        if refusal:
            return _result(key, STATUS_BLOCKED, refusal, signal=parsed)
        risk_budget = lots * risk_per_lot
        estimated_stop_risk = risk_budget
    else:
        risk_budget = equity * risk.risk_fraction
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
    # Provenance, when the amended text is all this pipeline ever saw. It is
    # recorded on the signal rather than in the refusal reason because the
    # message is traded: the trail has to say the original was never seen.
    if parsed.get("first_seen_edited"):
        signal["first_seen_edited"] = True
        signal["first_seen_edited_at"] = parsed.get("first_seen_edited_at")
    # An edit that was judged as a signal in its own right, because the earlier
    # revision never opened anything. The trail has to say so, otherwise a
    # revision 1 row that produced an intent looks like the pipeline amended a
    # live position.
    if parsed.get("supersedes_revision"):
        signal["supersedes_revision"] = parsed["supersedes_revision"]
    intent = {
        # This record describes the pipeline's own stage: it never writes to a
        # broker. Whether an order follows is decided by the execution engine
        # and recorded on the trade row. The value used to be "paper_only",
        # which reads as "this bot does not trade" even on a live account.
        "mode": "signal_only",
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
        # Which rule produced the size, and the ceiling it had to satisfy. In
        # fixed-lot mode the fraction below is inert, and saying so here is
        # what stops a later reader from believing the size was risk-scaled.
        "sizing_mode": risk.sizing_mode,
        "fixed_lots": risk.fixed_lots,
        "max_trade_risk_usd": risk.max_trade_risk_usd,
        "risk_fraction": risk.risk_fraction,
        "equity_at_sizing": round(equity, 2),
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

    def previous_revision(self, channel: str, message_id: int) -> dict[str, Any] | None:
        """Summary of every revision already stored for a message.

        Returns ``{"status", "signal", "opened", "revisions"}``: the newest
        status, the signal of the newest revision that opened a position (or the
        newest revision if none did), and whether *any* revision opened one.
        ``opened`` is the field the rule reads - a later revision must not be
        able to erase the fact that an earlier one bought.

        ``None`` means there is no earlier revision. A read that fails must
        raise rather than return ``None``: the caller needs to tell "nothing to
        protect" apart from "could not look".
        """
        ...

    def stored_text_hash(self, channel: str, message_id: int) -> str | None:
        """Hash of the text already stored for a message, if it was ever stored.

        An edit event can arrive for a message whose text has not changed. The
        edit handler compares against this so a no-op edit is not stored as a
        new revision and does not mark the message as edited.
        """
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
CREATE TABLE IF NOT EXISTS mirror_trades (
    trade_id           TEXT PRIMARY KEY,
    event_key          TEXT NOT NULL,
    channel            TEXT NOT NULL DEFAULT '',
    message_id         INTEGER NOT NULL DEFAULT 0,
    revision           INTEGER NOT NULL DEFAULT 0,
    symbol             TEXT NOT NULL DEFAULT '',
    side               TEXT NOT NULL DEFAULT '',
    size               REAL NOT NULL DEFAULT 0,
    entry              REAL NOT NULL DEFAULT 0,
    stop               REAL NOT NULL DEFAULT 0,
    current_stop       REAL,
    targets_json       TEXT NOT NULL DEFAULT '[]',
    plan_json          TEXT NOT NULL DEFAULT '{}',
    state              TEXT NOT NULL DEFAULT 'validated',
    legs_done          INTEGER NOT NULL DEFAULT 0,
    broker_order_id    INTEGER,
    broker_position_id INTEGER,
    fill_price         REAL,
    management_stopped INTEGER NOT NULL DEFAULT 0,
    note               TEXT NOT NULL DEFAULT '',
    text_sha256        TEXT NOT NULL DEFAULT '',
    created_at         TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at         TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS ix_mirror_trades_state ON mirror_trades(state);
CREATE TABLE IF NOT EXISTS mirror_transitions (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_id           TEXT NOT NULL,
    event_key          TEXT NOT NULL DEFAULT '',
    stage              TEXT NOT NULL,
    status             TEXT NOT NULL,
    detail             TEXT NOT NULL DEFAULT '',
    broker_order_id    INTEGER,
    broker_position_id INTEGER,
    message_id         INTEGER,
    revision           INTEGER,
    payload_json       TEXT,
    created_at         TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS ix_mirror_transitions_trade ON mirror_transitions(trade_id);
CREATE TABLE IF NOT EXISTS mirror_risk_state (
    id               INTEGER PRIMARY KEY CHECK (id = 1),
    day              TEXT NOT NULL,
    day_start_equity REAL NOT NULL DEFAULT 0,
    peak_equity      REAL NOT NULL DEFAULT 0,
    updated_at       TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS mirror_control (
    id               INTEGER PRIMARY KEY CHECK (id = 1),
    execution_paused INTEGER NOT NULL DEFAULT 0,
    reason           TEXT NOT NULL DEFAULT '',
    changed_at       TEXT NOT NULL DEFAULT (datetime('now')),
    changed_by       TEXT NOT NULL DEFAULT ''
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

        # The trade table gained the hash of the text it came from, so that the
        # same call posted under two message ids can be recognised as one idea
        # without depending on the event row being written first.
        have_trades = {row[1] for row in con.execute("PRAGMA table_info(mirror_trades)")}
        if "text_sha256" not in have_trades:
            con.execute("ALTER TABLE mirror_trades ADD COLUMN text_sha256 "
                        "TEXT NOT NULL DEFAULT ''")

        # Rows from the old code carry ok=true but were written *before* the
        # send, so they prove the slot was claimed, not that the message
        # arrived. They are backfilled as failed so the retry pass picks them
        # up: a duplicate notification is a smaller problem than a lost one.
        # attempts = 0 marks a row the new code has never touched.
        con.execute(
            "UPDATE notification_log "
            "SET status='failed', detail=coalesce(detail, ?) "
            "WHERE status='pending' AND attempts=0",
            ("legacy row: the slot was recorded before the send, so delivery "
             "is unconfirmed",),
        )

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

    def previous_revision(self, channel: str, message_id: int) -> dict[str, Any] | None:
        with self._connect() as con:
            rows = con.execute(
                "SELECT revision, status, signal_json FROM mirror_events"
                " WHERE channel=? AND message_id=? ORDER BY revision DESC LIMIT 100",
                (channel, message_id),
            ).fetchall()
        parsed = []
        for _revision, status, signal_json in rows:
            try:
                signal = json.loads(signal_json) if signal_json else {}
            except (TypeError, ValueError):
                signal = {}
            parsed.append({"status": status,
                           "signal": signal if isinstance(signal, dict) else {}})
        return _summarise_revisions(parsed)

    def stored_text_hash(self, channel: str, message_id: int) -> str | None:
        with self._connect() as con:
            row = con.execute(
                "SELECT text_sha256 FROM mirror_events WHERE channel=? AND message_id=?"
                " ORDER BY revision DESC LIMIT 1",
                (channel, message_id),
            ).fetchone()
        return row[0] if row and row[0] else None

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
        """Notifications that were not confirmed delivered, oldest first.

        Both ``failed`` and ``pending`` are included: a ``pending`` row is one
        whose send was interrupted, and the docstring's promise that it "is
        retried" used to be false.
        """
        with self._connect() as con:
            rows = con.execute(
                "SELECT n.event_key, n.kind, n.detail, n.attempts, "
                "e.status AS event_status, e.reason AS event_reason, "
                "e.signal_json, e.intent_json, e.channel, e.message_id "
                "FROM notification_log n "
                "LEFT JOIN mirror_events e ON e.event_key = n.event_key "
                "WHERE n.status IN ('failed','pending') "
                "ORDER BY n.sent_at ASC LIMIT ?",
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

    # -- trades ------------------------------------------------------------
    #
    # A trade is a different thing from an event. An event is a Telegram
    # message and exists once; a trade spans many broker interactions and
    # changes state. The trade row is the current state, and every change to
    # it also writes an append-only transition, so the history is never lost
    # by an overwrite.

    def open_trade(self, row: dict) -> str:
        """Create a trade once. Returns ``inserted`` or ``existing``."""
        with self._connect() as con:
            cur = con.execute(
                """INSERT OR IGNORE INTO mirror_trades
                   (trade_id, event_key, channel, message_id, revision, symbol, side,
                    size, entry, stop, current_stop, targets_json, plan_json, state,
                    legs_done, broker_order_id, broker_position_id, fill_price,
                    management_stopped, note, text_sha256, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (row["trade_id"], row["event_key"], row.get("channel", ""),
                 int(row.get("message_id", 0) or 0), int(row.get("revision", 0) or 0),
                 row.get("symbol", ""), row.get("side", ""),
                 float(row.get("size", 0) or 0), float(row.get("entry", 0) or 0),
                 float(row.get("stop", 0) or 0),
                 _opt_float(row.get("current_stop")),
                 json.dumps(row.get("targets") or [], sort_keys=True),
                 json.dumps(row.get("plan") or {}, sort_keys=True),
                 row.get("state", "validated"), int(row.get("legs_done", 0) or 0),
                 _opt_int(row.get("broker_order_id")),
                 _opt_int(row.get("broker_position_id")),
                 _opt_float(row.get("fill_price")),
                 1 if row.get("management_stopped") else 0,
                 row.get("note", ""),
                 row.get("text_sha256", ""),
                 row.get("created_at") or datetime.now(timezone.utc).isoformat(),
                 row.get("updated_at") or datetime.now(timezone.utc).isoformat()),
            )
            return CLAIM_INSERTED if cur.rowcount == 1 else CLAIM_DUPLICATE

    def update_trade(self, row: dict) -> bool:
        """Overwrite the current state of a trade."""
        with self._connect() as con:
            cur = con.execute(
                """UPDATE mirror_trades SET
                     state=?, legs_done=?, broker_order_id=?, broker_position_id=?,
                     fill_price=?, current_stop=?, management_stopped=?, note=?,
                     updated_at=?
                   WHERE trade_id=?""",
                (row.get("state", "validated"), int(row.get("legs_done", 0) or 0),
                 _opt_int(row.get("broker_order_id")),
                 _opt_int(row.get("broker_position_id")),
                 _opt_float(row.get("fill_price")),
                 _opt_float(row.get("current_stop")),
                 1 if row.get("management_stopped") else 0,
                 row.get("note", ""),
                 row.get("updated_at") or datetime.now(timezone.utc).isoformat(),
                 row["trade_id"]),
            )
            return cur.rowcount > 0

    def get_trade(self, trade_id: str) -> dict | None:
        with self._connect() as con:
            cursor = con.execute(
                "SELECT * FROM mirror_trades WHERE trade_id=?", (trade_id,))
            row = cursor.fetchone()
            if row is None:
                return None
            columns = [d[0] for d in cursor.description]
        return _trade_row_to_dict(dict(zip(columns, row)))

    def list_trades(self, *, states: tuple[str, ...] | None = None) -> list[dict]:
        sql = "SELECT * FROM mirror_trades"
        params: list[Any] = []
        if states:
            sql += f" WHERE state IN ({','.join('?' * len(states))})"
            params.extend(states)
        sql += " ORDER BY created_at ASC"
        with self._connect() as con:
            cursor = con.execute(sql, params)
            rows = cursor.fetchall()
            columns = [d[0] for d in cursor.description]
        return [_trade_row_to_dict(dict(zip(columns, row))) for row in rows]

    def record_transition(self, transition: dict) -> None:
        """Append one state transition. Transitions are never overwritten."""
        with self._connect() as con:
            con.execute(
                """INSERT INTO mirror_transitions
                   (trade_id, event_key, stage, status, detail, broker_order_id,
                    broker_position_id, message_id, revision, payload_json, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (transition["trade_id"], transition.get("event_key", ""),
                 transition.get("stage", ""), transition.get("status", ""),
                 transition.get("detail", ""),
                 _opt_int(transition.get("broker_order_id")),
                 _opt_int(transition.get("broker_position_id")),
                 _opt_int(transition.get("message_id")),
                 _opt_int(transition.get("revision")),
                 json.dumps(transition.get("payload") or {}, sort_keys=True),
                 transition.get("created_at") or datetime.now(timezone.utc).isoformat()),
            )

    def transitions(self, trade_id: str) -> list[dict]:
        with self._connect() as con:
            rows = con.execute(
                "SELECT * FROM mirror_transitions WHERE trade_id=? ORDER BY id ASC",
                (trade_id,),
            ).fetchall()
            columns = [d[0] for d in con.execute(
                "SELECT * FROM mirror_transitions WHERE trade_id=? ORDER BY id ASC",
                (trade_id,),
            ).description]
        out = []
        for row in rows:
            item = dict(zip(columns, row))
            if item.get("payload_json"):
                try:
                    item["payload"] = json.loads(item["payload_json"])
                except ValueError:
                    item["payload"] = None
            out.append(item)
        return out

    # -- risk state and the pause switch ----------------------------------

    def risk_state(self) -> dict | None:
        """The persisted daily-loss and drawdown state, if there is one."""
        with self._connect() as con:
            row = con.execute(
                "SELECT day, day_start_equity, peak_equity FROM mirror_risk_state "
                "WHERE id=1").fetchone()
        if row is None:
            return None
        return {"day": row[0], "day_start_equity": row[1], "peak_equity": row[2]}

    def save_risk_state(self, *, day: str, day_start_equity: float,
                        peak_equity: float) -> None:
        with self._connect() as con:
            con.execute(
                """INSERT INTO mirror_risk_state
                   (id, day, day_start_equity, peak_equity, updated_at)
                   VALUES (1, ?, ?, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET
                       day=excluded.day,
                       day_start_equity=excluded.day_start_equity,
                       peak_equity=excluded.peak_equity,
                       updated_at=excluded.updated_at""",
                (day, float(day_start_equity), float(peak_equity),
                 datetime.now(timezone.utc).isoformat()),
            )

    def control(self) -> dict | None:
        """The pause switch. ``None`` means it has never been written."""
        with self._connect() as con:
            row = con.execute(
                "SELECT execution_paused, reason, changed_at, changed_by "
                "FROM mirror_control WHERE id=1").fetchone()
        if row is None:
            return None
        return {"paused": bool(row[0]), "reason": row[1] or "",
                "changed_at": row[2], "changed_by": row[3] or ""}

    def set_paused(self, *, paused: bool, reason: str, changed_by: str) -> dict:
        """Write the pause switch. The row is the switch, not a copy of it."""
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as con:
            con.execute(
                """INSERT INTO mirror_control
                   (id, execution_paused, reason, changed_at, changed_by)
                   VALUES (1, ?, ?, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET
                       execution_paused=excluded.execution_paused,
                       reason=excluded.reason,
                       changed_at=excluded.changed_at,
                       changed_by=excluded.changed_by""",
                (1 if paused else 0, reason or "", now, changed_by or ""),
            )
        return {"paused": bool(paused), "reason": reason or "",
                "changed_at": now, "changed_by": changed_by or ""}

    def same_text_recently(self, text_sha256: str, *, since: str,
                           exclude_trade_id: str = "") -> dict | None:
        """A trade already opened from identical text, if one is recent.

        The channel posts the same signal twice as two messages with two ids
        (observed: 18788 and 18789, identical text and timestamp). ``event_key``
        includes the message id, so the database's uniqueness constraint cannot
        see that they are the same call, and copying both would double the risk
        on one idea. This is what notices.

        The trade row carries the hash of the text it came from, so this works
        however ``execute`` was reached — through the listener or directly.
        """
        with self._connect() as con:
            row = con.execute(
                """SELECT trade_id, message_id, created_at, state
                   FROM mirror_trades
                   WHERE text_sha256=? AND text_sha256<>'' AND trade_id<>?
                     AND created_at>=?
                   ORDER BY created_at ASC LIMIT 1""",
                (text_sha256, exclude_trade_id, since)).fetchone()
        if row is None:
            return None
        return {"trade_id": row[0], "message_id": row[1],
                "received_at": row[2], "status": row[3]}

    def recent(self, *, limit: int = 50, statuses: list[str] | None = None) -> list[dict]:
        """Newest events first, shaped like the Supabase adapter's rows."""
        sql = ("SELECT event_key, channel, message_id, revision, posted_at, received_at, "
               "status, reason, raw_text, signal_json, intent_json, source "
               "FROM mirror_events")
        params: list[Any] = []
        if statuses:
            sql += f" WHERE status IN ({','.join('?' * len(statuses))})"
            params.extend(statuses)
        sql += " ORDER BY received_at DESC LIMIT ?"
        params.append(int(limit))
        with self._connect() as con:
            rows = con.execute(sql, params).fetchall()
        keys = ("event_key", "channel", "message_id", "revision", "posted_at", "received_at",
                "status", "reason", "raw_text", "signal_json", "intent_json", "source")
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
    quote_error: str | None = None,
    now: datetime | None = None,
) -> PipelineResult:
    """Evaluate a message and record it exactly once."""
    # Only a later revision needs the earlier one. Reading it on every message
    # would put a database round-trip on the hot path for the common case,
    # which is a message seen for the first time.
    previous: dict[str, Any] | None = None
    if message.revision > 0:
        try:
            previous = ledger.previous_revision(message.channel, message.message_id)
        except Exception:
            # Unknown is not absent. Leaving it None sends the edit to a human
            # rather than assuming the earlier revision opened nothing.
            previous = None
    result = evaluate_message(message, instrument=instrument, risk=risk,
                              quote=quote, quote_error=quote_error, now=now,
                              previous=previous)
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
