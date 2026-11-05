"""Backtest metrics."""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
import pandas as pd


@dataclass
class Trade:
    setup_id: int
    side: str
    entry_price: float
    entry_ts: pd.Timestamp
    exit_price: float
    exit_ts: pd.Timestamp
    exit_type: str  # tp / sl / timeout
    size: float
    pnl: float
    risk_dollars: float
    R: float
    hours_held: float


@dataclass
class BacktestResult:
    trades: list[Trade] = field(default_factory=list)
    equity_curve: list[float] = field(default_factory=list)
    timestamps: list[pd.Timestamp] = field(default_factory=list)
    initial_equity: float = 100000.0
    final_equity: float = 100000.0
    max_drawdown: float = 0.0
    max_drawdown_pct: float = 0.0

    def metrics(self) -> dict:
        if not self.trades:
            return {"n_trades": 0}
        pnls = np.array([t.pnl for t in self.trades])
        Rs = np.array([t.R for t in self.trades])
        wins = Rs[Rs > 0]
        losses = Rs[Rs <= 0]
        gross_profit = pnls[pnls > 0].sum() if (pnls > 0).any() else 0.0
        gross_loss = abs(pnls[pnls < 0].sum()) if (pnls < 0).any() else 0.0
        pf = gross_profit / gross_loss if gross_loss > 0 else float("inf")
        win_rate = len(wins) / len(Rs) if len(Rs) else 0.0
        expectancy_R = float(Rs.mean()) if len(Rs) else 0.0
        # sharpe (per-trade, annualized roughly)
        if len(Rs) > 1 and Rs.std() > 0:
            sharpe = float(Rs.mean() / Rs.std() * math.sqrt(252))
        else:
            sharpe = 0.0
        avg_hold = float(np.mean([t.hours_held for t in self.trades]))
        return {
            "n_trades": len(self.trades),
            "win_rate": round(win_rate, 4),
            "profit_factor": round(pf, 3),
            "expectancy_R": round(expectancy_R, 4),
            "total_R": round(float(Rs.sum()), 3),
            "total_pnl": round(float(pnls.sum()), 2),
            "max_drawdown": round(self.max_drawdown, 2),
            "max_drawdown_pct": round(self.max_drawdown_pct, 4),
            "sharpe": round(sharpe, 3),
            "avg_hold_hours": round(avg_hold, 2),
            "final_equity": round(self.final_equity, 2),
        }
