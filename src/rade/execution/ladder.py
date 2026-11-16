"""The exit ladder policy.

This module turns a published signal into an explicit, ordered plan for
taking profit and moving the stop. It contains no broker calls and no I/O: it
is arithmetic over numbers, so it can be tested exhaustively before any of it
is trusted with an order.

Two policies live here. Exactly one of them is the confirmed behaviour; the
other is retained because tests exercise it directly and because a policy that
takes profit in thirds may be wanted again later.

**``hold_to_final`` — the confirmed policy** (``docs/23`` §4):

* **Two or more targets** — hold the whole position through the first target.
  When price passes it, move the stop to that first target. Nothing is closed
  at any intermediate target. The full position closes at the final target,
  which is attached to the order at the broker.
* **One target** — close the whole position at that target. There is no earlier
  target to pass, so no earlier stop move is invented.

**``ladder`` — the earlier policy, retained but not the default:**

* Three or more targets close a third at TP1, move the stop to breakeven, close
  a third at TP2, move the stop to TP1, and let the rest close at TP3.
* Two targets close half at TP1, move the stop to breakeven, rest at TP2.
* One target closes the whole position.

Three rules apply to both:

* **A leg's ``action`` says what it does.** A leg that only moves the stop
  closes nothing, and is marked as such rather than being inferred from a zero
  quantity — inferring it is how a stop move becomes an accidental full close.
* **Quantities are rounded down to the broker's step.** Rounding up would
  close more than the plan allows. A quantity that rounds below the broker's
  minimum is a *block*, not a smaller close: the policy is not quietly
  changed to fit the broker.
* **Stops only move in the risk-reducing direction.** A stop that would move
  away from the price, or to a level the market has already passed, is
  refused.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Literal

#: How the stop is set after a leg closes.
STOP_NONE = "none"
STOP_BREAKEVEN = "breakeven"
STOP_TARGET = "target"

StopAfterKind = Literal["none", "breakeven", "target"]

#: What a leg does when its target is reached.
#:
#: ``close`` sends a partial close for ``quantity``. ``stop_move`` closes
#: nothing and only moves the stop. ``broker_target`` is the final target: the
#: broker's attached take profit closes it, and the manager only confirms it.
ACTION_CLOSE = "close"
ACTION_STOP_MOVE = "stop_move"
ACTION_BROKER_TARGET = "broker_target"

LegAction = Literal["close", "stop_move", "broker_target"]

#: The confirmed policy: hold through the first target, close at the last.
POLICY_HOLD_TO_FINAL = "hold_to_final"

#: The earlier policy: take profit in thirds (or halves), moving the stop after
#: each partial close. Not the default.
POLICY_LADDER = "ladder"

#: The maximum number of targets the ladder acts on. Anything beyond this is
#: ignored and recorded.
MAX_LADDER_TARGETS = 3



class LadderError(RuntimeError):
    """The ladder cannot be built or cannot be executed as specified.

    The message says which number was wrong. It is a block, not a warning.
    """


@dataclass(frozen=True)
class ExitLeg:
    """One step of the ladder."""

    label: str
    target: float
    #: The share of the *original* size closed at this target.
    fraction: float
    #: The quantity to close, rounded down to the broker's step. Zero on a leg
    #: that only moves the stop.
    quantity: float
    #: True when this project sends the close order. The final leg is False:
    #: the broker's attached take profit closes it, and that is what keeps the
    #: position protected through a monitoring outage.
    managed: bool
    #: What this leg does. Explicit, because a leg with ``quantity == 0`` and
    #: ``managed == False`` would otherwise be read as "close the remainder",
    #: which is the opposite of what a stop-move leg means.
    action: LegAction = ACTION_CLOSE
    stop_after_kind: StopAfterKind = STOP_NONE
    stop_after_value: float | None = None
    #: What should be left open after this leg, for the verification step.
    expected_remaining_after: float = 0.0

    @property
    def closes_quantity(self) -> bool:
        """Whether reaching this leg's target closes any part of the position."""
        return self.action != ACTION_STOP_MOVE and self.quantity > 0

    @property
    def moves_stop(self) -> bool:
        return self.stop_after_kind != STOP_NONE


    def stop_after(self, *, fill_price: float) -> float | None:
        """The stop to apply once this leg has closed.

        ``fill_price`` is the position's average price, so "breakeven" means
        the real entry rather than the published one.
        """
        if self.stop_after_kind == STOP_BREAKEVEN:
            return float(fill_price)
        if self.stop_after_kind == STOP_TARGET:
            return float(self.stop_after_value) if self.stop_after_value is not None else None
        return None


@dataclass(frozen=True)
class ExitPlan:
    """The full ladder for one trade."""

    policy: str
    side: str
    entry: float
    initial_stop: float
    final_target: float
    total_size: float
    lot_step: float
    min_lot: float
    legs: tuple[ExitLeg, ...] = field(default_factory=tuple)
    ignored_targets: tuple[float, ...] = field(default_factory=tuple)

    @property
    def is_long(self) -> bool:
        return self.side == "buy"

    @property
    def managed_legs(self) -> tuple[ExitLeg, ...]:
        return tuple(leg for leg in self.legs if leg.managed)

    def description(self) -> str:
        parts = []
        for leg in self.legs:
            if leg.action == ACTION_STOP_MOVE:
                parts.append(f"hold in full, stop to {leg.target:g} once price "
                             f"passes {leg.target:g}")
                continue
            if leg.action == ACTION_BROKER_TARGET:
                parts.append(f"close all {leg.quantity:g} at {leg.target:g} "
                             "(broker target)")
                continue
            stop = ""
            if leg.stop_after_kind == STOP_BREAKEVEN:
                stop = ", then stop to breakeven"
            elif leg.stop_after_kind == STOP_TARGET and leg.stop_after_value is not None:
                stop = f", then stop to {leg.stop_after_value:g}"
            parts.append(f"close {leg.quantity:g} at {leg.target:g}{stop}")
        text = "; ".join(parts)
        if self.ignored_targets:
            ignored = ", ".join(f"{t:g}" for t in self.ignored_targets)
            text += f" (targets beyond TP3 ignored: {ignored})"
        return text


def round_down_to_step(value: float, step: float) -> float:
    """Round *down* to a multiple of ``step``.

    Rounding down is the only safe direction: rounding a close quantity up
    would close more than the plan permits.
    """
    if step <= 0:
        raise LadderError(f"the broker's quantity step must be positive, got {step!r}")
    return round(math.floor((value + 1e-12) / step) * step, 8)


def _fraction_for(index: int, count: int) -> float:
    """The share of the original size closed at leg ``index``.

    Three or more targets close thirds; two close halves; one closes
    everything. Fractions are exact rationals where possible so that the
    three thirds sum to one rather than to 0.9999999.
    """
    if count >= 3:
        return 1.0 / 3.0
    if count == 2:
        return 0.5
    return 1.0


def _hold_to_final_plan(*, side: str, entry: float, stop: float,
                        used: list[float], ignored: tuple[float, ...],
                        size: float, lot_step: float,
                        min_lot: float) -> ExitPlan:
    """Build the confirmed policy: hold through the first target, close at the last.

    Two legs at most. The first moves the stop and closes nothing; the second is
    the final target, which the broker's attached take profit closes. With a
    single target there is nothing to pass on the way, so no earlier stop move
    is invented and the position simply closes there.
    """
    count = len(used)
    final_target = float(used[-1])
    closing_quantity = round(float(size), 8)

    if closing_quantity + 1e-9 < min_lot:
        raise LadderError(
            f"the position of {size:g} lots is below the broker's minimum of "
            f"{min_lot:g}; the exit could not be placed"
        )

    legs: list[ExitLeg] = []
    if count == 1:
        legs.append(ExitLeg(
            label="tp1",
            target=float(used[0]),
            fraction=1.0,
            quantity=closing_quantity,
            managed=False,
            action=ACTION_BROKER_TARGET,
            stop_after_kind=STOP_NONE,
            expected_remaining_after=0.0,
        ))
    else:
        first = float(used[0])
        legs.append(ExitLeg(
            label="tp1",
            target=first,
            fraction=0.0,
            quantity=0.0,
            managed=False,
            action=ACTION_STOP_MOVE,
            stop_after_kind=STOP_TARGET,
            stop_after_value=first,
            expected_remaining_after=round(float(size), 8),
        ))
        legs.append(ExitLeg(
            label=f"tp{count}",
            target=final_target,
            fraction=1.0,
            quantity=closing_quantity,
            managed=False,
            action=ACTION_BROKER_TARGET,
            stop_after_kind=STOP_NONE,
            expected_remaining_after=0.0,
        ))

    return ExitPlan(
        policy=POLICY_HOLD_TO_FINAL,
        side=side,
        entry=float(entry),
        initial_stop=float(stop),
        final_target=final_target,
        total_size=round(float(size), 8),
        lot_step=float(lot_step),
        min_lot=float(min_lot),
        legs=tuple(legs),
        ignored_targets=ignored,
    )


def build_exit_plan(*, side: str, entry: float, stop: float,
                    targets: list[float], size: float,
                    lot_step: float, min_lot: float,
                    policy: str = POLICY_HOLD_TO_FINAL) -> ExitPlan:
    """Build the exit plan for one trade, or raise :class:`LadderError`.

    ``policy`` selects between the confirmed behaviour and the earlier
    take-profit-in-thirds behaviour. The default is the confirmed one; the
    earlier policy is only built when it is asked for by name.

    The plan is built against the *original* size. At execution time the
    manager checks the live position against the plan before it closes
    anything.
    """
    if side not in {"buy", "sell"}:
        raise LadderError(f"invalid side {side!r}")
    if size <= 0:
        raise LadderError(f"the position size must be positive, got {size!r}")
    if lot_step <= 0:
        raise LadderError(f"the broker's quantity step must be positive, got {lot_step!r}")
    if min_lot <= 0:
        raise LadderError(f"the broker's minimum quantity must be positive, got {min_lot!r}")
    if policy not in {POLICY_HOLD_TO_FINAL, POLICY_LADDER}:
        raise LadderError(f"unknown exit policy {policy!r}")

    # The geometry is validated here as well as in the pipeline, because this
    # function is a second entry point: the engine builds its plan from the
    # signal it was handed, and a plan built from a stop on the wrong side of
    # the entry would be sent to the broker as a stop that is already
    # triggered. One implementation of the check, both callers.
    if not math.isfinite(entry) or entry <= 0:
        raise LadderError(
            f"the entry price must be a finite positive number, got {entry!r}")
    if not math.isfinite(stop) or stop <= 0:
        raise LadderError(
            f"the stop price must be a finite positive number, got {stop!r}")
    if (side == "buy" and stop >= entry) or (side == "sell" and stop <= entry):
        raise LadderError(
            f"the stop {stop:g} is on the wrong side of the entry {entry:g} "
            f"for a {side}; the position would be stopped out on entry")

    ordered = list(targets)
    if not ordered:
        raise LadderError("a trade needs at least one target")

    for target in ordered:
        if not math.isfinite(target) or target <= 0:
            raise LadderError(
                f"a target is not a finite positive number: {target!r}")
        if (side == "buy" and target <= entry) or (side == "sell" and target >= entry):
            raise LadderError(
                f"the target {target:g} is on the wrong side of the entry "
                f"{entry:g} for a {side}")

    is_long = side == "buy"
    # Nearest first: the first target is the closest to the entry.
    ordered.sort(reverse=not is_long)

    used = ordered[:MAX_LADDER_TARGETS]
    ignored = tuple(float(t) for t in ordered[MAX_LADDER_TARGETS:])

    if policy == POLICY_HOLD_TO_FINAL:
        return _hold_to_final_plan(side=side, entry=entry, stop=stop, used=used,
                                   ignored=ignored, size=float(size),
                                   lot_step=float(lot_step),
                                   min_lot=float(min_lot))

    count = len(used)
    if count >= 3:
        policy = "three_targets"
    elif count == 2:
        policy = "two_targets"
    else:
        policy = "one_target"

    # The broker's attached take profit is always the *final* target of the
    # ladder. That is what protects the position when this process is not
    # running.
    final_target = float(used[-1])

    legs: list[ExitLeg] = []
    remaining = float(size)
    for index, target in enumerate(used):
        fraction = _fraction_for(index, count)
        raw = float(size) * fraction
        quantity = round_down_to_step(raw, lot_step)

        # The final leg is closed by the broker's take profit, so it takes
        # whatever is left rather than a computed third.
        is_final = index == count - 1
        if is_final:
            quantity = round_down_to_step(remaining, lot_step)
            managed = False
        else:
            managed = True

        if quantity < min_lot:
            raise LadderError(
                f"the {index + 1}th close would be {quantity:g} lots, below the "
                f"broker's minimum of {min_lot:g}; the ladder is blocked rather "
                "than changed"
            )
        if quantity > remaining + 1e-9:
            raise LadderError(
                f"the {index + 1}th close ({quantity:g}) is larger than the "
                f"remaining position ({remaining:g})"
            )

        remaining = round(remaining - quantity, 8)

        stop_kind: StopAfterKind = STOP_NONE
        stop_value: float | None = None
        if not is_final:
            if index == 0:
                stop_kind = STOP_BREAKEVEN
            else:
                # After the second leg the stop moves to the first target.
                stop_kind = STOP_TARGET
                stop_value = float(used[0])

        legs.append(ExitLeg(
            label=f"tp{index + 1}",
            target=float(target),
            fraction=fraction,
            quantity=quantity,
            managed=managed,
            action=(ACTION_BROKER_TARGET if is_final else ACTION_CLOSE),
            stop_after_kind=stop_kind,
            stop_after_value=stop_value,
            expected_remaining_after=round(remaining, 8),
        ))

    if remaining > 1e-9:
        raise LadderError(
            f"the ladder leaves {remaining:g} lots unaccounted for; the plan is "
            "incomplete and is blocked rather than adjusted"
        )

    return ExitPlan(
        policy=policy,
        side=side,
        entry=float(entry),
        initial_stop=float(stop),
        final_target=final_target,
        total_size=round(float(size), 8),
        lot_step=float(lot_step),
        min_lot=float(min_lot),
        legs=tuple(legs),
        ignored_targets=ignored,
    )


def target_reached(*, side: str, target: float, bid: float, ask: float) -> bool:
    """Whether a target has been reached at the current quote.

    The price used is the one the close would actually get: the bid when
    selling out of a long, the ask when buying back a short. Using the other
    side would trigger the close before the price was really there.
    """
    if side == "buy":
        return float(bid) >= float(target)
    return float(ask) <= float(target)


def stop_move_is_risk_reducing(*, side: str, old_stop: float | None,
                               new_stop: float, price: float) -> tuple[bool, str]:
    """Whether moving the stop to ``new_stop`` reduces risk.

    A stop must move *towards* the price and must sit on the correct side of
    it, otherwise the position would be stopped out immediately by the move
    itself.
    """
    if not math.isfinite(new_stop) or new_stop <= 0:
        return False, f"the new stop {new_stop!r} is not a usable price"
    if side == "buy":
        if old_stop is not None and new_stop < old_stop - 1e-9:
            return False, (
                f"moving the stop from {old_stop:g} to {new_stop:g} would widen "
                "the risk on a long position")
        if new_stop >= price:
            return False, (
                f"a stop at {new_stop:g} is not below the market price {price:g}")
        return True, "the stop moves up on a long position"
    if old_stop is not None and new_stop > old_stop + 1e-9:
        return False, (
            f"moving the stop from {old_stop:g} to {new_stop:g} would widen "
            "the risk on a short position")
    if new_stop <= price:
        return False, (
            f"a stop at {new_stop:g} is not above the market price {price:g}")
    return True, "the stop moves down on a short position"
