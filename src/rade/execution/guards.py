"""Pre-trade risk controls for the execution path.

The per-trade cap answers "how much can this one signal lose". It cannot answer
"how much has this account lost today", "how many positions am I already
carrying", or "has the market run away from the price the signal was published
at". Those are the four questions a copier needs answered before it places an
order, and until this module existed none of them was asked.

Two rules shape everything here:

* **A guard only ever prevents a new order.** It never closes a position, never
  moves a stop, and never stops managing an open trade. A daily-loss breaker
  that also abandons open risk would convert a bad day into an unmanaged bad
  day, which is worse than the loss it is trying to cap.
* **Unknown is not safe.** If the equity, the risk state or the pause flag
  cannot be read, the answer is "do not trade", with a reason that says which
  read failed. A guard that fails open is not a guard.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable


class GuardError(RuntimeError):
    """A guard input could not be established."""


@dataclass(frozen=True)
class RiskLimits:
    """The limits in force for a new order.

    Every field is a hard number, not a hint. ``0`` disables that particular
    check, which is recorded in :meth:`describe` so a disabled check cannot be
    mistaken for a passing one.
    """

    max_open_positions: int = 3
    daily_loss_fraction: float = 0.02
    max_drawdown_fraction: float = 0.05
    max_spread: float = 5.0
    max_slippage: float = 5.0

    @classmethod
    def from_env(cls, env: dict | None = None) -> "RiskLimits":
        """Read the limits from the environment, with the defaults above."""
        source = os.environ if env is None else env

        def number(name: str, default: float) -> float:
            raw = str(source.get(name, "")).strip()
            if not raw:
                return default
            try:
                return float(raw)
            except ValueError:
                raise GuardError(f"{name}={raw!r} is not a number") from None

        positions = number("MIRROR_MAX_OPEN_POSITIONS", cls.max_open_positions)
        if positions < 0:
            raise GuardError("MIRROR_MAX_OPEN_POSITIONS cannot be negative")
        return cls(
            max_open_positions=int(positions),
            daily_loss_fraction=number("MIRROR_DAILY_LOSS_FRACTION",
                                       cls.daily_loss_fraction),
            max_drawdown_fraction=number("MIRROR_MAX_DRAWDOWN_FRACTION",
                                         cls.max_drawdown_fraction),
            max_spread=number("MIRROR_MAX_SPREAD", cls.max_spread),
            max_slippage=number("MIRROR_MAX_SLIPPAGE", cls.max_slippage),
        )

    def describe(self) -> dict:
        """What is actually enforced, and what is switched off."""
        return {
            "max_open_positions": self.max_open_positions,
            "daily_loss_fraction": self.daily_loss_fraction,
            "max_drawdown_fraction": self.max_drawdown_fraction,
            "max_spread": self.max_spread,
            "max_slippage": self.max_slippage,
            "disabled": [
                name for name, value in (
                    ("max_open_positions", self.max_open_positions),
                    ("daily_loss_fraction", self.daily_loss_fraction),
                    ("max_drawdown_fraction", self.max_drawdown_fraction),
                    ("max_spread", self.max_spread),
                    ("max_slippage", self.max_slippage),
                ) if not value
            ],
        }


@dataclass(frozen=True)
class RiskSnapshot:
    """Everything a decision needs, gathered at one moment."""

    equity: float
    day_start_equity: float
    peak_equity: float
    open_positions: int
    spread: float
    slippage: float
    #: The pause switch. ``None`` means it could not be read, which blocks.
    paused: bool | None = False
    pause_reason: str = ""


@dataclass(frozen=True)
class GuardVerdict:
    """The answer, and the reason either way."""

    allowed: bool
    reason: str
    checks: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"allowed": self.allowed, "reason": self.reason,
                "checks": self.checks}


def evaluate(limits: RiskLimits, snapshot: RiskSnapshot) -> GuardVerdict:
    """Decide whether a new order may be placed.

    The order of the checks is deliberate: the cheapest and most categorical
    first, so the reason a trade was refused names the most serious problem
    rather than whichever check happened to run first.
    """
    checks: list[dict] = []

    def note(name: str, ok: bool, detail: str) -> bool:
        checks.append({"check": name, "ok": ok, "detail": detail})
        return ok

    if snapshot.paused is None:
        note("pause", False, "the pause switch could not be read")
        return GuardVerdict(False, "the pause switch could not be read, so "
                                   "execution is treated as paused", checks)
    if snapshot.paused:
        detail = snapshot.pause_reason or "execution is paused"
        note("pause", False, detail)
        return GuardVerdict(False, f"execution is paused: {detail}", checks)
    note("pause", True, "not paused")

    if not note("equity", snapshot.equity > 0,
                f"equity {snapshot.equity:g}"):
        return GuardVerdict(False, "the account equity is not usable, so no "
                                   "size can be computed safely", checks)

    if limits.max_drawdown_fraction > 0 and snapshot.peak_equity > 0:
        floor = snapshot.peak_equity * (1.0 - limits.max_drawdown_fraction)
        ok = note("max_drawdown", snapshot.equity > floor,
                  f"equity {snapshot.equity:g} against a floor of {floor:g} "
                  f"(peak {snapshot.peak_equity:g}, limit "
                  f"{limits.max_drawdown_fraction:.2%})")
        if not ok:
            return GuardVerdict(
                False,
                f"equity {snapshot.equity:g} is below the drawdown floor "
                f"{floor:g} ({limits.max_drawdown_fraction:.2%} from the peak "
                f"{snapshot.peak_equity:g}); new orders are blocked until a "
                "human reviews the account", checks)

    if limits.daily_loss_fraction > 0 and snapshot.day_start_equity > 0:
        floor = snapshot.day_start_equity * (1.0 - limits.daily_loss_fraction)
        ok = note("daily_loss", snapshot.equity > floor,
                  f"equity {snapshot.equity:g} against a floor of {floor:g} "
                  f"(day start {snapshot.day_start_equity:g}, limit "
                  f"{limits.daily_loss_fraction:.2%})")
        if not ok:
            return GuardVerdict(
                False,
                f"the day's loss has reached the limit: equity "
                f"{snapshot.equity:g} against a floor of {floor:g} "
                f"({limits.daily_loss_fraction:.2%} of the "
                f"{snapshot.day_start_equity:g} the day opened at); no new "
                "orders today", checks)

    if limits.max_open_positions > 0:
        ok = note("max_open_positions",
                  snapshot.open_positions < limits.max_open_positions,
                  f"{snapshot.open_positions} open against a limit of "
                  f"{limits.max_open_positions}")
        if not ok:
            return GuardVerdict(
                False,
                f"{snapshot.open_positions} positions are already open and the "
                f"limit is {limits.max_open_positions}", checks)

    if limits.max_spread > 0:
        ok = note("max_spread", snapshot.spread <= limits.max_spread,
                  f"spread {snapshot.spread:g} against a limit of "
                  f"{limits.max_spread:g}")
        if not ok:
            return GuardVerdict(
                False,
                f"the spread is {snapshot.spread:g}, wider than the "
                f"{limits.max_spread:g} limit", checks)

    if limits.max_slippage > 0:
        ok = note("max_slippage", snapshot.slippage <= limits.max_slippage,
                  f"the market is {snapshot.slippage:g} from the published "
                  f"entry, against a limit of {limits.max_slippage:g}")
        if not ok:
            return GuardVerdict(
                False,
                f"the market is {snapshot.slippage:g} away from the published "
                f"entry, beyond the {limits.max_slippage:g} slippage limit; "
                "the published risk geometry no longer applies", checks)

    return GuardVerdict(True, "all pre-trade checks passed", checks)


# ---------------------------------------------------------------------------
# the state the guards need remembered between calls
# ---------------------------------------------------------------------------


def utc_day(now: datetime | None = None) -> str:
    """The UTC day the daily-loss breaker resets on."""
    return (now or datetime.now(timezone.utc)).strftime("%Y-%m-%d")


@dataclass
class RiskState:
    """The persisted part of the guards: the day's opening equity and the peak.

    Both are needed across restarts. Without the day's opening equity the
    breaker silently resets every time the service is redeployed, which on a
    free tier is often enough to make it decorative. Without the peak there is
    no drawdown to measure.
    """

    day: str
    day_start_equity: float
    peak_equity: float

    def rolled(self, equity: float, now: datetime | None = None) -> "RiskState":
        """The state after observing ``equity`` today."""
        today = utc_day(now)
        if self.day != today or self.day_start_equity <= 0:
            return RiskState(day=today, day_start_equity=equity,
                             peak_equity=max(equity, self.peak_equity))
        return RiskState(day=today,
                         day_start_equity=self.day_start_equity,
                         peak_equity=max(equity, self.peak_equity))

    def to_dict(self) -> dict:
        return {"day": self.day,
                "day_start_equity": self.day_start_equity,
                "peak_equity": self.peak_equity}

    @classmethod
    def from_dict(cls, row: dict | None) -> "RiskState | None":
        if not row:
            return None
        try:
            return cls(day=str(row.get("day") or ""),
                       day_start_equity=float(row.get("day_start_equity") or 0.0),
                       peak_equity=float(row.get("peak_equity") or 0.0))
        except (TypeError, ValueError):
            return None


EquityProvider = Callable[[], float]
