"""Data classes for the Phase 2A labelling engine."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


@dataclass
class Setup:
    """One parsed trading setup from the channel."""

    message_pk: int  # raw_telegram_messages.id
    posted_at: datetime
    instrument: str
    direction: str  # "buy" | "sell"
    entry_type: str  # "market" | "limit"
    entry_price: float
    stop_loss: Optional[float] = None
    take_profits: list[float] = field(default_factory=list)
    raw_text: str = ""

    @property
    def risk_usd(self) -> Optional[float]:
        if self.stop_loss is None:
            return None
        return abs(self.entry_price - self.stop_loss)

    def is_valid(self) -> bool:
        return self.stop_loss is not None and self.risk_usd is not None and self.risk_usd > 0


@dataclass
class Label:
    """Resolution of one Setup against market bars."""

    setup: Setup
    is_recap: bool = False
    fill_status: str = "unfilled"  # "filled" | "unfilled"
    fill_price: Optional[float] = None
    fill_ts: Optional[datetime] = None
    exit_type: str = "unresolved"  # "tp" | "sl" | "timeout" | "unfilled" | "invalid"
    exit_price: Optional[float] = None
    exit_ts: Optional[datetime] = None
    R: Optional[float] = None
    hours_held: Optional[float] = None
    ambiguity: bool = False  # SL and a TP touched in the same bar
    ambiguity_optimistic: bool = False  # would TP have won under optimistic rule
    notes: str = ""
