"""TP-ladder normalisation.

The message parser preserves the trader's raw posting order and does not
filter targets that sit on the wrong side of the entry. Both matter once a
policy indexes ladder levels ordinally:

* **Wrong-side targets** can never be reached by a trade that filled at the
  stated entry, and they change the leg count, which changes the per-leg
  size (``size / n``).
* **Out-of-order targets** make "TP3" mean a different price in the engine
  than in the label.

On the archived cohort, 25 setups had a ladder whose raw order was not
nearest-first and 15 had at least one target on the wrong side of entry
(``docs/14-redirect-plan.md`` §2.4 Gap 1). The replay normalises both and
reports the counts.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class LadderInfo:
    """Normalised ladder plus a record of what was changed."""

    levels: list[float] = field(default_factory=list)
    """Nearest-first, wrong-side targets removed."""

    original: list[float] = field(default_factory=list)
    """The ladder exactly as published."""

    dropped_wrong_side: int = 0
    dropped_duplicate: int = 0
    reordered: bool = False
    """True when the surviving levels are not in their original order."""

    @property
    def altered(self) -> bool:
        return bool(self.dropped_wrong_side or self.dropped_duplicate or self.reordered)

    @property
    def n(self) -> int:
        return len(self.levels)


def normalise_ladder(direction: str, entry_price: float, take_profits) -> LadderInfo:
    """Return the nearest-first ladder for ``direction`` at ``entry_price``.

    ``direction`` is ``"buy"`` or ``"sell"``.
    """
    original = [float(t) for t in (take_profits or [])]
    if not original:
        return LadderInfo(levels=[], original=original)

    buy = direction.lower() == "buy"

    on_side = [t for t in original if (t > entry_price if buy else t < entry_price)]
    dropped_wrong_side = len(original) - len(on_side)

    # de-duplicate, preserving first occurrence
    seen: set[float] = set()
    unique: list[float] = []
    for t in on_side:
        if t not in seen:
            seen.add(t)
            unique.append(t)
    dropped_duplicate = len(on_side) - len(unique)

    # nearest first: ascending above entry for a long, descending below for a short
    ordered = sorted(unique, key=(lambda t: t - entry_price) if buy else (lambda t: entry_price - t))

    reordered = ordered != unique

    return LadderInfo(
        levels=ordered,
        original=original,
        dropped_wrong_side=dropped_wrong_side,
        dropped_duplicate=dropped_duplicate,
        reordered=reordered,
    )


def raw_ladder(direction: str, entry_price: float, take_profits) -> LadderInfo:
    """The ladder exactly as published, with no filtering or sorting.

    **Diagnostic use only.** This reproduces the legacy engine's behaviour
    (``src/rade/backtest/engine.py``), which indexes ``take_profits`` verbatim
    and therefore fills a wrong-side target instantly at that target's price.
    It exists so the reconciliation waterfall in
    ``docs/evidence/legacy-reconciliation.md`` can be computed rather than
    asserted. Never use it for a reported result.
    """
    original = [float(t) for t in (take_profits or [])]
    return LadderInfo(levels=list(original), original=original)
