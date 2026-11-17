"""The quote check, tested against the way a broker actually answers.

This file exists because the incident of 2026-11-16 was not a threshold that
needed tuning. The listener fetches the quote *after* the message arrives and
the provider stamps it at fetch time, so the difference between the two
timestamps is the **duration of the read**, not the age of the price. The old
code read a negative number as "this timestamp is in the future" and refused the
signal; a 2.5 s broker round-trip was enough.

Every test here therefore uses a message that is realistic in the way the
incident was realistic: posted a minute earlier, delivered by the poll, priced
by a read that took a moment.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from rade.observer.mirror_pipeline import (
    HUNG_QUOTE_READ_S,
    STATUS_BLOCKED,
    STATUS_PAPER_INTENT,
    STATUS_STALE,
    InstrumentSpec,
    MarketQuote,
    MessageEnvelope,
    RiskConfig,
    evaluate_message,
)

NOW = datetime(2026, 11, 16, 23, 10, tzinfo=timezone.utc)
INSTRUMENT = InstrumentSpec("XAUUSD", 100, 0.01, 0.01, 50, units_confirmed=True)
RISK = RiskConfig(4862.73, 0.005, 2.0, signal_max_age_seconds=900.0)
CALL = "XAUUSD BUY NOW 3850\nSl : 3845\nTp : 3860\nTp : 3865\nTp : 3870"


def message(*, lag_s: float = 0.0, source: str = "catchup") -> MessageEnvelope:
    """A call posted ``lag_s`` before the pipeline received it."""
    return MessageEnvelope("@test", 18048, 0, NOW - timedelta(seconds=lag_s), NOW,
                           CALL, source=source)


def quote_read_after(*, seconds: float) -> MarketQuote:
    """A quote whose timestamp is ``seconds`` *after* receipt - a slow read."""
    return MarketQuote(3850.1, 3850.4, NOW + timedelta(seconds=seconds))


def evaluate(msg, *, quote=None, quote_error=None, risk=RISK):
    return evaluate_message(msg, instrument=INSTRUMENT, risk=risk,
                            quote=quote, quote_error=quote_error)


# ---------------------------------------------------------------------------
# the incident: a slow read is not a stale price
# ---------------------------------------------------------------------------


def test_a_slow_quote_read_does_not_block_the_signal():
    # 2.5 s is what the deployed service saw. It must trade.
    result = evaluate(message(lag_s=60), quote=quote_read_after(seconds=2.5))
    assert result.status == STATUS_PAPER_INTENT, result.reason
    assert result.order_intent is not None


def test_a_five_second_read_does_not_block_the_signal():
    result = evaluate(message(lag_s=60), quote=quote_read_after(seconds=5.0))
    assert result.status == STATUS_PAPER_INTENT, result.reason


def test_a_read_just_inside_the_ceiling_still_trades():
    result = evaluate(message(lag_s=60),
                      quote=quote_read_after(seconds=HUNG_QUOTE_READ_S - 0.1))
    assert result.status == STATUS_PAPER_INTENT, result.reason


def test_a_read_past_the_ceiling_is_refused_and_names_the_latency():
    result = evaluate(message(lag_s=60),
                      quote=quote_read_after(seconds=HUNG_QUOTE_READ_S + 1.0))
    assert result.status == STATUS_BLOCKED
    assert "took" in result.reason
    assert "future" not in result.reason


# ---------------------------------------------------------------------------
# the freshness question is still asked - just not of the read duration
# ---------------------------------------------------------------------------


def test_a_genuinely_old_quote_is_still_stale():
    # Stamped 10 s *before* the message arrived: a cached tick, not a slow read.
    old = MarketQuote(3850.1, 3850.4, NOW - timedelta(seconds=10))
    result = evaluate(message(lag_s=15), quote=old)
    assert result.status == STATUS_BLOCKED
    assert "stale" in result.reason


def test_a_frozen_feed_is_not_mistaken_for_a_fresh_one():
    # The provider returns the same price with a fresh timestamp. The read is
    # fast and the price is "current", so this passes - the spread and slippage
    # guards are what bound the damage, and that is documented rather than
    # pretended away. The test pins that a fast read is never blocked.
    frozen = MarketQuote(3850.1, 3850.4, NOW)
    result = evaluate(message(lag_s=60), quote=frozen)
    assert result.status == STATUS_PAPER_INTENT, result.reason


# ---------------------------------------------------------------------------
# a provider failure must be diagnosable
# ---------------------------------------------------------------------------


def test_a_provider_failure_names_its_cause():
    result = evaluate(message(lag_s=60), quote=None,
                      quote_error="ConnectionError: TradeLocker session expired (401)")
    assert result.status == STATUS_BLOCKED
    assert "ConnectionError" in result.reason
    assert "fresh broker quote" in result.reason


def test_no_provider_configured_is_distinguishable():
    plain = evaluate(message(lag_s=60), quote=None)
    named = evaluate(message(lag_s=60), quote=None, quote_error="TimeoutError: 30s")
    assert plain.reason != named.reason
    assert "TimeoutError" in named.reason


# ---------------------------------------------------------------------------
# one age rule for every source
# ---------------------------------------------------------------------------


def test_a_catchup_signal_older_than_the_limit_is_stale():
    # There used to be a lenient branch for catch-up that made this pass
    # regardless of the configured limit. It is gone: the limit is the allowance.
    result = evaluate(message(lag_s=1000, source="catchup"),
                      quote=quote_read_after(seconds=0.3))
    assert result.status == STATUS_STALE
    assert "stale" in result.reason


def test_a_catchup_signal_inside_the_limit_is_traded():
    result = evaluate(message(lag_s=300, source="catchup"),
                      quote=quote_read_after(seconds=0.3))
    assert result.status == STATUS_PAPER_INTENT, result.reason
