"""The fixed-lot sizing rule.

One rule, stated once: the configured lot is either valid for this signal or the
signal is refused. There is no third outcome.

Every third outcome is a size nobody chose. Rounding to the nearest valid lot
changes the risk without saying so; clamping to the broker's maximum makes the
order smaller than the one that was decided; skipping quietly makes the operator
believe a signal was considered when it was discarded. All three look like
success in a log line, which is exactly why the rule forbids them.

The rule also cannot be applied without a ceiling. A fixed lot is not a risk
limit: ``0.03`` lots on a 10-unit stop and ``0.03`` lots on a 30-unit stop are
the same order and three times the money. The ceiling is what turns the lot into
a limit, so a missing ceiling is a configuration error and blocks the signal
rather than falling back to another sizing mode.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

__all__ = ["LotSpec", "check_broker_lot", "check_fixed_lot",
           "describe_fixed_lot", "stop_risk_usd"]


@dataclass(frozen=True)
class LotSpec:
    """The broker's quantity rules for one instrument.

    ``verified`` is not a formality. An unverified contract size means the USD
    risk of a lot cannot be computed at all, so every check below would be
    arithmetic over a guess.
    """

    contract_units_per_lot: float
    lot_step: float
    min_lot: float
    max_lot: float
    verified: bool = True


def _representable(lots: float, step: float) -> tuple[bool, float]:
    """Whether ``lots`` sits exactly on the broker's step, and the ratio."""
    ratio = lots / step
    # A finite step can still overflow the ratio (a 1e-320 step against 0.03
    # lots is infinity), and ``round`` raises on that. Treat it as "not on the
    # step" rather than letting an arithmetic edge case escape as an exception.
    if not math.isfinite(ratio):
        return False, ratio
    return abs(ratio - round(ratio)) < 1e-9, ratio


def _spec_numbers(spec: LotSpec) -> tuple[tuple[str, float], ...]:
    """The four spec values, paired with their names for error messages."""
    return (
        ("contract units per lot", spec.contract_units_per_lot),
        ("lot step", spec.lot_step),
        ("minimum lot", spec.min_lot),
        ("maximum lot", spec.max_lot),
    )


def check_broker_lot(*, lots: float, spec: LotSpec) -> tuple[float | None, str]:
    """Whether this exact lot is tradeable at the broker, ignoring the ceiling.

    This is the half of the rule that depends only on the instrument, so the
    pipeline and the engine can both apply it without either owning a copy of
    the arithmetic. The ceiling is deliberately absent: it answers a different
    question ("how much money may this lose"), and the engine has to be able to
    ask the broker question on its own.

    Returns ``(lots, "")`` or ``(None, reason)``. Every number is checked for
    finiteness **before** it is used in arithmetic. ``NaN`` is the reason that
    ordering matters: every comparison against NaN is false, so ``nan <= 0``
    does not reject it, and a NaN risk is not greater than any ceiling. A NaN
    contract size therefore passes a naive check and reports success.
    """
    if not spec.verified:
        return None, ("the broker's instrument specification is not verified, so "
                      "the USD risk of a lot cannot be computed")

    non_finite = [
        f"{name} {value!r}"
        for name, value in _spec_numbers(spec)
        if not math.isfinite(value)
    ]
    if non_finite:
        return None, ("the broker's contract/lot specification is not a set of "
                      f"finite numbers ({'; '.join(non_finite)}); the USD risk of "
                      "a lot cannot be computed from it")

    if (spec.contract_units_per_lot <= 0 or spec.lot_step <= 0
            or spec.min_lot <= 0 or spec.max_lot < spec.min_lot):
        return None, ("the broker's contract/lot specification is incomplete "
                      f"(units {spec.contract_units_per_lot!r}, step "
                      f"{spec.lot_step!r}, min {spec.min_lot!r}, max "
                      f"{spec.max_lot!r})")

    if not math.isfinite(lots) or lots <= 0:
        return None, f"the lot {lots!r} is not a usable number"

    on_step, ratio = _representable(lots, spec.lot_step)
    if not on_step:
        return None, (f"the lot {lots:g} is not representable at the broker's lot "
                      f"step {spec.lot_step:g} "
                      f"({lots:g} / {spec.lot_step:g} = {ratio:.6f}, not a whole "
                      "number of steps)")

    if lots < spec.min_lot:
        return None, (f"the lot {lots:g} is below the broker's minimum of "
                      f"{spec.min_lot:g}")

    if lots > spec.max_lot:
        return None, (f"the lot {lots:g} is above the broker's maximum of "
                      f"{spec.max_lot:g}")

    return round(float(lots), 8), ""


def stop_risk_usd(*, lots: float, spec: LotSpec, stop_distance: float) -> float:
    """The dollars at risk if the stop is hit, at this size and this stop.

    Kept in one place because it is the only number that makes a fixed lot a
    risk limit rather than a size, and two implementations of it would be one
    too many.
    """
    return lots * spec.contract_units_per_lot * stop_distance


def check_fixed_lot(*, lots: float, spec: LotSpec, stop_distance: float,
                    max_risk_usd: float | None) -> tuple[float | None, str]:
    """Decide whether the configured lot may be used for this signal.

    Returns ``(lots, "")`` when it may, and ``(None, reason)`` when it may not.
    The reason always names the number that failed, because a refusal that says
    only "invalid size" cannot be diagnosed from the ledger after the fact.

    The checks run most-categorical first, so the reason a signal was refused
    names the most serious problem rather than whichever check happened to run
    first: is this a tradeable lot at this broker, then is its dollar risk
    inside the ceiling.
    """
    usable, reason = check_broker_lot(lots=lots, spec=spec)
    if usable is None:
        return None, reason

    # The ceiling comes before the arithmetic that needs it, so the reason names
    # the configuration rather than a derived number.
    if max_risk_usd is None:
        return None, ("no per-trade USD risk ceiling is configured "
                      "(MIRROR_MAX_TRADE_RISK_USD); a fixed lot without a "
                      "ceiling is not a risk limit, so the signal is refused")
    if not math.isfinite(max_risk_usd) or max_risk_usd <= 0:
        return None, (f"the per-trade USD risk ceiling {max_risk_usd!r} must be a "
                      "positive number; no size can be accepted against it")

    if not math.isfinite(stop_distance) or stop_distance <= 0:
        return None, (f"the stop distance {stop_distance!r} is not usable, so the "
                      "risk of this size cannot be computed")

    risk_usd = stop_risk_usd(lots=usable, spec=spec, stop_distance=stop_distance)
    if not math.isfinite(risk_usd):
        return None, (
            f"the stop risk of {usable:g} lot over a {stop_distance:g} stop is not "
            "a finite dollar amount, so it cannot be compared with the "
            f"${max_risk_usd:.2f} ceiling")
    if risk_usd > max_risk_usd + 1e-9:
        return None, (
            f"the stop risk ${risk_usd:.2f} at {usable:g} lot exceeds the "
            f"${max_risk_usd:.2f} per-trade ceiling "
            f"(stop distance {stop_distance:g}, "
            f"{spec.contract_units_per_lot:g} units per lot); the signal is "
            "refused rather than resized")

    return usable, ""


def describe_fixed_lot(*, lots: float | None, max_risk_usd: float | None,
                       contract_units: float | None = None) -> dict:
    """What the fixed-lot rule is configured to do, for the status page.

    ``ready`` is false when the rule cannot accept any signal, and
    ``problem`` says why. A rule that is switched off must not read as a rule
    that is passing.

    ``max_stop_distance`` is the stop width the ceiling can actually accommodate
    at this lot and contract size. It is reported because the two settings that
    produce it are configured independently: a lot and a ceiling that are each
    plausible can together refuse every signal the channel posts, and neither
    number alone says so.
    """
    def with_extras(base: dict) -> dict:
        if (contract_units and lots and max_risk_usd
                and math.isfinite(contract_units) and contract_units > 0
                and math.isfinite(lots) and lots > 0
                and math.isfinite(max_risk_usd) and max_risk_usd > 0):
            base["max_stop_distance"] = max_risk_usd / (lots * contract_units)
        else:
            base["max_stop_distance"] = None
        return base

    if lots is None:
        return with_extras({"mode": "risk_fraction", "fixed_lots": None,
                            "max_trade_risk_usd": max_risk_usd, "ready": True,
                            "problem": ""})
    # A NaN lot reads as "configured" to every comparison, so it is checked by
    # name before anything else can treat it as a working rule.
    if not math.isfinite(lots) or lots <= 0:
        return with_extras({"mode": "fixed_lots", "fixed_lots": lots,
                            "max_trade_risk_usd": max_risk_usd, "ready": False,
                            "problem": (f"the configured fixed lot {lots!r} is not a usable "
                                        f"number; every signal will be refused")})
    if max_risk_usd is None:
        return with_extras({"mode": "fixed_lots", "fixed_lots": float(lots),
                            "max_trade_risk_usd": None, "ready": False,
                            "problem": ("no per-trade USD risk ceiling is configured; every "
                                        "signal will be refused")})
    if not math.isfinite(max_risk_usd) or max_risk_usd <= 0:
        return with_extras({"mode": "fixed_lots", "fixed_lots": float(lots),
                            "max_trade_risk_usd": max_risk_usd, "ready": False,
                            "problem": ("the per-trade USD risk ceiling is not a positive "
                                        "number; every signal will be refused")})
    return with_extras({"mode": "fixed_lots", "fixed_lots": float(lots),
                        "max_trade_risk_usd": float(max_risk_usd), "ready": True,
                        "problem": ""})
