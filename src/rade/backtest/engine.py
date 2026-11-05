"""Event-driven backtest engine (bar-by-bar on 30m)."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta

import numpy as np
import pandas as pd

from .costs import CostModel
from .metrics import BacktestResult, Trade


@dataclass
class Position:
    setup_id: int
    side: str
    entry_price: float
    entry_ts: pd.Timestamp
    size: float  # total size in units
    remaining: float
    stop_loss: float
    take_profits: list[float]
    tp_idx: int = 0
    sl_at_be: bool = False
    move_sl_to_be: bool = False
    max_hold_hours: float = 15.0
    risk_dollars: float = 0.0


class BacktestEngine:
    def __init__(
        self,
        bars: pd.DataFrame,
        setups: list,  # labelled setups with entry, SL, TPs, posted_at, message_pk
        cost_model: CostModel,
        risk_pct: float = 0.005,
        initial_equity: float = 100000.0,
        max_hold_hours: float = 15.0,
        move_sl_to_be: bool = False,
        fill_window_hours: float = 24.0,
        max_daily_loss_pct: float = None,
        max_dd_pct: float = None,
        max_open_positions: int = None,
    ):
        self.bars = bars.sort_values("ts").reset_index(drop=True)
        # coerce ts to tz-aware UTC
        self.bars["ts"] = pd.to_datetime(self.bars["ts"], utc=True)
        self.setups = sorted(setups, key=lambda s: pd.Timestamp(s.posted_at))
        self.cost_model = cost_model
        self.risk_pct = risk_pct
        self.initial_equity = initial_equity
        self.max_hold_hours = max_hold_hours
        self.move_sl_to_be = move_sl_to_be
        self.fill_window_hours = fill_window_hours
        self.max_daily_loss_pct = max_daily_loss_pct
        self.max_dd_pct = max_dd_pct
        self.max_open_positions = max_open_positions
        self._start_of_day_equity = initial_equity
        self._current_day = None
        self._trading_halted = False

    def run(self) -> BacktestResult:
        result = BacktestResult(initial_equity=self.initial_equity)
        equity = self.initial_equity
        peak = equity
        max_dd = 0.0
        max_dd_pct = 0.0

        positions: list[Position] = []
        pending: list = []  # setups not yet filled
        setup_idx = 0
        n_setups = len(self.setups)

        # helper to add setups whose posted_at <= ts
        def ingest_pending(ts):
            nonlocal setup_idx
            while setup_idx < n_setups and pd.Timestamp(self.setups[setup_idx].posted_at) <= ts:
                pending.append(self.setups[setup_idx])
                setup_idx += 1

        # expire pending older than fill_window
        def expire_pending(ts):
            cutoff = ts - pd.Timedelta(hours=self.fill_window_hours)
            pending[:] = [s for s in pending if pd.Timestamp(s.posted_at) >= cutoff]

        for i in range(len(self.bars)):
            bar = self.bars.iloc[i]
            ts = pd.Timestamp(bar["ts"])
            if ts.tzinfo is None:
                ts = ts.tz_localize("UTC")

            ingest_pending(ts)
            expire_pending(ts)

            # daily reset
            day = ts.date()
            if self._current_day != day:
                self._current_day = day
                self._start_of_day_equity = equity

            # max DD halt
            if self.max_dd_pct is not None and peak > 0 and (peak - equity) / peak >= self.max_dd_pct:
                self._trading_halted = True

            # --- entries: check pending setups for limit touch ---
            still_pending = []
            for s in pending:
                if self._trading_halted:
                    still_pending.append(s)
                    continue
                if self.max_daily_loss_pct is not None and (equity - self._start_of_day_equity) <= -self.max_daily_loss_pct * self.initial_equity:
                    still_pending.append(s)
                    continue
                if self.max_open_positions is not None and len(positions) >= self.max_open_positions:
                    still_pending.append(s)
                    continue
                entry = float(s.entry_price)
                if bar["low"] <= entry <= bar["high"]:
                    # fill at entry (limit, 0 slippage)
                    sl = float(s.stop_loss) if s.stop_loss is not None else None
                    if sl is None:
                        still_pending.append(s)
                        continue
                    risk_price = abs(entry - sl)
                    if risk_price <= 0:
                        still_pending.append(s)
                        continue
                    risk_dollars = self.risk_pct * equity
                    size = risk_dollars / risk_price
                    tps = list(s.take_profits) if s.take_profits else []
                    pos = Position(
                        setup_id=int(s.message_pk),
                        side=s.direction,
                        entry_price=entry,
                        entry_ts=ts,
                        size=size,
                        remaining=size,
                        stop_loss=sl,
                        take_profits=tps,
                        move_sl_to_be=self.move_sl_to_be,
                        max_hold_hours=self.max_hold_hours,
                        risk_dollars=risk_dollars,
                    )
                    positions.append(pos)
                    # entry cost (spread)
                    equity -= self.cost_model.entry_cost(size)
                else:
                    still_pending.append(s)
            pending = still_pending

            # --- exits on open positions ---
            still_open = []
            for pos in positions:
                closed_this_bar = False
                # timeout check
                hours_held = (ts - pos.entry_ts).total_seconds() / 3600.0
                if hours_held >= pos.max_hold_hours:
                    # close remaining at bar close (market, slippage)
                    exit_px = float(bar["close"])
                    if pos.side == "sell":
                        pnl = pos.remaining * (pos.entry_price - exit_px)
                    else:
                        pnl = pos.remaining * (exit_px - pos.entry_price)
                    pnl -= self.cost_model.market_exit_cost(pos.remaining)
                    R = pnl / pos.risk_dollars if pos.risk_dollars > 0 else 0.0
                    trade = Trade(
                        setup_id=pos.setup_id,
                        side=pos.side,
                        entry_price=pos.entry_price,
                        entry_ts=pos.entry_ts,
                        exit_price=exit_px,
                        exit_ts=ts,
                        exit_type="timeout",
                        size=pos.remaining,
                        pnl=pnl,
                        risk_dollars=pos.risk_dollars,
                        R=R,
                        hours_held=hours_held,
                    )
                    result.trades.append(trade)
                    equity += pnl
                    pos.remaining = 0
                    closed_this_bar = True

                if closed_this_bar or pos.remaining <= 0:
                    if pos.remaining <= 0:
                        continue
                    still_open.append(pos)
                    continue

                # SL check
                if pos.side == "buy":
                    sl_touched = bar["low"] <= pos.stop_loss
                else:
                    sl_touched = bar["high"] >= pos.stop_loss

                if sl_touched:
                    exit_px = pos.stop_loss
                    if pos.side == "sell":
                        pnl = pos.remaining * (pos.entry_price - exit_px)
                    else:
                        pnl = pos.remaining * (exit_px - pos.entry_price)
                    pnl -= self.cost_model.market_exit_cost(pos.remaining)
                    R = pnl / pos.risk_dollars if pos.risk_dollars > 0 else 0.0
                    trade = Trade(
                        setup_id=pos.setup_id,
                        side=pos.side,
                        entry_price=pos.entry_price,
                        entry_ts=pos.entry_ts,
                        exit_price=exit_px,
                        exit_ts=ts,
                        exit_type="sl",
                        size=pos.remaining,
                        pnl=pnl,
                        risk_dollars=pos.risk_dollars,
                        R=R,
                        hours_held=hours_held,
                    )
                    result.trades.append(trade)
                    equity += pnl
                    pos.remaining = 0
                    closed_this_bar = True

                if closed_this_bar or pos.remaining <= 0:
                    if pos.remaining <= 0:
                        continue
                    still_open.append(pos)
                    continue

                # TP check (nearest first)
                # determine which TPs are touched this bar
                tp_hit = None
                if pos.tp_idx < len(pos.take_profits):
                    tp = pos.take_profits[pos.tp_idx]
                    if pos.side == "buy":
                        if bar["high"] >= tp:
                            tp_hit = tp
                    else:
                        if bar["low"] <= tp:
                            tp_hit = tp

                if tp_hit is not None:
                    n_tps = len(pos.take_profits)
                    frac = 1.0 / n_tps if n_tps > 0 else 1.0
                    close_size = pos.size * frac
                    if close_size > pos.remaining:
                        close_size = pos.remaining
                    if pos.side == "sell":
                        pnl = close_size * (pos.entry_price - tp_hit)
                    else:
                        pnl = close_size * (tp_hit - pos.entry_price)
                    pnl -= self.cost_model.limit_exit_cost(close_size)
                    R = pnl / pos.risk_dollars if pos.risk_dollars > 0 else 0.0
                    trade = Trade(
                        setup_id=pos.setup_id,
                        side=pos.side,
                        entry_price=pos.entry_price,
                        entry_ts=pos.entry_ts,
                        exit_price=tp_hit,
                        exit_ts=ts,
                        exit_type="tp",
                        size=close_size,
                        pnl=pnl,
                        risk_dollars=pos.risk_dollars,
                        R=R,
                        hours_held=hours_held,
                    )
                    result.trades.append(trade)
                    equity += pnl
                    pos.remaining -= close_size
                    pos.tp_idx += 1

                    # move SL to BE after first TP if configured
                    if pos.move_sl_to_be and pos.tp_idx == 1:
                        pos.stop_loss = pos.entry_price
                        pos.sl_at_be = True

                if pos.remaining > 1e-9:
                    still_open.append(pos)

            positions = still_open

            # track equity curve
            result.equity_curve.append(equity)
            result.timestamps.append(ts)
            if equity > peak:
                peak = equity
            dd = peak - equity
            if dd > max_dd:
                max_dd = dd
            if peak > 0 and dd / peak > max_dd_pct:
                max_dd_pct = dd / peak

        # close any remaining positions at last bar close
        if positions:
            last_bar = self.bars.iloc[-1]
            last_ts = pd.Timestamp(last_bar["ts"])
            if last_ts.tzinfo is None:
                last_ts = last_ts.tz_localize("UTC")
            for pos in positions:
                exit_px = float(last_bar["close"])
                if pos.side == "sell":
                    pnl = pos.remaining * (pos.entry_price - exit_px)
                else:
                    pnl = pos.remaining * (exit_px - pos.entry_price)
                pnl -= self.cost_model.market_exit_cost(pos.remaining)
                R = pnl / pos.risk_dollars if pos.risk_dollars > 0 else 0.0
                trade = Trade(
                    setup_id=pos.setup_id,
                    side=pos.side,
                    entry_price=pos.entry_price,
                    entry_ts=pos.entry_ts,
                    exit_price=exit_px,
                    exit_ts=last_ts,
                    exit_type="timeout",
                    size=pos.remaining,
                    pnl=pnl,
                    risk_dollars=pos.risk_dollars,
                    R=R,
                    hours_held=(last_ts - pos.entry_ts).total_seconds() / 3600.0,
                )
                result.trades.append(trade)
                equity += pnl

        result.final_equity = equity
        result.max_drawdown = max_dd
        result.max_drawdown_pct = max_dd_pct
        return result
