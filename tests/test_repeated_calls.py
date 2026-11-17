"""A repeated call is answered from storage, never with broker round-trips.

The channel posts one idea two or three times under different message ids.
Every copy used to buy a fresh quote and a fresh equity before anything noticed
the text was the same - a burst of copies was a burst of requests, and a burst
of requests is exactly the shape of traffic that gets a broker's edge to refuse
us. Storage can answer "we have seen this text" for almost nothing.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from rade.observer.mirror_pipeline import (
    InstrumentSpec,
    MarketQuote,
    MessageEnvelope,
    RiskConfig,
    SQLiteIntentLedger,
    process_once,
)

NOW = datetime(2026, 11, 17, 13, 11, 42, tzinfo=timezone.utc)
INSTRUMENT = InstrumentSpec("XAUUSD", 100.0, 0.01, 0.01, 50.0, units_confirmed=True)
RISK = RiskConfig(10_000, 0.005, 2.0, fixed_lots=0.03, max_trade_risk_usd=50.0)
CALL = "XAUUSD BUY NOW 3854\nSl : 3845\nTp : 3860\nTp : 3865\nTp : 3870"


def envelope(mid: int, *, revision: int = 0, text: str = CALL,
             at: datetime | None = None) -> MessageEnvelope:
    when = at or NOW
    return MessageEnvelope(channel="@source", message_id=mid, revision=revision,
                           posted_at=when, received_at=when, text=text,
                           source="test")


def test_the_second_copy_is_answered_without_a_second_evaluation(tmp_path):
    """Same text, different message id, seconds apart: one evaluation only."""
    ledger = SQLiteIntentLedger(tmp_path / "ledger.sqlite")
    first = process_once(envelope(18925), ledger=ledger, instrument=INSTRUMENT,
                         risk=RISK, quote=MarketQuote(3858, 3858.2, NOW), now=NOW)
    second = process_once(envelope(18926), ledger=ledger, instrument=INSTRUMENT,
                          risk=RISK, quote=MarketQuote(3858, 3858.2, NOW), now=NOW)

    assert first.status == "paper_intent"
    assert second.status == "duplicate"
    assert "identical to message 18925" in second.reason
    # No intent was built, which is the whole point: nothing was sized, so no
    # price was needed and no equity was read to size it with.
    assert second.order_intent is None
    assert second.signal is None


def test_a_copy_outside_the_window_is_evaluated_normally(tmp_path):
    """The window is short on purpose: a re-posted call hours later is new."""
    ledger = SQLiteIntentLedger(tmp_path / "ledger.sqlite")
    process_once(envelope(1), ledger=ledger, instrument=INSTRUMENT, risk=RISK,
                 quote=MarketQuote(3858, 3858.2, NOW), now=NOW)
    later = NOW + timedelta(hours=3)
    result = process_once(envelope(2, at=later), ledger=ledger,
                          instrument=INSTRUMENT, risk=RISK,
                          quote=MarketQuote(3861, 3861.2, later), now=later)
    assert result.status == "paper_intent"
    assert result.order_intent is not None


def test_a_shorter_window_releases_the_second_copy(tmp_path):
    ledger = SQLiteIntentLedger(tmp_path / "ledger.sqlite")
    process_once(envelope(1), ledger=ledger, instrument=INSTRUMENT, risk=RISK,
                 quote=MarketQuote(3858, 3858.2, NOW), now=NOW)
    later = NOW + timedelta(minutes=20)
    result = process_once(envelope(2, at=later), ledger=ledger,
                          instrument=INSTRUMENT, risk=RISK,
                          quote=MarketQuote(3858, 3858.2, later), now=later,
                          duplicate_window_s=600.0)
    assert result.status == "paper_intent"


def test_an_edit_is_never_swallowed_by_the_repeated_call_check(tmp_path):
    """A revision is the channel changing its mind; it must reach a human."""
    ledger = SQLiteIntentLedger(tmp_path / "ledger.sqlite")
    process_once(envelope(1), ledger=ledger, instrument=INSTRUMENT, risk=RISK,
                 quote=MarketQuote(3858, 3858.2, NOW), now=NOW)
    result = process_once(envelope(1, revision=1,
                                   text=CALL.replace("3860", "3858")),
                          ledger=ledger, instrument=INSTRUMENT, risk=RISK,
                          quote=MarketQuote(3858, 3858.2, NOW), now=NOW)
    assert result.status == "manual_review"


def test_the_repeated_event_is_still_recorded(tmp_path):
    """Skipping the broker must not skip the bookkeeping: the cursor advances
    by claiming the event, or the listener would re-read the same message
    forever."""
    ledger = SQLiteIntentLedger(tmp_path / "ledger.sqlite")
    process_once(envelope(18925), ledger=ledger, instrument=INSTRUMENT,
                 risk=RISK, quote=MarketQuote(3858, 3858.2, NOW), now=NOW)
    process_once(envelope(18926), ledger=ledger, instrument=INSTRUMENT,
                 risk=RISK, quote=MarketQuote(3858, 3858.2, NOW), now=NOW)
    stored = ledger.counts()
    assert stored.get("paper_intent") == 1
    assert stored.get("duplicate") == 1
