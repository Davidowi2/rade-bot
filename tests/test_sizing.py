"""The fixed-lot rule: a size is valid or the signal is refused.

The rule exists because every alternative is a size nobody chose. These tests
are written around that: each refusal must name the condition that failed, and
no path may return a number different from the one that was configured.
"""
import os
import subprocess
import sys
from pathlib import Path

import pytest

from rade.execution.sizing import LotSpec, check_fixed_lot, describe_fixed_lot

#: Gold on the demo account: 100 units per lot, 0.01 steps, 0.01 minimum.
GOLD = LotSpec(contract_units_per_lot=100.0, lot_step=0.01, min_lot=0.01,
               max_lot=50.0)


def check(**overrides):
    args = {"lots": 0.03, "spec": GOLD, "stop_distance": 5.0,
            "max_risk_usd": 50.0}
    args.update(overrides)
    return check_fixed_lot(**args)


def test_a_valid_lot_is_returned_unchanged():
    lots, reason = check()
    assert reason == ""
    assert lots == pytest.approx(0.03)


def test_the_risk_is_the_stop_distance_times_the_size():
    # 0.03 lot x 100 units x 10 price units = $30.
    lots, reason = check(stop_distance=10.0, max_risk_usd=30.0)
    assert reason == ""
    assert lots == pytest.approx(0.03)


def test_a_lot_that_is_not_on_the_broker_step_is_refused():
    lots, reason = check(lots=0.035)
    assert lots is None
    assert "not representable" in reason
    # The reason names the arithmetic, so it can be diagnosed from the ledger.
    assert "0.035" in reason


def test_a_lot_below_the_broker_minimum_is_refused():
    lots, reason = check(spec=LotSpec(100.0, 0.01, 0.05, 50.0))
    assert lots is None
    assert "below the broker's minimum" in reason


def test_a_lot_above_the_broker_maximum_is_refused_not_clamped():
    lots, reason = check(spec=LotSpec(100.0, 0.01, 0.01, 0.02))
    assert lots is None, "clamping would be a size nobody chose"
    assert "above the broker's maximum" in reason


def test_a_missing_ceiling_refuses_every_signal():
    lots, reason = check(max_risk_usd=None)
    assert lots is None
    assert "no per-trade USD risk ceiling is configured" in reason
    # A fixed lot without a ceiling is not a risk limit, so this is a refusal
    # and not a fallback to another sizing mode.
    assert "refused" in reason


def test_a_ceiling_that_is_not_positive_refuses_every_signal():
    for value in (0.0, -10.0):
        lots, reason = check(max_risk_usd=value)
        assert lots is None
        assert "must be a positive number" in reason


def test_stop_risk_above_the_ceiling_is_refused_not_resized():
    # 0.03 lot x 100 units x 10 = $30 against a $25 ceiling.
    lots, reason = check(stop_distance=10.0, max_risk_usd=25.0)
    assert lots is None, "the size must not be reduced to fit the ceiling"
    assert "$30.00" in reason and "$25.00" in reason


def test_stop_risk_exactly_at_the_ceiling_is_allowed():
    lots, reason = check(stop_distance=10.0, max_risk_usd=30.0)
    assert reason == ""
    assert lots == pytest.approx(0.03)


def test_an_unverified_specification_is_refused():
    lots, reason = check(spec=LotSpec(100.0, 0.01, 0.01, 50.0, verified=False))
    assert lots is None
    assert "not verified" in reason


def test_an_incomplete_specification_is_refused():
    lots, reason = check(spec=LotSpec(0.0, 0.01, 0.01, 50.0))
    assert lots is None
    assert "incomplete" in reason


def test_a_zero_lot_is_refused():
    lots, reason = check(lots=0.0)
    assert lots is None
    assert "not a usable number" in reason


def test_a_zero_stop_distance_is_refused():
    lots, reason = check(stop_distance=0.0)
    assert lots is None
    assert "stop distance" in reason


def test_the_rule_never_returns_a_different_size():
    """Whatever happens, the answer is the configured lot or nothing."""
    cases = [
        {"lots": 0.03, "stop_distance": 5.0, "max_risk_usd": 50.0},
        {"lots": 0.03, "stop_distance": 10.0, "max_risk_usd": 25.0},
        {"lots": 0.03, "stop_distance": 1.0, "max_risk_usd": None},
        {"lots": 0.03, "spec": LotSpec(100.0, 0.01, 0.05, 50.0)},
        {"lots": 0.03, "spec": LotSpec(100.0, 0.01, 0.01, 0.02)},
        {"lots": 0.037, "stop_distance": 5.0, "max_risk_usd": 50.0},
    ]
    for case in cases:
        lots, reason = check(**case)
        assert lots in (None, pytest.approx(0.03)), case
        assert (lots is None) == bool(reason), case


def test_describe_reports_risk_fraction_when_no_lot_is_configured():
    described = describe_fixed_lot(lots=None, max_risk_usd=None)
    assert described["mode"] == "risk_fraction"
    assert described["ready"] is True


def test_describe_reports_a_missing_ceiling_as_not_ready():
    described = describe_fixed_lot(lots=0.03, max_risk_usd=None)
    assert described["mode"] == "fixed_lots"
    assert described["ready"] is False
    assert "every signal will be refused" in described["problem"]


def test_describe_reports_a_configured_rule_as_ready():
    described = describe_fixed_lot(lots=0.03, max_risk_usd=25.0)
    assert described["ready"] is True
    assert described["problem"] == ""
    assert described["max_trade_risk_usd"] == pytest.approx(25.0)


# ---------------------------------------------------------------------------
# non-finite numbers
# ---------------------------------------------------------------------------
#
# Every comparison against NaN is false, so ``nan <= 0`` does not reject a NaN
# and ``risk > ceiling`` does not reject the NaN risk it produces. A NaN
# contract size therefore passed the ceiling check and returned success. These
# tests exist because "the rule fails closed" was only true for numbers that
# were numbers.


@pytest.mark.parametrize("field,label", [
    ("contract_units_per_lot", "contract units per lot"),
    ("lot_step", "lot step"),
    ("min_lot", "minimum lot"),
    ("max_lot", "maximum lot"),
])
@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_a_non_finite_spec_value_is_refused_by_name(field, label, bad):
    spec = LotSpec(**{**GOLD.__dict__, field: bad})
    lots, reason = check(spec=spec)
    assert lots is None
    assert "not a set of finite numbers" in reason
    # The reason names the field, so the operator can fix the right one.
    assert label in reason


def test_a_nan_contract_size_cannot_slip_under_the_ceiling():
    """The reported bypass, kept as a test so it cannot come back."""
    spec = LotSpec(contract_units_per_lot=float("nan"), lot_step=0.01,
                   min_lot=0.01, max_lot=50.0)
    lots, reason = check(spec=spec, max_risk_usd=25.0)
    assert lots is None
    assert "not a set of finite numbers" in reason


def test_a_nan_lot_step_is_refused_rather_than_raising():
    """``round(nan)`` raises ValueError; the check must not get that far."""
    spec = LotSpec(contract_units_per_lot=100.0, lot_step=float("nan"),
                   min_lot=0.01, max_lot=50.0)
    lots, reason = check(spec=spec)
    assert lots is None
    assert "not a set of finite numbers" in reason


def test_a_step_that_overflows_the_ratio_is_refused_not_raised():
    """A finite step can still overflow ``lots / step`` into infinity."""
    spec = LotSpec(contract_units_per_lot=100.0, lot_step=1e-320,
                   min_lot=0.01, max_lot=50.0)
    lots, reason = check(spec=spec)
    assert lots is None
    assert "not representable" in reason


@pytest.mark.parametrize("bad", [float("nan"), float("inf")])
def test_a_non_finite_lot_is_refused(bad):
    lots, reason = check(lots=bad)
    assert lots is None
    assert "not a usable number" in reason


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_a_non_finite_stop_distance_is_refused(bad):
    lots, reason = check(stop_distance=bad)
    assert lots is None
    assert "stop distance" in reason


@pytest.mark.parametrize("bad", [float("nan"), float("inf")])
def test_a_non_finite_ceiling_is_refused(bad):
    lots, reason = check(max_risk_usd=bad)
    assert lots is None
    assert "positive number" in reason


@pytest.mark.parametrize("bad", [float("nan"), float("inf")])
def test_a_non_finite_configured_lot_is_not_ready(bad):
    described = describe_fixed_lot(lots=bad, max_risk_usd=25.0)
    assert described["ready"] is False
    assert "not a usable number" in described["problem"]


def test_an_infinite_ceiling_is_not_ready():
    described = describe_fixed_lot(lots=0.03, max_risk_usd=float("inf"))
    assert described["ready"] is False
    assert "positive number" in described["problem"]


def test_a_non_finite_number_never_returns_a_size():
    """The rule's one invariant, extended to the numbers that compare false."""
    for field in ("contract_units_per_lot", "lot_step", "min_lot", "max_lot"):
        for bad in (float("nan"), float("inf"), float("-inf")):
            spec = LotSpec(**{**GOLD.__dict__, field: bad})
            lots, reason = check(spec=spec)
            assert lots is None, (field, bad)
            assert reason, (field, bad)


# ---------------------------------------------------------------------------
# import order
# ---------------------------------------------------------------------------


def test_the_pipeline_can_be_the_first_module_imported():
    """``import rade.observer.mirror_pipeline`` must work on its own.

    This runs in a fresh interpreter because the in-process test session has
    already imported ``rade.execution`` by the time any test body executes, and
    that hides the problem. ``mirror_pipeline`` imports
    ``rade.execution.sizing``, which executes the ``rade.execution`` package
    ``__init__``; when that file imported the engine eagerly the engine tried
    to read names from a half-built ``mirror_pipeline`` and the import failed
    with "cannot import name 'STATUS_MANUAL_REVIEW' from partially initialized
    module". The full suite passed anyway, because an earlier test module had
    imported the package first — so only a subprocess can catch a regression.
    """
    script = (
        "import rade.observer.mirror_pipeline as m\n"
        "assert m.STATUS_MANUAL_REVIEW == 'manual_review'\n"
        "import rade.execution as e\n"
        "assert e.ExecutionEngine.__name__ == 'ExecutionEngine'\n"
        "assert e.LotSpec.__name__ == 'LotSpec'\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True, text=True, timeout=120,
        cwd=str(Path(__file__).resolve().parents[1]),
        env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")},
    )
    assert result.returncode == 0, result.stderr

