"""Tests for the Telegram signal-age guard.

The quote check and this check are easy to confuse, and confusing them is the
bug this file exists to prevent. A quote is *the price right now*; it says
nothing about when the instruction was written. A signal recovered by a poll, a
catch-up after a reconnect, or a restart arrives with a perfectly fresh quote
and a three-hour-old instruction, and the price has often not moved enough for
the slippage guard to notice.

So every test here uses a quote that is current, and varies only the age of the
message. If the age guard were missing, all of them would produce an intent.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from rade.execution.engine import ExecutionEngine
from rade.observer.mirror_pipeline import (
    STATUS_PAPER_INTENT,
    STATUS_STALE,
    InstrumentSpec,
    MarketQuote,
    MessageEnvelope,
    RiskConfig,
    SQLiteIntentLedger,
    envelope_from_telegram,
    evaluate_message,
    format_age,
    process_once,
    signal_age_seconds,
    stale_reason,
)
from rade.observer.notify import NOTIFY_STATUSES, RecordingNotifier

NOW = datetime(2026, 11, 13, 12, 0, tzinfo=timezone.utc)
INSTRUMENT = InstrumentSpec("XAUUSD", 100, 0.01, 0.01, 50, units_confirmed=True)
RISK = RiskConfig(10_000, 0.005, 2.0, signal_max_age_seconds=120.0)
SELL = "XAUUSD SELL NOW 3915\nSl : 3925\nTp : 3910\nTp : 3905\nTp : 3900"


def message(*, age_s: float = 0.0, mid: int = 100, text: str = SELL,
            posted_at: datetime | None = "default") -> MessageEnvelope:
    """A message that was posted ``age_s`` seconds before it was received."""
    posted = (NOW - timedelta(seconds=age_s)
              if posted_at == "default" else posted_at)
    return MessageEnvelope("@test", mid, 0, posted, NOW, text)


def fresh_quote() -> MarketQuote:
    """A quote stamped at receipt: as current as a quote can be."""
    return MarketQuote(3915.0, 3915.0, NOW)


def evaluate(msg: MessageEnvelope, *, risk: RiskConfig = RISK, quote=None):
    return evaluate_message(msg, instrument=INSTRUMENT, risk=risk,
                            quote=quote or fresh_quote())


# ---------------------------------------------------------------------------
# the age itself
# ---------------------------------------------------------------------------


def test_a_signal_that_just_arrived_is_not_stale():
    result = evaluate(message(age_s=0))
    assert result.status == STATUS_PAPER_INTENT
    assert result.order_intent is not None


def test_age_is_measured_from_posting_to_receipt():
    assert signal_age_seconds(message(age_s=37)) == pytest.approx(37.0)


def test_a_missing_posted_timestamp_has_no_age():
    assert signal_age_seconds(message(posted_at=None)) is None


def test_durations_are_readable_at_every_scale():
    assert format_age(4) == "4s"
    assert format_age(150) == "2m"
    assert format_age(3 * 3600) == "3.0h"
    assert format_age(50 * 3600) == "2.1d"


# ---------------------------------------------------------------------------
# the limit
# ---------------------------------------------------------------------------


def test_a_signal_older_than_the_limit_is_stale_and_says_so():
    result = evaluate(message(age_s=300))

    assert result.status == STATUS_STALE
    assert result.order_intent is None, "a stale signal must never be sized"
    assert "stale" in result.reason
    assert "5m" in result.reason, "the reason names how late it was"
    assert "2m" in result.reason, "and the limit it broke"
    assert "MIRROR_MAX_SIGNAL_AGE_S" in result.reason


def test_the_limit_is_inclusive_at_the_boundary():
    # Exactly at the limit is admitted; the limit is a ceiling on lateness, not
    # a target to approach. One second past it is refused.
    assert evaluate(message(age_s=120)).status == STATUS_PAPER_INTENT
    assert evaluate(message(age_s=121)).status == STATUS_STALE


def test_a_stale_signal_is_refused_even_with_a_perfect_quote():
    """The whole point: a current price does not make a current instruction."""
    quote = MarketQuote(3915.0, 3915.0, NOW)  # zero age, zero slippage
    result = evaluate(message(age_s=3600), quote=quote)
    assert result.status == STATUS_STALE


def test_a_stale_signal_never_reads_the_equity():
    """Nothing is sized, so the broker is not even asked for the balance."""
    calls: list[int] = []

    def provider() -> float:
        calls.append(1)
        return 10_000.0

    risk = RiskConfig(0, 0.005, 2.0, equity_provider=provider,
                      signal_max_age_seconds=120.0)
    result = evaluate(message(age_s=600), risk=risk)

    assert result.status == STATUS_STALE
    assert calls == [], "sizing must not start for a signal that is already stale"


def test_the_limit_is_configurable():
    lenient = RiskConfig(10_000, 0.005, 2.0, signal_max_age_seconds=600.0)
    assert evaluate(message(age_s=300), risk=lenient).status == STATUS_PAPER_INTENT
    assert evaluate(message(age_s=900), risk=lenient).status == STATUS_STALE


def test_zero_disables_the_limit_but_not_the_timestamp_checks():
    off = RiskConfig(10_000, 0.005, 2.0, signal_max_age_seconds=0.0)
    assert evaluate(message(age_s=86_400), risk=off).status == STATUS_PAPER_INTENT
    # An unusable timestamp is a data problem, not a limit, so it survives.
    assert evaluate(message(posted_at=None), risk=off).status == STATUS_STALE


# ---------------------------------------------------------------------------
# timestamps that cannot be trusted
# ---------------------------------------------------------------------------


def test_a_signal_with_no_posted_timestamp_is_not_traded():
    result = evaluate(message(posted_at=None))
    assert result.status == STATUS_STALE
    assert "no posted timestamp" in result.reason
    assert result.order_intent is None


def test_a_timestamp_far_in_the_future_is_refused():
    result = evaluate(message(posted_at=NOW + timedelta(minutes=30)))
    assert result.status == STATUS_STALE
    assert "in the future" in result.reason


def test_a_small_clock_disagreement_is_tolerated():
    """Telegram's clock and ours will never agree exactly; that is not a signal."""
    result = evaluate(message(posted_at=NOW + timedelta(seconds=20)))
    assert result.status == STATUS_PAPER_INTENT


# ---------------------------------------------------------------------------
# the paths that produce a late signal
# ---------------------------------------------------------------------------


def test_a_catch_up_after_a_reconnect_is_stale():
    """The listener rebuilds the envelope at fetch time, so receipt is now."""
    envelope = envelope_from_telegram(
        type("M", (), {"id": 18788, "date": NOW - timedelta(minutes=45),
                       "message": SELL, "edit_date": None})(),
        channel="@test",
        received_at=NOW,
    )
    result = evaluate(envelope)
    assert result.status == STATUS_STALE
    assert "45m" in result.reason


def test_replaying_a_stored_envelope_against_a_later_clock_is_stale():
    """A stored envelope keeps its original receipt time; ``now`` restores the
    judgement it would get today."""
    stored = message(age_s=0, mid=555)  # received at NOW
    assert evaluate(stored).status == STATUS_PAPER_INTENT
    later = evaluate_message(stored, instrument=INSTRUMENT, risk=RISK,
                             quote=fresh_quote(), now=NOW + timedelta(minutes=10))
    assert later.status == STATUS_STALE


def test_a_stale_limit_signal_is_not_labelled_stale():
    """LIMIT calls are never copied, and that is the reason that gets recorded.

    Labelling them stale would hide the real reason and make the two problems
    impossible to tell apart in the ledger.
    """
    limit = "XAUUSD SELL LIMIT 3915\nSl : 3925\nTp : 3910\nTp : 3905\nTp : 3900"
    result = evaluate(message(age_s=3600, text=limit))
    assert result.status == STATUS_PAPER_INTENT


# ---------------------------------------------------------------------------
# what the rest of the system does with a stale signal
# ---------------------------------------------------------------------------


def test_a_stale_signal_is_recorded_once_with_its_status(tmp_path):
    ledger = SQLiteIntentLedger(tmp_path / "s.sqlite")
    result = process_once(message(age_s=900, mid=777), ledger=ledger,
                          instrument=INSTRUMENT, risk=RISK, quote=fresh_quote())

    assert result.status == STATUS_STALE
    counts = ledger.counts()
    assert counts.get(STATUS_STALE) == 1
    # A redelivery of the same message does not become a second row.
    again = process_once(message(age_s=900, mid=777), ledger=ledger,
                         instrument=INSTRUMENT, risk=RISK, quote=fresh_quote())
    assert again.duplicate is True
    assert ledger.counts().get(STATUS_STALE) == 1


def test_the_engine_refuses_to_submit_a_stale_signal(tmp_path):
    """Belt and braces: the status gate in the engine is the last word."""
    from tests.test_execution import FakeBroker

    ledger = SQLiteIntentLedger(tmp_path / "e.sqlite")
    broker = FakeBroker()
    engine = ExecutionEngine(broker=broker, ledger=ledger,
                             notifier=RecordingNotifier(), log=lambda line: None)
    msg = message(age_s=900, mid=888)
    result = evaluate(msg)

    outcome = engine.execute(result, msg)

    assert outcome.get("executed") is False
    assert outcome.get("reason") == f"signal status {STATUS_STALE}"
    assert broker.place_calls == [], "no order may leave the process"
    assert ledger.list_trades() == []


def test_a_stale_signal_notifies_as_stale_not_as_blocked():
    """An operator needs to know the listener was away, not that the market was
    wrong."""
    assert NOTIFY_STATUSES[STATUS_STALE] == "stale"


# ---------------------------------------------------------------------------
# the test transport for notifications
# ---------------------------------------------------------------------------


def test_the_file_notifier_records_each_message_once(tmp_path):
    """The test transport exists so the notify path can be proved without a bot."""
    from rade.observer.notify import FileNotifier

    path = tmp_path / "alerts.jsonl"
    notifier = FileNotifier(path=str(path))
    ok, detail = notifier.send("[STALE] a signal arrived late")
    assert ok is True
    assert "written to" in detail

    lines = path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["text"] == "[STALE] a signal arrived late"
    assert record["at"]


def test_the_file_notifier_is_refused_on_a_hosted_deployment(tmp_path, monkeypatch):
    """Writing alerts to a disk that gets wiped looks exactly like sending them."""
    from rade.observer.notify import FileNotifier

    monkeypatch.setenv("RENDER", "true")
    with pytest.raises(RuntimeError, match="hosted deployment"):
        FileNotifier(path=str(tmp_path / "alerts.jsonl"))


def test_the_env_picks_the_file_transport_when_asked(tmp_path, monkeypatch):
    from rade.observer.notify import FileNotifier, notifier_from_env

    monkeypatch.delenv("RENDER", raising=False)
    monkeypatch.delenv("MIRROR_REQUIRE_DURABLE_STORE", raising=False)
    monkeypatch.setenv("MIRROR_NOTIFY_FILE", str(tmp_path / "alerts.jsonl"))
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    assert isinstance(notifier_from_env(), FileNotifier)


def test_every_notified_disposition_renders_a_message():
    """A status added to the notify map must also render.

    The live run caught this the hard way: ``stale`` was added to
    NOTIFY_STATUSES but not to the header table, so the first stale signal
    killed the listener thread with KeyError instead of notifying. Iterating the
    map means a future status cannot repeat it.
    """
    from rade.observer.mirror_pipeline import PipelineResult
    from rade.observer.notify import NOTIFY_STATUSES, build_notification

    for status in NOTIFY_STATUSES:
        result = PipelineResult(
            event_key="k", status=status, reason="because",
            signal={"side": "sell", "instrument": "XAUUSD",
                    "entry_type": "market", "published_entry": 1.0,
                    "stop_loss": 2.0, "take_profits": [0.5]},
            order_intent=None, notification_preview="")
        text = build_notification(result)
        assert text is not None, status
        assert status in text
        assert "no broker order was sent" in text
