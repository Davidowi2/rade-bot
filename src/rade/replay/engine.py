"""Paired exit-policy replay engine.

Executes the frozen spec in ``docs/14-redirect-plan.md`` §3-§4 against a bar
series, one setup at a time, under a chosen exit policy.

Design points that the acceptance criteria (AC-1..AC-16) pin down:

* **Entry cost reaches R.** The legacy engine charged the entry spread to
  equity only, so no leg's P&L - and therefore no R - ever bore it. Here the
  entry cost is charged once per setup and subtracted from setup-level net
  P&L. ``net_R`` is authoritative; ``legacy_R`` is kept alongside so the
  difference is visible and measurable (AC-3).
* **Sizing is frozen.** Size derives from a constant reference equity, never
  from the running equity curve, so both arms hold identical positions on
  identical entries (AC-4).
* **Intra-bar ordering is pessimistic and explicit.** A bar carries OHLC
  only, so the order of events inside it is unknowable. The stop wins ties;
  one target per bar; the fill is assumed at the limit even when the bar
  opened beyond it. Every such case is flagged and counted (AC-6, AC-7).
* **Caps are reported, not assumed.** Whether the pre-ladder or runner cap
  actually bound is a headline output (AC-8).
"""
from __future__ import annotations

import bisect
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Iterable, NamedTuple, Sequence

from .assumptions import Assumptions
from .ladder import LadderInfo, normalise_ladder

POLICY_A = "A"
"""Close the entire remaining position at the final target."""

POLICY_B = "B"
"""Close half the final tranche at the final target, trail the other half."""

POLICY_C = "C"
"""Policy A plus: TP2 moves the stop to TP1."""

POLICY_CB = "CB"
"""Policy B plus: TP2 moves the stop to TP1."""

POLICIES = (POLICY_A, POLICY_B, POLICY_C, POLICY_CB)


class Bar(NamedTuple):
    ts: datetime
    open: float
    high: float
    low: float
    close: float


@dataclass(frozen=True)
class Setup:
    """A published setup, as parsed by ``labelling/setup_extractor.py``."""

    setup_id: int
    posted_at: datetime
    direction: str
    entry_price: float
    stop_loss: float | None
    take_profits: Sequence[float]


@dataclass
class Leg:
    """One closed tranche of a setup."""

    kind: str
    price: float
    size: float
    ts: datetime
    is_limit: bool
    """True for target fills (free). False for market exits (slippage)."""


@dataclass
class ReplayResult:
    setup_id: int
    policy: str
    status: str
    """``filled`` | ``unfilled`` | ``invalid`` | ``no_bars``."""

    direction: str = ""
    entry_price: float = 0.0
    fill_ts: datetime | None = None
    fill_px: float | None = None
    exit_ts: datetime | None = None
    exit_reason: str = ""
    exit_price: float | None = None

    legs: list[Leg] = field(default_factory=list)

    gross_pnl: float = 0.0
    """Raw price move across all legs, before any cost."""

    exit_cost: float = 0.0
    """Slippage + commission on market exits."""

    entry_cost: float = 0.0
    """Spread + commission on entry, charged once per setup."""

    legacy_pnl: float = 0.0
    """``gross_pnl - exit_cost``. This is what the legacy engine reports."""

    net_pnl: float = 0.0
    """``legacy_pnl - entry_cost``. Authoritative."""

    risk_dollars: float = 0.0
    legacy_R: float = 0.0
    net_R: float = 0.0

    hours_held: float = 0.0
    hours_to_ftl: float | None = None
    """Hours from fill to the final ladder level being touched. ``None`` when
    the level was never reached inside the runner horizon."""

    flags: dict[str, bool] = field(default_factory=dict)

    @property
    def altered(self) -> bool:
        return bool(self.flags.get("ladder_altered"))

    def flag(self, name: str) -> bool:
        return bool(self.flags.get(name, False))


def _flags() -> dict[str, bool]:
    return {
        "ambiguous_bar": False,
        "entry_bar_stop": False,
        "open_gap": False,
        "ladder_altered": False,
        "dropped_wrong_side": False,
        "reordered_ladder": False,
        "cap_pre_ladder": False,
        "cap_runner": False,
        "window_end": False,
        "ftl_reached": False,
        "entry_bar_stop_ignored": False,
    }


def _find_fill(
    entry: float,
    posted_at: datetime,
    bars: Sequence[Bar],
    times: Sequence[datetime],
    fill_window_hours: float,
) -> int | None:
    """Index of the bar whose range contains the entry level.

    The limit rests from ``posted_at`` and fills at the first bar whose range
    brackets the entry price, within the fill window.
    """
    fill_deadline = posted_at + timedelta(hours=fill_window_hours)
    lo = bisect.bisect_left(times, posted_at)
    for i in range(lo, len(bars)):
        if bars[i].ts > fill_deadline:
            break
        bar = bars[i]
        if bar.low <= entry <= bar.high:
            return i
    return None


def ftl_reach(
    setup: Setup,
    bars: Sequence[Bar],
    a: Assumptions,
    times: Sequence[datetime] | None = None,
) -> tuple[bool, float | None]:
    """Whether the final ladder level is touched inside the runner horizon.

    This is **arm-independent**: it depends on the ladder and the price path,
    not on the exit policy, so both arms report the same population. It is the
    population the plan calls the FTL-reaching subset - the setups where the
    runner has anything to run on.

    Reaching the level is not the same as the ladder *completing*: a trade can
    be stopped out before the final target is reached, and those setups still
    count here. That distinction is the one the plan's correction note insists
    on.
    """
    if setup.stop_loss is None or not setup.take_profits:
        return False, None
    entry = float(setup.entry_price)
    sl = float(setup.stop_loss)
    if abs(entry - sl) <= 0:
        return False, None

    ladder = normalise_ladder(setup.direction, entry, setup.take_profits)
    if ladder.n == 0:
        return False, None

    times = times if times is not None else [b.ts for b in bars]
    fill_idx = _find_fill(entry, setup.posted_at, bars, times, a.fill_window_hours)
    if fill_idx is None:
        return False, None

    buy = setup.direction.lower() == "buy"
    final_level = ladder.levels[-1]
    horizon = bars[fill_idx].ts + timedelta(hours=a.runner_cap_hours)
    for i in range(fill_idx, len(bars)):
        bar = bars[i]
        if bar.ts > horizon:
            break
        touched = (bar.high >= final_level) if buy else (bar.low <= final_level)
        if touched:
            return True, (bar.ts - bars[fill_idx].ts).total_seconds() / 3600.0
    return False, None


def replay_setup(
    setup: Setup,
    bars: Sequence[Bar],
    policy: str,
    a: Assumptions,
    bar_times: Sequence[datetime] | None = None,
) -> ReplayResult:
    """Replay one setup under ``policy`` against ``bars``.

    ``bars`` must be sorted ascending by ``ts`` and already filtered to the
    instrument and timeframe. ``bar_times`` is an optional pre-computed
    ``[ts, ...]`` list used for binary-search windowing.
    """
    if policy not in POLICIES:
        raise ValueError(f"unknown policy {policy!r}; expected one of {POLICIES}")

    flags = _flags()
    buy = setup.direction.lower() == "buy"

    if setup.stop_loss is None or not setup.take_profits:
        return ReplayResult(setup_id=setup.setup_id, policy=policy, status="invalid",
                            direction=setup.direction, entry_price=setup.entry_price, flags=flags)

    entry = float(setup.entry_price)
    sl = float(setup.stop_loss)
    risk_price = abs(entry - sl)
    if risk_price <= 0:
        return ReplayResult(setup_id=setup.setup_id, policy=policy, status="invalid",
                            direction=setup.direction, entry_price=entry, flags=flags)

    ladder: LadderInfo = normalise_ladder(setup.direction, entry, setup.take_profits)
    if ladder.n == 0:
        return ReplayResult(setup_id=setup.setup_id, policy=policy, status="invalid",
                            direction=setup.direction, entry_price=entry, flags=flags)
    flags["ladder_altered"] = ladder.altered
    flags["dropped_wrong_side"] = ladder.dropped_wrong_side > 0
    flags["reordered_ladder"] = ladder.reordered

    # --- sizing: frozen against a constant reference equity (AC-4) -----
    risk_dollars = a.risk_pct * a.reference_equity
    size = risk_dollars / risk_price
    entry_cost = (a.spread + a.commission) * size

    if not bars:
        return ReplayResult(setup_id=setup.setup_id, policy=policy, status="no_bars",
                            direction=setup.direction, entry_price=entry, flags=flags)

    times = bar_times if bar_times is not None else [b.ts for b in bars]

    # --- find the fill --------------------------------------------------
    fill_idx = _find_fill(entry, setup.posted_at, bars, times, a.fill_window_hours)
    if fill_idx is None:
        return ReplayResult(setup_id=setup.setup_id, policy=policy, status="unfilled",
                            direction=setup.direction, entry_price=entry, flags=flags)

    fill_bar = bars[fill_idx]
    # The limit was resting before the fill bar opened, so a fill at the bar
    # open is possible; we assume the worse price (the limit itself) and flag it.
    open_gap = (fill_bar.open <= entry) if buy else (fill_bar.open >= entry)
    flags["open_gap"] = open_gap

    # --- the FTL-reaching population (arm-independent) -------------------
    reached, hours_to_ftl = ftl_reach(setup, bars, a, times)
    flags["ftl_reached"] = reached

    # --- walk ------------------------------------------------------------
    be = entry + a.breakeven_offset if buy else entry - a.breakeven_offset
    stop = sl
    n = ladder.n
    leg_size = size / n
    tp_i = 0
    remaining = size
    peak: float | None = None
    runner_active = False
    ladder_complete = False
    legs: list[Leg] = []
    exit_reason = ""
    exit_px: float | None = None
    exit_ts: datetime | None = None

    # The caps are wall-clock rules evaluated at bar boundaries, so a position
    # closes at the *first bar at or after* the boundary - not at the last bar
    # before it. That distinction is load-bearing: the 30m series has 73
    # weekend gaps of ~49.5h, so a Friday-evening fill has no bar at all until
    # Sunday. Truncating at the bar before the boundary would close the trade
    # at a stale Friday price and mislabel it `window_end` (AC-8).
    #
    # So the scan runs to the first bar at or after the outermost cap, plus
    # that bar itself. `window_end` is then reserved for what the plan says it
    # is: the end of available data, closed at the final bar's close.
    scan_hours = max(a.pre_ladder_cap_hours, a.runner_cap_hours)
    scan_end_ts = fill_bar.ts + timedelta(hours=scan_hours)
    scan_end_idx = min(bisect.bisect_left(times, scan_end_ts) + 1, len(bars))

    trail_distance = a.trail_multiple_r * risk_price
    tp2_to_tp1 = policy in (POLICY_C, POLICY_CB)
    last_bar: Bar | None = None

    for i in range(fill_idx, scan_end_idx):
        bar = bars[i]
        last_bar = bar
        held = (bar.ts - fill_bar.ts).total_seconds() / 3600.0

        # (a) stop first within the bar - pessimistic, and the natural path
        #     for a resting limit (price passes through the level)
        stop_hit = (bar.low <= stop) if buy else (bar.high >= stop)
        target_hit = False
        if tp_i < n:
            tp = ladder.levels[tp_i]
            target_hit = (bar.high >= tp) if buy else (bar.low <= tp)
        if stop_hit and target_hit:
            flags["ambiguous_bar"] = True
        # Optimistic bound: the target wins the tie (AC-7).
        if a.optimistic_ambiguity and stop_hit and target_hit:
            stop_hit = False
        # Optimistic bound on the fill bar: the stop is assumed to have been
        # breached *before* the limit filled, so it does not take the position
        # out. On coarse bars this is the single largest source of difference
        # between the two bounds.
        if a.optimistic_ambiguity and stop_hit and bar.ts == fill_bar.ts:
            flags["entry_bar_stop"] = True
            flags["entry_bar_stop_ignored"] = True
            stop_hit = False
        if stop_hit:
            if stop == sl:
                exit_reason = "sl"
            elif tp2_to_tp1 and stop == ladder.levels[0]:
                exit_reason = "tp1_stop"
            elif stop == be:
                exit_reason = "be_stop"
            else:
                exit_reason = "trail_stop"
            legs.append(Leg(exit_reason, stop, remaining, bar.ts, is_limit=False))
            remaining = 0.0
            exit_px = stop
            exit_ts = bar.ts
            if bar.ts == fill_bar.ts:
                flags["entry_bar_stop"] = True
            break

        # (b) hard cap while the ladder is incomplete
        if not ladder_complete and held >= a.pre_ladder_cap_hours:
            legs.append(Leg("time_cap", bar.close, remaining, bar.ts, is_limit=False))
            remaining = 0.0
            exit_reason = "time_cap"
            exit_px = bar.close
            exit_ts = bar.ts
            flags["cap_pre_ladder"] = True
            break

        # (c) at most one target leg per bar, nearest first
        if target_hit:
            tp = ladder.levels[tp_i]
            is_last = tp_i == n - 1
            if policy in (POLICY_B, POLICY_CB) and is_last:
                close_size = leg_size * a.runner_fraction_at_ftl
                legs.append(Leg("ftl_half", tp, close_size, bar.ts, is_limit=True))
                remaining -= close_size
                tp_i += 1
                runner_active = True
                ladder_complete = True
                peak = bar.high if buy else bar.low
                exit_reason = "ftl_half"
                exit_px = tp
                exit_ts = bar.ts
            else:
                kind = "ftl" if is_last else f"tp{tp_i + 1}"
                legs.append(Leg(kind, tp, leg_size, bar.ts, is_limit=True))
                remaining -= leg_size
                tp_i += 1
                exit_reason = kind
                exit_px = tp
                exit_ts = bar.ts
                if tp_i >= n:
                    ladder_complete = True
                    remaining = 0.0
            # first target leg moves the stop to cost-adjusted breakeven
            if tp_i >= 1 and a.breakeven_after_tp1:
                stop = be
            # policy C: the second target leg moves the stop to TP1
            if tp2_to_tp1 and tp_i == 2 and n >= 2:
                tp1 = ladder.levels[0]
                stop = max(stop, tp1) if buy else min(stop, tp1)

        if remaining <= 1e-12:
            break

        # (d) trail: ratcheted, never backward, effective from the next bar
        if runner_active and peak is not None:
            peak = max(peak, bar.high) if buy else min(peak, bar.low)
            trail = peak - trail_distance if buy else peak + trail_distance
            stop = max(stop, trail) if buy else min(stop, trail)

        # (e) runner cap
        if runner_active and held >= a.runner_cap_hours:
            legs.append(Leg("runner_cap", bar.close, remaining, bar.ts, is_limit=False))
            remaining = 0.0
            exit_reason = "runner_cap"
            exit_px = bar.close
            exit_ts = bar.ts
            flags["cap_runner"] = True
            break

    # --- data ran out ----------------------------------------------------
    if remaining > 1e-12:
        # close at the last bar we actually evaluated, never at a bar beyond
        # the runner cap
        last = last_bar if last_bar is not None else bars[fill_idx]
        legs.append(Leg("window_end", last.close, remaining, last.ts, is_limit=False))
        remaining = 0.0
        exit_reason = "window_end"
        exit_px = last.close
        exit_ts = last.ts
        flags["window_end"] = True

    # --- pnl -------------------------------------------------------------
    gross = 0.0
    exit_cost = 0.0
    for leg in legs:
        gross += (leg.price - entry) * leg.size if buy else (entry - leg.price) * leg.size
        if not leg.is_limit:
            exit_cost += (a.slippage + a.commission) * leg.size

    legacy_pnl = gross - exit_cost
    net_pnl = legacy_pnl - entry_cost

    hours_held = 0.0
    if exit_ts is not None and fill_bar.ts is not None:
        hours_held = (exit_ts - fill_bar.ts).total_seconds() / 3600.0

    return ReplayResult(
        setup_id=setup.setup_id,
        policy=policy,
        status="filled",
        direction=setup.direction,
        entry_price=entry,
        fill_ts=fill_bar.ts,
        fill_px=entry,
        exit_ts=exit_ts,
        exit_reason=exit_reason,
        exit_price=exit_px,
        legs=legs,
        gross_pnl=gross,
        exit_cost=exit_cost,
        entry_cost=entry_cost,
        legacy_pnl=legacy_pnl,
        net_pnl=net_pnl,
        risk_dollars=risk_dollars,
        legacy_R=legacy_pnl / risk_dollars if risk_dollars else 0.0,
        net_R=net_pnl / risk_dollars if risk_dollars else 0.0,
        hours_held=hours_held,
        hours_to_ftl=hours_to_ftl,
        flags=flags,
    )


def replay_cohort(
    setups: Iterable[Setup],
    bars: Sequence[Bar],
    policy: str,
    a: Assumptions,
) -> list[ReplayResult]:
    """Replay every setup under ``policy``. Deterministic and order-stable."""
    times = [b.ts for b in bars]
    return [replay_setup(s, bars, policy, a, times) for s in setups]
