"""Frozen assumptions for the paired replay.

Every value here is fixed by ``docs/14-redirect-plan.md`` §4.3 and must not be
tuned on the validation or holdout splits. Anything that needs tuning is a
*policy* parameter (trail multiple, runner fraction), and those are selected
on validation only.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class Assumptions:
    """Cost model, sizing, caps and horizon for one replay run."""

    # --- cost model (observed) -----------------------------------------
    spread: float = 0.25
    """Charged on entry, once per setup."""

    slippage: float = 0.25
    """Charged on market exits (stop, time cap, runner cap, window end). Limit
    exits are free."""

    commission: float = 0.0
    """Absent from the whole repo; kept explicit so the model is complete."""

    # --- sizing (frozen) ------------------------------------------------
    reference_equity: float = 100_000.0
    """Constant. Sizing never derives from the running equity curve, so both
    arms hold identical positions on identical entries (AC-4)."""

    risk_pct: float = 0.005
    """0.5% of ``reference_equity`` per setup."""

    # --- horizons -------------------------------------------------------
    fill_window_hours: float = 24.0
    """How long after ``posted_at`` the limit order may fill."""

    pre_ladder_cap_hours: float = 24.0
    """Hard cap while the TP ladder is still incomplete."""

    runner_cap_hours: float = 48.0
    """Hard cap once the runner is active. Policy A never reaches it."""

    # --- policy parameters (selected on validation only) ----------------
    trail_multiple_r: float = 1.0
    """Trail distance behind the favourable peak, in units of initial risk."""

    runner_fraction_at_ftl: float = 0.5
    """Fraction of the final tranche that trails; the remainder closes."""

    breakeven_after_tp1: bool = True
    """Move the stop to cost-adjusted breakeven after the first target leg.

    ``True`` is the frozen plan's common rule, applied identically to both
    arms. ``False`` reproduces the *archived* S0 configuration, which did not
    bundle this move - the two are not the same strategy, and the absolute
    numbers are only comparable with the flag matched (AC-14).
    """

    # --- determinism ----------------------------------------------------
    seed: int = 42
    """Recorded for reproducibility. The replay itself is deterministic and
    uses no randomness (AC-12)."""

    optimistic_ambiguity: bool = False
    """Resolve the stop/target tie in favour of the target.

    The base case is pessimistic: a bar that touches both the stop and a
    target closes at the stop, because a resting limit is filled as price
    passes through the level. This flag produces the **optimistic bound**,
    which the plan requires to be published next to the base case (AC-7).

    It matters most on coarse bars. At 30m, 607 of 1,969 setups are stopped
    out on the very bar their limit filled on, because a 30-minute bar's
    range is wide enough to span both the entry and the stop. On 1m bars that
    is far rarer, so the gap between the two bounds is a direct measure of how
    much of the 30m result is an artefact of the bar width.
    """

    @property
    def breakeven_offset(self) -> float:
        """Price offset that covers the modelled round-trip cost.

        A long's breakeven stop sits at ``entry + offset``; a short's at
        ``entry - offset``. Sized to cover the entry spread and the market
        exit slippage, so a runner stopped at the floor is approximately 0R
        *under this model* - an assumption, not a guarantee.
        """
        return self.spread + self.slippage

    def to_dict(self) -> dict:
        d = asdict(self)
        d["breakeven_offset"] = self.breakeven_offset
        return d
