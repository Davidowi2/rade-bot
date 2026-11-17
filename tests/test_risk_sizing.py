"""Fitting the order to the risk budget instead of refusing it.

The rule used to be "exactly the configured lot or no trade". It cost real
signals, because the channel posts a 10-point stop and by the time the message
is read the market has usually moved, widening the stop the order would actually
carry. At a fixed size the risk then exceeded the ceiling and the signal died -
even though a smaller size would have fitted comfortably.

The configured lot is now a **ceiling on size**. These tests pin the three
properties that make that safe:

* the size never risks more than the ceiling;
* the size never exceeds the configured maximum, however tight the stop;
* when even the broker's minimum lot breaks the ceiling, the signal is refused
  with the numbers, rather than being silently skipped or rounded up.
"""
from __future__ import annotations

import pytest

from rade.execution.sizing import LotSpec, fit_lot_to_risk, stop_risk_usd

XAUUSD = LotSpec(contract_units_per_lot=100, lot_step=0.01, min_lot=0.01,
                 max_lot=50.0, verified=True)


def test_a_stop_the_ceiling_cannot_afford_at_full_size_is_fitted_down():
    """16-point stop, $30 ceiling: 0.02 lots would risk $32, so it becomes 0.01."""
    lots, reason = fit_lot_to_risk(cap_lots=0.02, spec=XAUUSD,
                                   stop_distance=16.0, max_risk_usd=30.0)

    assert reason == ""
    assert lots == 0.01
    assert stop_risk_usd(lots=lots, spec=XAUUSD, stop_distance=16.0) <= 30.0


def test_the_size_never_risks_more_than_the_ceiling():
    for stop in (1.0, 2.5, 7.0, 10.0, 12.3, 19.33, 25.0, 30.0):
        lots, reason = fit_lot_to_risk(cap_lots=0.03, spec=XAUUSD,
                                       stop_distance=stop, max_risk_usd=30.0)
        assert reason == "", f"stop {stop} should still be affordable"
        risk = stop_risk_usd(lots=lots, spec=XAUUSD, stop_distance=stop)
        assert risk <= 30.0 + 1e-9, f"stop {stop} risks ${risk:.2f}"


def test_the_size_never_exceeds_the_configured_maximum():
    """A tight stop must not turn a small budget into a large position.

    1.5-point stop: $30 would buy 0.2 lots. The cap holds it at 0.02.
    """
    lots, reason = fit_lot_to_risk(cap_lots=0.02, spec=XAUUSD,
                                   stop_distance=1.5, max_risk_usd=30.0)

    assert reason == ""
    assert lots == 0.02, "the ceiling is a maximum, not a target"
    assert stop_risk_usd(lots=lots, spec=XAUUSD, stop_distance=1.5) == 3.0


def test_rounding_is_always_down():
    """0.0155 lots rounds to 0.01, never to 0.02 - up would cross the ceiling."""
    lots, _ = fit_lot_to_risk(cap_lots=0.03, spec=XAUUSD,
                              stop_distance=19.33, max_risk_usd=30.0)

    assert lots == 0.01
    assert stop_risk_usd(lots=lots, spec=XAUUSD, stop_distance=19.33) < 30.0


def test_a_stop_too_wide_for_even_the_minimum_lot_is_refused_with_the_numbers():
    """49.5-point stop: 0.01 lots risks $49.50, so there is no legal size."""
    lots, reason = fit_lot_to_risk(cap_lots=0.02, spec=XAUUSD,
                                   stop_distance=49.5, max_risk_usd=30.0)

    assert lots is None
    assert "smallest tradeable lot" in reason
    assert "49.50" in reason, "the reason must name the risk that failed"
    assert "30.00" in reason, "the reason must name the ceiling"


def test_the_result_is_always_on_the_brokers_lot_step():
    lots, _ = fit_lot_to_risk(cap_lots=0.05, spec=XAUUSD,
                              stop_distance=13.0, max_risk_usd=30.0)

    assert lots is not None
    assert abs((lots / XAUUSD.lot_step) - round(lots / XAUUSD.lot_step)) < 1e-9


def test_an_unverified_specification_refuses_rather_than_guessing():
    unverified = LotSpec(contract_units_per_lot=100, lot_step=0.01, min_lot=0.01,
                         max_lot=50.0, verified=False)

    lots, reason = fit_lot_to_risk(cap_lots=0.02, spec=unverified,
                                   stop_distance=10.0, max_risk_usd=30.0)

    assert lots is None
    assert "not verified" in reason


def test_a_missing_ceiling_refuses_rather_than_sizing_without_one():
    lots, reason = fit_lot_to_risk(cap_lots=0.02, spec=XAUUSD,
                                   stop_distance=10.0, max_risk_usd=None)

    assert lots is None
    assert "ceiling" in reason


def test_a_zero_stop_distance_is_refused():
    lots, reason = fit_lot_to_risk(cap_lots=0.02, spec=XAUUSD,
                                   stop_distance=0.0, max_risk_usd=30.0)

    assert lots is None
    assert "stop distance" in reason
