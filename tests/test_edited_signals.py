"""A later revision of a signal: what is traded, what is quiet, what waits.

The channel amends its own posts a couple of seconds after publishing, usually
just to add a tick mark. The rule used to be "a later revision is never applied
automatically", which was really "any edit is a human's problem". That is wrong
in both directions: it filed a tick mark as a review, and it refused the edited
version of a call the pipeline had never acted on in the first place.

The rule is about money, not about editing:

* nothing was opened for the earlier revision -> the edit is the best version of
  a signal nobody acted on, so it is judged like any other signal;
* something was opened and the edit changes the trade -> a human, because moving
  a live stop is not a decision this pipeline makes on its own;
* something was opened and the edit changes only the wording -> cosmetic, filed
  as a duplicate so it cannot reach the executor by any path.

Every uncertain case falls back to review. A needless review is a nuisance; a
missed amendment is a position with the wrong stop.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from rade.observer.mirror_pipeline import (
    InstrumentSpec,
    MarketQuote,
    MessageEnvelope,
    RiskConfig,
    SQLiteIntentLedger,
    evaluate_message,
    process_once,
)
from rade.observer.notify import NOTIFY_STATUSES

NOW = datetime(2026, 11, 12, 12, 0, tzinfo=timezone.utc)
INSTRUMENT = InstrumentSpec("XAUUSD", 100, 0.01, 0.01, 50, units_confirmed=True)
RISK = RiskConfig(10_000, 0.005, 2.0)
QUOTE = MarketQuote(3915, 3915, NOW)

SELL = "XAUUSD SELL NOW 3915\nSl : 3925\nTp : 3910\nTp : 3905\nTp : 3900"
#: What the channel actually publishes after its edit: the same call with a tick.
SELL_TICKED = SELL.replace("3915", "3915✅", 1)
BUY = "XAUUSD BUY NOW 3915\nSl : 3905\nTp : 3920"


def envelope(text=SELL, *, revision=0, mid=100, posted_at=None, received_at=None):
    return MessageEnvelope("@source", mid, revision,
                           posted_at or NOW, received_at or NOW, text)


def run(ledger, text=SELL, *, revision=0, mid=100, quote=QUOTE):
    return process_once(envelope(text, revision=revision, mid=mid), ledger=ledger,
                        instrument=INSTRUMENT, risk=RISK, quote=quote)


def accepted(ledger, *, mid=100):
    """The pipeline acted on the original, so a position may now be open."""
    result = run(ledger, mid=mid)
    assert result.status == "paper_intent"
    return result


# --------------------------------------------------------------------------
# the tick mark: the case that fills the dashboard with phantom reviews
# --------------------------------------------------------------------------


def test_a_tick_mark_after_an_open_intent_is_not_a_review(tmp_path):
    ledger = SQLiteIntentLedger(tmp_path / "l.sqlite")
    accepted(ledger)

    edited = run(ledger, SELL_TICKED, revision=1)

    assert edited.status == "duplicate"
    assert edited.requires_review is False
    assert "cosmetic edit" in edited.reason
    assert edited.order_intent is None


def test_a_cosmetic_edit_can_never_reach_the_executor(tmp_path):
    """Duplicate, not merely ignored: the flag is a second line of defence."""
    ledger = SQLiteIntentLedger(tmp_path / "l.sqlite")
    accepted(ledger)

    edited = run(ledger, SELL_TICKED, revision=1)

    assert edited.duplicate is True
    assert edited.order_intent is None


def test_a_cosmetic_edit_does_not_send_a_phone_notification():
    assert "duplicate" not in NOTIFY_STATUSES
    assert "ignored" not in NOTIFY_STATUSES


# --------------------------------------------------------------------------
# a real amendment: still a human's decision
# --------------------------------------------------------------------------


def test_a_moved_stop_after_an_open_intent_still_needs_a_human(tmp_path):
    ledger = SQLiteIntentLedger(tmp_path / "l.sqlite")
    accepted(ledger)

    edited = run(ledger, SELL.replace("Sl : 3925", "Sl : 3930"), revision=1)

    assert edited.status == "manual_review"
    assert "stop_loss" in edited.reason
    assert edited.order_intent is None


def test_a_changed_target_after_an_open_intent_still_needs_a_human(tmp_path):
    ledger = SQLiteIntentLedger(tmp_path / "l.sqlite")
    accepted(ledger)

    edited = run(ledger, SELL.replace("Tp : 3900", "Tp : 3895"), revision=1)

    assert edited.status == "manual_review"
    assert "take_profits" in edited.reason


def test_a_direction_flip_after_an_open_intent_still_needs_a_human(tmp_path):
    ledger = SQLiteIntentLedger(tmp_path / "l.sqlite")
    accepted(ledger)

    edited = run(ledger, BUY, revision=1)

    assert edited.status == "manual_review"
    assert "direction" in edited.reason


def test_an_unreadable_earlier_revision_goes_to_a_human():
    """Unknown is not harmless. The old behaviour is kept for this case."""
    result = evaluate_message(envelope(SELL_TICKED, revision=1),
                              instrument=INSTRUMENT, risk=RISK, quote=QUOTE,
                              previous=None)

    assert result.status == "manual_review"
    assert "could not be read" in result.reason


class ExplodingLedger(SQLiteIntentLedger):
    """A ledger that can store but cannot answer questions about the past."""

    def previous_revision(self, channel, message_id):
        raise RuntimeError("database is down")


def test_a_ledger_that_cannot_answer_does_not_fail_open(tmp_path):
    ledger = ExplodingLedger(tmp_path / "l.sqlite")
    accepted(ledger)

    edited = run(ledger, SELL_TICKED, revision=1)

    assert edited.status == "manual_review"
    assert edited.order_intent is None


# --------------------------------------------------------------------------
# nothing to protect: the edit is the best version of the call
# --------------------------------------------------------------------------


def test_an_edit_after_a_blocked_original_is_evaluated_normally(tmp_path):
    """The original never opened anything, so refusing the edit protects nothing.

    This is the case that made the old rule cost trades: a signal blocked on a
    bad quote, then amended two seconds later, was filed for review instead of
    being read as the trader's final instruction.
    """
    ledger = SQLiteIntentLedger(tmp_path / "l.sqlite")
    blocked = run(ledger, quote=None)
    assert blocked.status == "blocked"

    edited = run(ledger, SELL_TICKED, revision=1)

    assert edited.status == "paper_intent"
    assert edited.order_intent is not None
    assert edited.signal["supersedes_revision"] == 1


def test_an_edit_after_an_ignored_message_is_evaluated_normally(tmp_path):
    ledger = SQLiteIntentLedger(tmp_path / "l.sqlite")
    ignored = run(ledger, "good morning all")
    assert ignored.status == "ignored"

    edited = run(ledger, SELL_TICKED, revision=1)

    assert edited.status == "paper_intent"


def test_re_evaluating_an_edit_still_applies_the_age_limit(tmp_path):
    """Relaxing the edit rule must not relax the age limit.

    The edit is read as a fresh signal, so it is also judged as one: a call
    posted half an hour ago is stale however it is worded.
    """
    ledger = SQLiteIntentLedger(tmp_path / "l.sqlite")
    run(ledger, quote=None)

    stale = process_once(
        envelope(SELL_TICKED, revision=1, posted_at=NOW - timedelta(minutes=30)),
        ledger=ledger, instrument=INSTRUMENT, risk=RISK, quote=QUOTE)

    assert stale.status == "stale"
    assert stale.order_intent is None


# --------------------------------------------------------------------------
# the guards that must not move: a normal signal is still accepted
# --------------------------------------------------------------------------


def test_a_first_sighting_is_still_accepted(tmp_path):
    ledger = SQLiteIntentLedger(tmp_path / "l.sqlite")

    result = run(ledger)

    assert result.status == "paper_intent"
    assert result.order_intent["size_lots"] == 0.05


def test_a_first_sighting_that_is_already_edited_is_still_accepted():
    """Catch-up reads the amended body as revision 0; it is traded as it stands."""
    already_edited = MessageEnvelope("@source", 100, 0, NOW, NOW, SELL_TICKED,
                                     edited_at=NOW + timedelta(seconds=2))

    result = evaluate_message(already_edited, instrument=INSTRUMENT, risk=RISK,
                              quote=QUOTE)

    assert result.status == "paper_intent"
    assert result.signal["first_seen_edited"] is True


# --------------------------------------------------------------------------
# the ledger read the rule depends on
# --------------------------------------------------------------------------


def test_previous_revision_is_none_before_anything_is_stored(tmp_path):
    ledger = SQLiteIntentLedger(tmp_path / "l.sqlite")
    assert ledger.previous_revision("@source", 100) is None


def test_previous_revision_returns_the_status_and_the_parsed_signal(tmp_path):
    ledger = SQLiteIntentLedger(tmp_path / "l.sqlite")
    accepted(ledger)

    previous = ledger.previous_revision("@source", 100)

    assert previous["status"] == "paper_intent"
    # The recorded signal uses the intent vocabulary: side / published_entry.
    assert previous["signal"]["side"] == "sell"
    assert previous["signal"]["published_entry"] == 3915
    assert previous["signal"]["stop_loss"] == 3925
