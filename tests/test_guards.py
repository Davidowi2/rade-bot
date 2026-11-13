"""Tests for the pre-trade risk controls.

Two things are being proved here, and they are different:

* the arithmetic of each guard, as pure functions, so a limit that is off by
  a factor of two cannot hide behind a test that only checks "blocked";
* that the engine actually consults them at the two places that matter — before
  an entry, and before every management pass — including the cases where the
  inputs cannot be read at all, because a guard that fails open is worse than
  no guard.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from rade.execution.engine import STATE_BLOCKED, ManagedTrade, trade_id_for
from rade.execution.guards import (
    GuardError,
    RiskLimits,
    RiskSnapshot,
    RiskState,
    evaluate,
    utc_day,
)

from tests.test_execution import (  # noqa: F401 - fixtures live with the engine tests
    FakeBroker,
    engine_for,
    envelope,
    evaluated,
)


def snapshot(**overrides) -> RiskSnapshot:
    base = dict(equity=10_000.0, day_start_equity=10_000.0, peak_equity=10_000.0,
                open_positions=0, spread=0.2, slippage=0.5)
    base.update(overrides)
    return RiskSnapshot(**base)


def sell(entry: float, *, stop: float | None = None,
         targets: list[float] | None = None) -> str:
    """A distinct SELL call, so the duplicate guard is not what is being tested."""
    stop = stop if stop is not None else round(entry + 10, 2)
    targets = targets or [round(entry - 5, 2), round(entry - 10, 2),
                          round(entry - 15, 2)]
    lines = [f"XAUUSD SELL NOW {entry:.2f}", f"Sl : {stop:.2f}"]
    lines.extend(f"Tp : {t:.2f}" for t in targets)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# the guards as arithmetic
# ---------------------------------------------------------------------------


def test_a_clean_account_passes_every_check():
    verdict = evaluate(RiskLimits(), snapshot())
    assert verdict.allowed is True
    assert {check["check"] for check in verdict.checks} >= {
        "pause", "equity", "max_drawdown", "daily_loss",
        "max_open_positions", "max_spread", "max_slippage",
    }


def test_the_daily_loss_breaker_blocks_at_the_limit_and_names_the_floor():
    limits = RiskLimits(daily_loss_fraction=0.02)
    # 2% of 10,000 is 200, so 9,800 is the floor. Exactly at it is blocked:
    # the limit is a ceiling on loss, not a target to reach.
    assert evaluate(limits, snapshot(equity=9_801.0)).allowed is True
    verdict = evaluate(limits, snapshot(equity=9_800.0))
    assert verdict.allowed is False
    assert "day's loss" in verdict.reason
    assert "9800" in verdict.reason


def test_the_drawdown_stop_uses_the_peak_not_the_day():
    limits = RiskLimits(max_drawdown_fraction=0.05, daily_loss_fraction=0.0)
    # The peak was 12,000, so the floor is 11,400 even though the day opened
    # at 11,900 and the daily breaker would allow trading.
    verdict = evaluate(limits, snapshot(equity=11_300.0, peak_equity=12_000.0,
                                        day_start_equity=11_900.0))
    assert verdict.allowed is False
    assert "drawdown floor" in verdict.reason


def test_the_open_position_limit_counts_before_it_places():
    limits = RiskLimits(max_open_positions=2)
    assert evaluate(limits, snapshot(open_positions=1)).allowed is True
    verdict = evaluate(limits, snapshot(open_positions=2))
    assert verdict.allowed is False
    assert "limit is 2" in verdict.reason


def test_a_wide_spread_blocks_before_the_price_is_used():
    limits = RiskLimits(max_spread=1.0)
    assert evaluate(limits, snapshot(spread=1.0)).allowed is True
    verdict = evaluate(limits, snapshot(spread=1.5))
    assert verdict.allowed is False
    assert "spread" in verdict.reason


def test_slippage_is_measured_from_the_published_entry():
    limits = RiskLimits(max_slippage=3.0)
    assert evaluate(limits, snapshot(slippage=3.0)).allowed is True
    verdict = evaluate(limits, snapshot(slippage=3.1))
    assert verdict.allowed is False
    assert "published entry" in verdict.reason


def test_the_drawdown_stop_is_reported_before_the_daily_breaker():
    """The more serious reason is the one recorded."""
    limits = RiskLimits(daily_loss_fraction=0.01, max_drawdown_fraction=0.02)
    verdict = evaluate(limits, snapshot(equity=9_000.0))
    assert verdict.allowed is False
    assert "drawdown floor" in verdict.reason


def test_an_unreadable_pause_switch_blocks_everything():
    verdict = evaluate(RiskLimits(), snapshot(paused=None))
    assert verdict.allowed is False
    assert "could not be read" in verdict.reason


def test_a_paused_account_blocks_and_says_why():
    verdict = evaluate(RiskLimits(), snapshot(paused=True,
                                              pause_reason="manual intervention"))
    assert verdict.allowed is False
    assert "manual intervention" in verdict.reason


def test_unusable_equity_blocks_because_no_size_can_be_computed():
    verdict = evaluate(RiskLimits(), snapshot(equity=0.0))
    assert verdict.allowed is False
    assert "equity is not usable" in verdict.reason


def test_a_disabled_check_is_reported_as_disabled_not_as_passing():
    described = RiskLimits(max_spread=0.0, max_slippage=0.0).describe()
    assert set(described["disabled"]) == {"max_spread", "max_slippage"}
    assert described["max_open_positions"] == 3


def test_limits_come_from_the_environment_and_reject_nonsense():
    limits = RiskLimits.from_env({"MIRROR_MAX_OPEN_POSITIONS": "1",
                                  "MIRROR_DAILY_LOSS_FRACTION": "0.01"})
    assert limits.max_open_positions == 1
    assert limits.daily_loss_fraction == pytest.approx(0.01)
    with pytest.raises(GuardError):
        RiskLimits.from_env({"MIRROR_MAX_SPREAD": "wide"})
    with pytest.raises(GuardError):
        RiskLimits.from_env({"MIRROR_MAX_OPEN_POSITIONS": "-1"})


# ---------------------------------------------------------------------------
# the state that has to survive a restart
# ---------------------------------------------------------------------------


def test_the_day_rolls_over_at_utc_midnight_and_keeps_the_peak():
    state = RiskState(day="2026-11-12", day_start_equity=10_000.0,
                      peak_equity=10_500.0)
    rolled = state.rolled(9_900.0, now=datetime(2026, 11, 13, 0, 0,
                                                tzinfo=timezone.utc))
    assert rolled.day == "2026-11-13"
    assert rolled.day_start_equity == pytest.approx(9_900.0), "a new day opens here"
    assert rolled.peak_equity == pytest.approx(10_500.0), "the peak is history"


def test_within_a_day_the_opening_equity_is_kept():
    state = RiskState(day=utc_day(), day_start_equity=10_000.0, peak_equity=10_000.0)
    rolled = state.rolled(10_400.0)
    assert rolled.day_start_equity == pytest.approx(10_000.0)
    assert rolled.peak_equity == pytest.approx(10_400.0)


# ---------------------------------------------------------------------------
# the engine actually consults them
# ---------------------------------------------------------------------------


def test_a_breaker_trips_before_the_order_is_sent(tmp_path):
    broker = FakeBroker()
    engine, ledger, _ = engine_for(tmp_path, broker)
    engine.limits = RiskLimits(daily_loss_fraction=0.02)

    # The first trade opens the day at 10,000.
    first = evaluated(sell(3915), mid=100)
    engine.execute(first, envelope(sell(3915), mid=100))
    assert broker.place_calls, "the first order should go out"

    # The account drops past the 2% floor, then a second, different signal
    # arrives.
    broker.account_equity = 9_700.0
    second = evaluated(sell(3916), mid=101)
    outcome = engine.execute(second, envelope(sell(3916), mid=101))

    assert outcome.get("executed") is False
    assert outcome.get("guard") is True
    assert len(broker.place_calls) == 1, "no second order was sent"
    trade = ManagedTrade.from_row(ledger.get_trade(trade_id_for(second.event_key)))
    assert trade.state == STATE_BLOCKED
    assert "day's loss" in trade.note


def test_the_equity_is_re_read_for_every_order(tmp_path):
    """A process up for days must not size against its startup balance.

    Sizing happens in the pipeline, so the refresh lives in ``RiskConfig``:
    the provider is called per signal, and a provider that cannot answer
    blocks the signal rather than falling back to the startup number.
    """
    from rade.observer.mirror_pipeline import (
        InstrumentSpec,
        MarketQuote,
        MessageEnvelope,
        RiskConfig,
        evaluate_message,
    )

    instrument = InstrumentSpec("XAUUSD", 100, 0.01, 0.01, 50, units_confirmed=True)
    balance = {"equity": 10_000.0}
    risk = RiskConfig(10_000.0, 0.005, 2.0,
                      equity_provider=lambda: balance["equity"])

    def size(mid: int) -> float:
        message = MessageEnvelope("@source", mid, 0, datetime(2026, 11, 13, 12, 0,
                                                             tzinfo=timezone.utc),
                                  datetime(2026, 11, 13, 12, 0, tzinfo=timezone.utc),
                                  "XAUUSD SELL NOW 3915\nSl : 3925\nTp : 3910")
        result = evaluate_message(message, instrument=instrument, risk=risk,
                                  quote=MarketQuote(3915.0, 3915.0,
                                                    datetime(2026, 11, 13, 12, 0,
                                                             tzinfo=timezone.utc)))
        assert result.status == "paper_intent", result.reason
        return float(result.order_intent["size_lots"])

    small = size(200)
    balance["equity"] = 20_000.0
    large = size(201)
    assert large == pytest.approx(small * 2, rel=0.02)


def test_a_failing_equity_provider_blocks_instead_of_using_the_startup_number():
    from rade.observer.mirror_pipeline import (
        InstrumentSpec,
        MarketQuote,
        MessageEnvelope,
        RiskConfig,
        evaluate_message,
    )

    def broken() -> float:
        raise RuntimeError("the account state is unavailable")

    message = MessageEnvelope("@source", 202, 0,
                              datetime(2026, 11, 13, 12, 0, tzinfo=timezone.utc),
                              datetime(2026, 11, 13, 12, 0, tzinfo=timezone.utc),
                              "XAUUSD SELL NOW 3915\nSl : 3925\nTp : 3910")
    result = evaluate_message(
        message,
        instrument=InstrumentSpec("XAUUSD", 100, 0.01, 0.01, 50, units_confirmed=True),
        risk=RiskConfig(10_000.0, 0.005, 2.0, equity_provider=broken),
        quote=MarketQuote(3915.0, 3915.0,
                          datetime(2026, 11, 13, 12, 0, tzinfo=timezone.utc)),
    )
    assert result.status == "blocked"
    assert "could not be read at sizing time" in result.reason


def test_the_open_position_limit_stops_the_next_signal(tmp_path):
    broker = FakeBroker()
    engine, ledger, _ = engine_for(tmp_path, broker)
    engine.limits = RiskLimits(max_open_positions=1)

    first = evaluated(sell(3915), mid=300)
    engine.execute(first, envelope(sell(3915), mid=300))
    assert broker.place_calls, "the first order fits"

    second = evaluated(sell(3917), mid=301)
    outcome = engine.execute(second, envelope(sell(3917), mid=301))
    assert outcome.get("guard") is True
    assert "already open" in outcome["reason"]
    assert len(broker.place_calls) == 1


def test_an_unreadable_equity_blocks_the_order(tmp_path):
    broker = FakeBroker()
    engine, ledger, _ = engine_for(tmp_path, broker)
    broker.fail_equity = True

    result = evaluated(mid=400)
    outcome = engine.execute(result, envelope(mid=400))

    assert outcome.get("guard") is True
    assert "equity could not be read" in outcome["reason"]
    assert broker.place_calls == []
    trade = ManagedTrade.from_row(ledger.get_trade(trade_id_for(result.event_key)))
    assert trade.state == STATE_BLOCKED


def test_a_wide_spread_blocks_the_order(tmp_path):
    broker = FakeBroker(bid=3915.0, ask=3918.0)   # 3.0 wide
    engine, ledger, _ = engine_for(tmp_path, broker)
    engine.limits = RiskLimits(max_spread=1.0, max_slippage=100.0)

    result = evaluated(mid=500)
    outcome = engine.execute(result, envelope(mid=500))
    assert outcome.get("guard") is True
    assert "spread" in outcome["reason"]
    assert broker.place_calls == []


def test_a_pause_stops_new_orders(tmp_path):
    broker = FakeBroker()
    engine, ledger, _ = engine_for(tmp_path, broker)
    engine.pause(reason="operator stepped away", changed_by="test")

    result = evaluated(mid=600)
    outcome = engine.execute(result, envelope(mid=600))
    assert outcome.get("guard") is True
    assert "paused" in outcome["reason"]
    assert broker.place_calls == []


def test_a_pause_stops_management_but_leaves_the_broker_stop_attached(tmp_path):
    broker = FakeBroker()
    engine, ledger, _ = engine_for(tmp_path, broker)
    result = evaluated(mid=700)
    engine.execute(result, envelope(mid=700))
    trade_id = trade_id_for(result.event_key)
    engine.confirm_fill(ManagedTrade.from_row(ledger.get_trade(trade_id)))

    engine.pause(reason="manual", changed_by="test")
    broker.bid, broker.ask = 3909.8, 3910.0
    outcome = engine.manage_trade(ManagedTrade.from_row(ledger.get_trade(trade_id)))

    assert outcome.get("paused") is True
    assert broker.close_calls == [], "no leg ran while paused"
    assert broker.modify_calls == [], "no stop moved while paused"
    # The position still carries the stop that was attached at entry.
    position = broker.position_book[0]
    assert engine._attached_levels(position).stop == pytest.approx(3925.0)


def test_the_pause_switch_is_a_row_that_survives_a_new_engine(tmp_path):
    """The switch is the stored row, not a flag in the process."""
    broker = FakeBroker()
    engine, ledger, _ = engine_for(tmp_path, broker)
    engine.pause(reason="storage test", changed_by="test")

    # A new engine over the same ledger is what a restart produces.
    from rade.execution.engine import ExecutionEngine
    restarted = ExecutionEngine(broker=FakeBroker(), ledger=ledger,
                                log=lambda line: None)
    state = restarted.pause_state()
    assert state["paused"] is True
    assert state["reason"] == "storage test"

    restarted.resume(reason="clear", changed_by="test")
    assert restarted.pause_state()["paused"] is False


def test_the_guard_state_reports_what_is_enforced(tmp_path):
    broker = FakeBroker()
    engine, ledger, _ = engine_for(tmp_path, broker)
    engine.limits = RiskLimits(max_spread=0.0)
    described = engine.guard_state()
    assert described["limits"]["disabled"] == ["max_spread"]
    assert described["limits"]["max_open_positions"] == 3


# ---------------------------------------------------------------------------
# the same call posted twice
# ---------------------------------------------------------------------------

#: Taken verbatim from the channel: messages 18788 and 18789 carry identical
#: text and identical timestamps, because the channel re-posts its calls. The
#: event key includes the message id, so the store's unique constraint cannot
#: see that these are one idea.
DOUBLE_POST = "XAUUSD BUY NOW 3909\ufe0f\nSl : 3900\nTp : 3915\nTp : 3920\nTp : 3925"


def test_the_same_call_posted_twice_is_copied_once(tmp_path):
    # The quote is placed where this signal was published, so the slippage
    # guard passes and this test is about duplication only.
    broker = FakeBroker(bid=3909.0, ask=3909.2)
    engine, ledger, _ = engine_for(tmp_path, broker)
    quote = (3909.0, 3909.2)
    first = evaluated(DOUBLE_POST, mid=880, quote=quote)
    engine.execute(first, envelope(DOUBLE_POST, mid=880))
    assert len(broker.place_calls) == 1

    # The channel posts the identical call again under a new message id.
    second = evaluated(DOUBLE_POST, mid=889, quote=quote)
    outcome = engine.execute(second, envelope(DOUBLE_POST, mid=889))

    assert outcome.get("executed") is False
    assert len(broker.place_calls) == 1, "the second post must not place an order"
    trade = ManagedTrade.from_row(ledger.get_trade(trade_id_for(second.event_key)))
    assert trade.state == "not_copied"
    assert "posted again" in trade.note


def test_a_different_call_is_still_copied(tmp_path):
    """The duplicate check is about identical text, not about frequency."""
    broker = FakeBroker()
    engine, ledger, _ = engine_for(tmp_path, broker)
    engine.execute(evaluated(sell(3915), mid=890), envelope(sell(3915), mid=890))
    other = sell(3918)
    outcome = engine.execute(evaluated(other, mid=891), envelope(other, mid=891))
    assert outcome.get("executed") is True
    assert len(broker.place_calls) == 2


def test_a_failed_duplicate_lookup_blocks_rather_than_guesses(tmp_path):
    broker = FakeBroker()
    engine, ledger, _ = engine_for(tmp_path, broker)

    def explode(*args, **kwargs):
        raise RuntimeError("the store is unavailable")

    ledger.same_text_recently = explode
    outcome = engine.execute(evaluated(mid=892), envelope(mid=892))
    assert outcome.get("executed") is False
    assert broker.place_calls == []
