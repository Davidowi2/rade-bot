"""Cost model for the backtest."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class CostModel:
    spread: float = 0.25
    slippage: float = 0.25
    slippage_enabled: bool = True

    def entry_cost(self, size: float) -> float:
        return self.spread * size

    def market_exit_cost(self, size: float) -> float:
        if not self.slippage_enabled:
            return 0.0
        return self.slippage * size

    def limit_exit_cost(self, size: float) -> float:
        return 0.0
