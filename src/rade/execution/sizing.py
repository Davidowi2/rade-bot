"""Sizing rules: a ceiling on size, and the risk budget that fills it.

The configured lot is a **maximum**, not a target. A signal is copied at the
largest size up to that maximum whose stop risk stays inside the per-trade USD
ceiling - see :func:`fit_lot_to_risk`.

This replaced "exactly this lot or refuse", which was too strict in a way that
cost real trades. The channel posts a 10-point stop; by the time the message is
read the market has often moved, so the stop is 15 or 20 points from where the
order would actually fill. At a fixed 0.02 lots that is $30-$40 of risk against
a $30 ceiling, so the signal was refused even though 0.01 lots risks half of
that and fits comfortably. The ceiling is the risk limit; the lot is only the
largest size the operator wants.

What is still forbidden, for the original reason:

* rounding **up** to the nearest step, which would put the order back over the
  ceiling - the one thing the ceiling exists to prevent;
* growing past the configured maximum, so a very tight stop cannot turn a small
  budget into a large position;
* any silent substitution. Every size is either computed by this rule or the
  signal is refused with the numbers that failed.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

__all__ = ["LotSpec", "check_broker_lot", "check_fixed_lot",
           "describe_fixed_lot", "fit_lot_to_risk", "stop_risk_usd"]


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
            f"{spec.contract_units_per_lot:g} units per lot); this size was "
            "fitted to a different price and the market has moved since")

    return usable, ""


def fit_lot_to_risk(*, cap_lots: float, spec: LotSpec, stop_distance: float,
                    max_risk_usd: float | None) -> tuple[float | None, str]:
    """The largest lot up to ``cap_lots`` whose stop risk stays inside the ceiling.

    The fixed lot becomes a ceiling on size rather than an exact size. That is a
    deliberate change of policy, and it exists because the old one refused
    trades the budget could easily afford:

        published entry 3870, stop 3880, market already 3860.62

    At 0.02 lots that is $38.66 of stop risk against a $30 ceiling, so the order
    was refused - even though 0.01 lots risks $19.33 and fits. Refusing was
    never the point; sizing was.

    Rounding is always DOWN, to the broker's step. Rounding up would put the
    order back over the ceiling, which is the one thing this function exists to
    prevent. ``cap_lots`` is applied after the arithmetic, so the size can never
    exceed the configured maximum however tight the stop is - the tighter the
    stop the larger the affordable size, and a 1-point stop would otherwise ask
    for 0.3 lots on a $30 budget.

    Returns ``(lots, "")`` or ``(None, reason)``. The reason names the numbers,
    because "size too large" cannot be diagnosed from the ledger afterwards.
    """
    usable, reason = check_broker_lot(lots=cap_lots, spec=spec)
    if usable is None:
        return None, reason

    if max_risk_usd is None:
        return None, ("no per-trade USD risk ceiling is configured "
                      "(MIRROR_MAX_TRADE_RISK_USD); without it there is no "
                      "budget to size against, so the signal is refused")
    if not math.isfinite(max_risk_usd) or max_risk_usd <= 0:
        return None, (f"the per-trade USD risk ceiling {max_risk_usd!r} must be a "
                      "positive number; no size can be accepted against it")

    if not math.isfinite(stop_distance) or stop_distance <= 0:
        return None, (f"the stop distance {stop_distance!r} is not usable, so no "
                      "size can be computed for it")

    risk_per_lot = stop_distance * spec.contract_units_per_lot
    if not math.isfinite(risk_per_lot) or risk_per_lot <= 0:
        return None, (f"the risk of one lot over a {stop_distance:g} stop is "
                      f"{risk_per_lot!r}, so no size can be computed")

    affordable = max_risk_usd / risk_per_lot
    steps = math.floor(affordable / spec.lot_step + 1e-9)
    lots = round(steps * spec.lot_step, 8)

    if lots > usable:
        # The ceiling is not the binding constraint here; the configured
        # maximum is. Risk at the cap is necessarily below the ceiling.
        lots = usable

    if lots < spec.min_lot:
        smallest_risk = stop_risk_usd(lots=spec.min_lot, spec=spec,
                                      stop_distance=stop_distance)
        return None, (
            f"the smallest tradeable lot {spec.min_lot:g} would risk "
            f"${smallest_risk:.2f} over a {stop_distance:.2f} stop, above the "
            f"${max_risk_usd:.2f} per-trade ceiling; the stop is too wide to "
            "copy inside the budget")

    return lots, ""


def describe_fixed_lot(*, lots: float | None, max_risk_usd: float | None,
                       contract_units: float | None = None,
                       min_lot: float | None = None) -> dict:
    """What the sizing rule is configured to do, for the status page.

    ``ready`` is false when the rule cannot accept any signal, and
    ``problem`` says why. A rule that is switched off must not read as a rule
    that is passing.

    Two stop widths are reported, because after the rule changed they are no
    longer the same number:

    * ``max_stop_distance`` - the widest stop that fits at the *configured*
      lot. This is the size the operator chose, so it says what that choice
      buys.
    * ``widest_stop_distance`` - the widest stop the copier can take at all,
      which is governed by the broker's **minimum** lot, not the configured
      one. Since a wide stop is fitted down to 0.01, this is the number that
      decides whether a signal is copied or refused, and reporting only the
      first would understate the range by a factor of ``lots / min_lot``.
    """
    def with_extras(base: dict) -> dict:
        if (contract_units and lots and max_risk_usd
                and math.isfinite(contract_units) and contract_units > 0
                and math.isfinite(lots) and lots > 0
                and math.isfinite(max_risk_usd) and max_risk_usd > 0):
            base["max_stop_distance"] = max_risk_usd / (lots * contract_units)
            if min_lot and math.isfinite(min_lot) and min_lot > 0:
                # The floor is what admits a wide stop: the order is fitted
                # down to it before it is refused.
                base["widest_stop_distance"] = (
                    max_risk_usd / (min_lot * contract_units))
            else:
                base["widest_stop_distance"] = None
        else:
            base["max_stop_distance"] = None
            base["widest_stop_distance"] = None
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
