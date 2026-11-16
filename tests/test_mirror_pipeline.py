"""Safety checks for the Telegram-to-paper-intent pipeline."""
from datetime import datetime, timedelta, timezone

from rade.observer.mirror_pipeline import (
    InstrumentSpec,
    MarketQuote,
    MessageEnvelope,
    RiskConfig,
    SQLiteIntentLedger,
    evaluate_message,
    process_once,
    text_hash,
)


NOW = datetime(2026, 11, 12, 12, 0, tzinfo=timezone.utc)
INSTRUMENT = InstrumentSpec("XAUUSD", 100, 0.01, 0.01, 50, units_confirmed=True)
RISK = RiskConfig(10_000, 0.005, 2.0)
VALID = "XAUUSD SELL NOW 3915\nSl : 3925\nTp : 3910\nTp : 3905\nTp : 3900"


def message(text=VALID, *, revision=0, mid=100):
    return MessageEnvelope("@source", mid, revision, NOW, NOW, text)


def test_market_signal_generates_only_a_paper_intent():
    result = evaluate_message(
        message(), instrument=INSTRUMENT, risk=RISK,
        quote=MarketQuote(3915, 3915, NOW),
    )
    assert result.status == "paper_intent"
    assert result.order_intent["broker_write"] is False
    assert result.order_intent["type"] == "market"
    assert result.order_intent["size_lots"] == 0.05
    assert result.order_intent["risk_budget"] == 50
    assert "no broker order sent" in result.notification_preview


def test_no_quote_blocks_now_signal():
    result = evaluate_message(message(), instrument=INSTRUMENT, risk=RISK)
    assert result.status == "blocked"
    assert result.order_intent is None


def test_non_signal_or_missing_instrument_is_ignored():
    result = evaluate_message(
        message("BUY NOW 3915\nSL 3925\nTP 3900"),
        instrument=INSTRUMENT, risk=RISK,
    )
    assert result.status == "ignored"
    assert result.order_intent is None


def test_wrong_side_stop_or_target_requires_review():
    bad_stop = evaluate_message(
        message(VALID.replace("Sl : 3925", "Sl : 3900")),
        instrument=INSTRUMENT, risk=RISK,
        quote=MarketQuote(3915, 3915, NOW),
    )
    bad_tp = evaluate_message(
        message(VALID.replace("Tp : 3910", "Tp : 3920")),
        instrument=INSTRUMENT, risk=RISK,
        quote=MarketQuote(3915, 3915, NOW),
    )
    assert bad_stop.status == bad_tp.status == "manual_review"
    assert bad_stop.order_intent is bad_tp.order_intent is None


def test_stale_quote_fails_closed():
    old = datetime(2026, 11, 12, 11, 59, 50, tzinfo=timezone.utc)
    result = evaluate_message(
        message(), instrument=INSTRUMENT, risk=RISK,
        quote=MarketQuote(3915, 3915, old),
    )
    assert result.status == "blocked"
    assert "stale" in result.reason


def test_limit_signal_does_not_need_quote():
    text = "XAUUSD BUY LIMIT 3900\nSl : 3890\nTp : 3910\nTp : 3920"
    result = evaluate_message(message(text), instrument=INSTRUMENT, risk=RISK)
    assert result.status == "paper_intent"
    assert result.order_intent["type"] == "limit"
    assert result.order_intent["price"] == 3900
    assert result.order_intent["size_lots"] == 0.05


def test_minimum_lot_that_breaks_risk_cap_is_skipped():
    tiny = RiskConfig(equity=100, risk_fraction=0.005, quote_max_age_seconds=2)
    result = evaluate_message(
        message(), instrument=INSTRUMENT, risk=tiny,
        quote=MarketQuote(3915, 3915, NOW),
    )
    assert result.status == "skipped_minimum_lot"
    assert result.order_intent is None


def test_edits_are_never_automatically_ordered():
    result = evaluate_message(
        message(revision=1), instrument=INSTRUMENT, risk=RISK,
        quote=MarketQuote(3915, 3915, NOW),
    )
    assert result.status == "manual_review"
    assert result.order_intent is None


# --------------------------------------------------------------------------
# an edit seen for the first time: the revision counter only rises when the
# pipeline saw the original, so catch-up and restart deliver an edited body
# as revision 0. Trusting the revision alone copies the edited call.
# --------------------------------------------------------------------------


def _edited(*, posted_ago=timedelta(0), edit_after=timedelta(minutes=1)):
    return MessageEnvelope(
        "@source", 100, 0, NOW - posted_ago, NOW, VALID,
        edited_at=NOW - posted_ago + edit_after)


def test_an_edit_seen_for_the_first_time_is_not_copied():
    result = evaluate_message(
        _edited(), instrument=INSTRUMENT, risk=RISK,
        quote=MarketQuote(3915, 3915, NOW),
    )
    assert result.status == "manual_review"
    assert result.order_intent is None
    assert "first seen in its edited form" in result.reason
    assert "original call is unknown" in result.reason


def test_a_first_sighting_edit_is_refused_as_an_edit_not_as_stale():
    """The edit is the stronger fact: a fresher copy would still be an edit."""
    result = evaluate_message(
        _edited(posted_ago=timedelta(hours=3), edit_after=timedelta(minutes=2)),
        instrument=INSTRUMENT, risk=RISK,
        quote=MarketQuote(3915, 3915, NOW),
    )
    assert result.status == "manual_review"
    assert "edited" in result.reason
    assert result.signal is not None


def test_a_first_sighting_edit_is_recorded_without_an_intent(tmp_path):
    ledger = SQLiteIntentLedger(tmp_path / "observer.sqlite")
    result = process_once(
        _edited(), ledger=ledger, instrument=INSTRUMENT, risk=RISK,
        quote=MarketQuote(3915, 3915, NOW),
    )
    assert result.status == "manual_review"
    assert result.order_intent is None
    assert ledger.list_trades() == []


def test_the_adapter_carries_the_edit_timestamp():
    """Without this the guard has nothing to see: the envelope is the only
    place the pipeline learns that the body changed."""
    from rade.observer.mirror_pipeline import envelope_from_telegram

    class _Msg:
        id = 7
        date = NOW
        message = VALID
        edit_date = NOW + timedelta(seconds=30)

    envelope = envelope_from_telegram(_Msg(), channel="@source")
    assert envelope.edited_at == NOW + timedelta(seconds=30)
    assert evaluate_message(
        envelope, instrument=INSTRUMENT, risk=RISK,
        quote=MarketQuote(3915, 3915, NOW),
    ).status == "manual_review"


def test_ledger_deduplicates_exact_message_revision(tmp_path):
    ledger = SQLiteIntentLedger(tmp_path / "observer.sqlite")
    first = process_once(
        message(), ledger=ledger, instrument=INSTRUMENT, risk=RISK,
        quote=MarketQuote(3915, 3915, NOW),
    )
    second = process_once(
        message(), ledger=ledger, instrument=INSTRUMENT, risk=RISK,
        quote=MarketQuote(3915, 3915, NOW),
    )
    assert first.status == "paper_intent"
    assert second.status == "duplicate"
    assert second.duplicate is True


def test_edited_revision_is_persisted_for_review_without_new_order(tmp_path):
    ledger = SQLiteIntentLedger(tmp_path / "observer.sqlite")
    original = process_once(
        message(), ledger=ledger, instrument=INSTRUMENT, risk=RISK,
        quote=MarketQuote(3915, 3915, NOW),
    )
    edited = process_once(
        message(VALID.replace("Tp : 3900", "Tp : 3895"), revision=1),
        ledger=ledger, instrument=INSTRUMENT, risk=RISK,
        quote=MarketQuote(3915, 3915, NOW),
    )
    assert original.status == "paper_intent"
    assert edited.status == "manual_review"
    assert edited.order_intent is None


# --------------------------------------------------------------------------
# quote freshness: the listener fetches the quote *after* receipt, so a
# negative age is the normal case and must not be read as a stale price
# --------------------------------------------------------------------------

def test_quote_fetched_after_receipt_is_fresh_not_stale():
    later = NOW + timedelta(milliseconds=300)
    result = evaluate_message(
        message(), instrument=INSTRUMENT, risk=RISK,
        quote=MarketQuote(3915.2, 3915.4, later),
    )
    assert result.status == "paper_intent", result.reason
    assert result.order_intent["quote"]["age_ms"] == -300.0


def test_quote_from_the_future_beyond_tolerance_is_blocked():
    far = NOW + timedelta(seconds=30)
    result = evaluate_message(
        message(), instrument=INSTRUMENT, risk=RISK,
        quote=MarketQuote(3915, 3915, far),
    )
    assert result.status == "blocked"
    assert "future" in result.reason


def test_inverted_quote_is_blocked():
    result = evaluate_message(
        message(), instrument=INSTRUMENT, risk=RISK,
        quote=MarketQuote(3916.0, 3915.0, NOW),
    )
    assert result.status == "blocked"
    assert "invalid" in result.reason


# --------------------------------------------------------------------------
# nothing is ever represented as filled
# --------------------------------------------------------------------------

def test_limit_call_is_pending_and_never_filled():
    text = "XAUUSD BUY LIMIT 3900\nSl : 3890\nTp : 3910\nTp : 3920"
    result = evaluate_message(message(text), instrument=INSTRUMENT, risk=RISK)
    assert result.status == "paper_intent"
    assert result.order_intent["order_state"] == "pending_limit"
    assert result.order_intent["filled"] is False


def test_market_call_is_also_never_filled():
    result = evaluate_message(
        message(), instrument=INSTRUMENT, risk=RISK,
        quote=MarketQuote(3915, 3915, NOW),
    )
    assert result.order_intent["order_state"] == "would_fill_market"
    assert result.order_intent["filled"] is False


# --------------------------------------------------------------------------
# ambiguity goes to a human, recap posts are ignored
# --------------------------------------------------------------------------

def test_two_entry_lines_are_reviewed_not_guessed():
    text = ("XAUUSD SELL NOW 3915\nSl : 3925\nTp : 3900\n\n"
            "XAUUSD SELL NOW 3918\nSl : 3928\nTp : 3905")
    result = evaluate_message(message(text), instrument=INSTRUMENT, risk=RISK,
                              quote=MarketQuote(3915, 3915, NOW))
    assert result.status == "manual_review"
    assert "entry lines" in result.reason
    assert result.order_intent is None


def test_a_target_line_with_two_numbers_is_reviewed():
    text = "XAUUSD SELL NOW 3915\nSl : 3925\nTp : 3910 / 3905"
    result = evaluate_message(message(text), instrument=INSTRUMENT, risk=RISK,
                              quote=MarketQuote(3915, 3915, NOW))
    assert result.status == "manual_review"
    assert "target line" in result.reason


def test_recap_without_an_entry_line_is_ignored():
    text = "XAUUSD BUY closed at TP2, +120 pips. Great week."
    result = evaluate_message(message(text), instrument=INSTRUMENT, risk=RISK)
    assert result.status == "ignored"
    assert result.order_intent is None


def test_ladder_is_normalised_to_nearest_first():
    text = "XAUUSD SELL NOW 3915\nSl : 3925\nTp : 3900\nTp : 3910\nTp : 3905"
    result = evaluate_message(message(text), instrument=INSTRUMENT, risk=RISK,
                              quote=MarketQuote(3915, 3915, NOW))
    assert result.status == "paper_intent"
    assert result.order_intent["take_profits"] == [3910, 3905, 3900]
    assert result.signal["ladder_normalised"] is True


# --------------------------------------------------------------------------
# fail closed on missing inputs
# --------------------------------------------------------------------------

def test_zero_equity_blocks():
    result = evaluate_message(message(), instrument=INSTRUMENT,
                              risk=RiskConfig(0, 0.005, 2),
                              quote=MarketQuote(3915, 3915, NOW))
    assert result.status == "blocked"
    assert "equity" in result.reason


def test_risk_above_the_cap_blocks_instead_of_being_clamped():
    result = evaluate_message(message(), instrument=INSTRUMENT,
                              risk=RiskConfig(10_000, 0.02, 2),
                              quote=MarketQuote(3915, 3915, NOW))
    assert result.status == "blocked"
    assert "risk" in result.reason


def test_incomplete_contract_spec_blocks():
    bad = InstrumentSpec("XAUUSD", 0, 0.01, 0.01, 50, units_confirmed=True)
    result = evaluate_message(message(), instrument=bad, risk=RISK,
                              quote=MarketQuote(3915, 3915, NOW))
    assert result.status == "blocked"
    assert "contract" in result.reason


def test_unconfirmed_contract_units_are_not_used_to_size():
    """A plausible number nobody checked must not produce a size."""
    unconfirmed = InstrumentSpec("XAUUSD", 100, 0.01, 0.01, 50)
    result = evaluate_message(message(), instrument=unconfirmed, risk=RISK,
                              quote=MarketQuote(3915, 3915, NOW))
    assert result.status == "blocked"
    assert "not confirmed" in result.reason
    assert result.order_intent is None


def test_a_limit_call_also_needs_a_confirmed_contract_size():
    """The unverified number was reachable from a limit call, so it is gated too."""
    limit_text = VALID.replace("NOW", "LIMIT", 1)
    unconfirmed = InstrumentSpec("XAUUSD", 100, 0.01, 0.01, 50)
    result = evaluate_message(message(limit_text), instrument=unconfirmed, risk=RISK)
    assert result.status == "blocked"
    assert "not confirmed" in result.reason
    assert result.order_intent is None


def test_instrument_mismatch_is_reviewed():
    other = InstrumentSpec("XAGUSD", 100, 0.01, 0.01, 50, units_confirmed=True)
    result = evaluate_message(message(), instrument=other, risk=RISK,
                              quote=MarketQuote(3915, 3915, NOW))
    assert result.status == "manual_review"
    assert "instrument" in result.reason


# --------------------------------------------------------------------------
# idempotency: restarts, conflicts
# --------------------------------------------------------------------------

def test_a_restart_does_not_reissue_the_same_intent(tmp_path):
    path = tmp_path / "observer.sqlite"
    first = process_once(message(), ledger=SQLiteIntentLedger(path),
                         instrument=INSTRUMENT, risk=RISK,
                         quote=MarketQuote(3915, 3915, NOW))
    # a new process, same database
    second = process_once(message(), ledger=SQLiteIntentLedger(path),
                          instrument=INSTRUMENT, risk=RISK,
                          quote=MarketQuote(3915, 3915, NOW))
    assert first.status == "paper_intent"
    assert second.status == "duplicate"
    assert second.duplicate is True


def test_same_revision_with_a_changed_body_is_a_conflict_not_an_update(tmp_path):
    ledger = SQLiteIntentLedger(tmp_path / "observer.sqlite")
    first = process_once(message(), ledger=ledger, instrument=INSTRUMENT, risk=RISK,
                         quote=MarketQuote(3915, 3915, NOW))
    tampered = process_once(
        message(VALID.replace("Sl : 3925", "Sl : 3920")), ledger=ledger,
        instrument=INSTRUMENT, risk=RISK, quote=MarketQuote(3915, 3915, NOW),
    )
    assert first.status == "paper_intent"
    assert tampered.status == "conflict"
    assert tampered.order_intent is None
    assert tampered.requires_review is True
    assert ledger.revision_for("@source", 100) == 0


def test_revision_for_reports_the_highest_stored_revision(tmp_path):
    ledger = SQLiteIntentLedger(tmp_path / "observer.sqlite")
    assert ledger.revision_for("@source", 100) == 0
    process_once(message(), ledger=ledger, instrument=INSTRUMENT, risk=RISK,
                 quote=MarketQuote(3915, 3915, NOW))
    process_once(message(VALID, revision=1), ledger=ledger, instrument=INSTRUMENT,
                 risk=RISK, quote=MarketQuote(3915, 3915, NOW))
    assert ledger.revision_for("@source", 100) == 1


def test_stored_text_hash_reports_the_latest_revision(tmp_path):
    """The edit handler compares against this, so it must be the newest text."""
    ledger = SQLiteIntentLedger(tmp_path / "observer.sqlite")
    assert ledger.stored_text_hash("@source", 100) is None
    process_once(message(), ledger=ledger, instrument=INSTRUMENT, risk=RISK,
                 quote=MarketQuote(3915, 3915, NOW))
    assert ledger.stored_text_hash("@source", 100) == text_hash(VALID)
    edited = VALID.replace("Tp : 3900", "Tp : 3895")
    process_once(message(edited, revision=1), ledger=ledger, instrument=INSTRUMENT,
                 risk=RISK, quote=MarketQuote(3915, 3915, NOW))
    assert ledger.stored_text_hash("@source", 100) == text_hash(edited)


def test_counts_group_by_disposition(tmp_path):
    ledger = SQLiteIntentLedger(tmp_path / "observer.sqlite")
    process_once(message(), ledger=ledger, instrument=INSTRUMENT, risk=RISK,
                 quote=MarketQuote(3915, 3915, NOW))
    process_once(message("hello", mid=101), ledger=ledger, instrument=INSTRUMENT, risk=RISK)
    assert ledger.counts() == {"paper_intent": 1, "ignored": 1}


# ---------------------------------------------------------------------------
# fixed-lot sizing (the confirmed rule)
# ---------------------------------------------------------------------------


def fixed_risk(*, lots=0.03, ceiling=50.0, fraction=0.005):
    return RiskConfig(10_000, fraction, 2.0, fixed_lots=lots,
                      max_trade_risk_usd=ceiling)


def fixed_evaluated(*, text=VALID, risk=None):
    return evaluate_message(
        message(text), instrument=INSTRUMENT, risk=risk or fixed_risk(),
        quote=MarketQuote(3915, 3915, NOW),
    )


def test_a_fixed_lot_is_the_size_and_not_a_risk_fraction_of_equity():
    result = fixed_evaluated()
    assert result.status == "paper_intent"
    assert result.order_intent["size_lots"] == 0.03
    assert result.order_intent["sizing_mode"] == "fixed_lots"
    assert result.order_intent["fixed_lots"] == 0.03


def test_the_intent_records_which_rule_produced_the_size():
    result = fixed_evaluated()
    intent = result.order_intent
    assert intent["sizing_mode"] == "fixed_lots"
    assert intent["max_trade_risk_usd"] == 50.0
    # The fraction is inert in this mode, and the intent says so by recording
    # the mode rather than leaving a reader to assume it was used.
    assert intent["risk_fraction"] == 0.005


def test_a_missing_ceiling_blocks_the_signal_and_says_so():
    result = fixed_evaluated(risk=fixed_risk(ceiling=None))
    assert result.status == "blocked"
    assert result.order_intent is None
    assert "no per-trade USD risk ceiling" in result.reason


def test_stop_risk_above_the_ceiling_blocks_the_signal():
    # 0.03 x 100 units x 10 price units = $30 against a $25 ceiling.
    result = fixed_evaluated(risk=fixed_risk(ceiling=25.0))
    assert result.status == "blocked"
    assert result.order_intent is None
    assert "$30.00" in result.reason


def test_a_fixed_lot_below_the_broker_minimum_is_blocked_not_skipped():
    instrument = InstrumentSpec("XAUUSD", 100, 0.01, 0.05, 50, units_confirmed=True)
    result = evaluate_message(message(), instrument=instrument,
                              risk=fixed_risk(), quote=MarketQuote(3915, 3915, NOW))
    assert result.status == "blocked", "a refusal, not a quiet skip"
    assert result.order_intent is None
    assert "below the broker's minimum" in result.reason


def test_a_fixed_lot_above_the_broker_maximum_is_blocked_not_clamped():
    instrument = InstrumentSpec("XAUUSD", 100, 0.01, 0.01, 0.02, units_confirmed=True)
    result = evaluate_message(message(), instrument=instrument,
                              risk=fixed_risk(), quote=MarketQuote(3915, 3915, NOW))
    assert result.status == "blocked"
    assert result.order_intent is None
    assert "above the broker's maximum" in result.reason


def test_a_fixed_lot_that_is_not_on_the_step_is_blocked():
    result = fixed_evaluated(risk=fixed_risk(lots=0.035))
    assert result.status == "blocked"
    assert result.order_intent is None
    assert "not representable" in result.reason


def test_the_fixed_lot_mode_does_not_validate_an_unused_risk_fraction():
    """A fraction that would block risk-fraction sizing must not block this."""
    result = fixed_evaluated(risk=fixed_risk(fraction=0.0))
    assert result.status == "paper_intent"
    assert result.order_intent["size_lots"] == 0.03


def test_an_entry_past_tp1_is_still_tradeable():
    """TP1 is no longer an exit, so a market beyond it is not a refusal."""
    result = evaluate_message(
        message(), instrument=INSTRUMENT, risk=fixed_risk(),
        quote=MarketQuote(3909, 3909, NOW),
    )
    assert result.status == "paper_intent"
    assert result.order_intent["size_lots"] == 0.03


def test_an_entry_already_past_the_final_target_is_sent_to_review():
    result = evaluate_message(
        message(), instrument=INSTRUMENT, risk=fixed_risk(),
        quote=MarketQuote(3899, 3899, NOW),
    )
    assert result.status == "manual_review"
    assert "final target" in result.reason
    assert result.order_intent is None
