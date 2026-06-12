"""Backtester. Runs the exact same Strategy against historical candles.

Execution model (conservative, no lookahead): a decision made on the close
of candle i is filled at the OPEN of candle i+1, plus slippage and fees.
The final candle therefore never generates a fill.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .broker import PaperBroker
from .config import Config
from .data import Candle, get_candles
from .strategy import ENTER, EXIT, HOLD, PositionState, Strategy


@dataclass
class BacktestResult:
    start: int
    end: int
    starting_cash: float
    final_equity: float
    buy_hold_equity: float
    n_trades: int
    n_wins: int
    total_fees: float
    max_drawdown: float
    buy_hold_max_drawdown: float
    sharpe: float
    cagr: float
    equity_curve: list[tuple[int, float]] = field(default_factory=list)
    trades: list[dict] = field(default_factory=list)

    def summary(self) -> str:
        years = max((self.end - self.start) / (365 * 86400), 1e-9)
        bh_ret = self.buy_hold_equity / self.starting_cash - 1
        ret = self.final_equity / self.starting_cash - 1
        win_rate = self.n_wins / self.n_trades if self.n_trades else 0.0
        fmt_date = lambda t: datetime.fromtimestamp(t, timezone.utc).date()
        return "\n".join([
            f"Period:            {fmt_date(self.start)} .. {fmt_date(self.end)} "
            f"({years:.2f} years)",
            f"Starting cash:     ${self.starting_cash:,.2f}",
            f"Final equity:      ${self.final_equity:,.2f}  ({ret:+.1%} total, "
            f"{self.cagr:+.1%} CAGR)",
            f"Buy & hold:        ${self.buy_hold_equity:,.2f}  ({bh_ret:+.1%} total)",
            f"Max drawdown:      {self.max_drawdown:.1%}  "
            f"(buy & hold: {self.buy_hold_max_drawdown:.1%})",
            f"Sharpe (ann.):     {self.sharpe:.2f}",
            f"Trades:            {self.n_trades}  (win rate {win_rate:.0%})",
            f"Fees+slippage pd:  ${self.total_fees:,.2f} in fees",
        ])


def run_backtest(cfg: Config, candles_by_product: dict[str, list[Candle]]) -> BacktestResult:
    strategy = Strategy(cfg)
    broker = PaperBroker.new(cfg)
    warmup = strategy.min_history()

    by_time = {p: {c.time: c for c in cs} for p, cs in candles_by_product.items()}
    timeline = sorted(set.intersection(*(set(m) for m in by_time.values())))
    if len(timeline) <= warmup + 1:
        raise ValueError(f"not enough history: {len(timeline)} candles, "
                         f"need > {warmup + 1}")
    series = {p: [by_time[p][t] for t in timeline] for p in candles_by_product}

    pending: dict[str, tuple[str, float | None]] = {}  # product -> (action, stop)
    equity_curve: list[tuple[int, float]] = []
    trades: list[dict] = []
    max_dd = 0.0

    for i in range(len(timeline)):
        # 1) Fill orders decided on the previous close, at this candle's open.
        for product, (action, stop) in list(pending.items()):
            candle = series[product][i]
            if action == ENTER and product not in broker.positions:
                opens = {p: series[p][i].open for p in series}
                eq = broker.equity(opens)
                dd = broker.drawdown(opens)
                if dd <= cfg.max_drawdown_halt:
                    pos = broker.open_position(product, candle.open, candle.time,
                                               stop=stop, equity=eq)
                    if pos:
                        trades.append({"timestamp": candle.time, "product": product,
                                       "side": "buy", "qty": pos.qty,
                                       "price": pos.entry_price})
            elif action == EXIT and product in broker.positions:
                fill = broker.close_position(product, candle.open)
                trades.append({"timestamp": candle.time, "product": product,
                               "side": "sell", **fill})
        pending.clear()

        # 2) Decide on this candle's close (skip the final candle: a decision
        #    there could never be filled).
        if i >= warmup and i < len(timeline) - 1:
            for product in series:
                bpos = broker.positions.get(product)
                pos_state = (PositionState(stop=bpos.stop,
                                           highest_close=bpos.highest_close)
                             if bpos else None)
                decision = strategy.evaluate(series[product], i, pos_state)
                if decision.action == ENTER:
                    pending[product] = (ENTER, decision.initial_stop)
                elif decision.action == EXIT:
                    pending[product] = (EXIT, None)
                elif decision.action == HOLD and bpos:
                    bpos.stop = decision.new_stop
                    bpos.highest_close = max(bpos.highest_close,
                                             series[product][i].close)

        closes = {p: series[p][i].close for p in series}
        equity_curve.append((timeline[i], broker.equity(closes)))
        max_dd = max(max_dd, broker.drawdown(closes))

    # Mark any open positions to the final close (positions stay open;
    # equity already reflects them).
    final_equity = equity_curve[-1][1]

    # Buy & hold benchmark: equal split at the first decidable candle's open,
    # same fees and slippage on entry, marked to the final close.
    first = warmup + 1
    per_leg = cfg.starting_cash / len(series)
    bh_qty = {}
    for p in series:
        entry = series[p][first].open * (1 + cfg.slippage)
        bh_qty[p] = per_leg * (1 - cfg.fee_rate) / entry
    bh_value = sum(bh_qty[p] * series[p][-1].close for p in series)
    bh_peak, bh_max_dd = 0.0, 0.0
    for i in range(first, len(timeline)):
        value = sum(bh_qty[p] * series[p][i].close for p in series)
        bh_peak = max(bh_peak, value)
        bh_max_dd = max(bh_max_dd, 1 - value / bh_peak)

    returns = []
    for j in range(1, len(equity_curve)):
        prev, cur = equity_curve[j - 1][1], equity_curve[j][1]
        if prev > 0:
            returns.append(cur / prev - 1)
    sharpe = 0.0
    if len(returns) > 2:
        mean = sum(returns) / len(returns)
        var = sum((r - mean) ** 2 for r in returns) / (len(returns) - 1)
        std = math.sqrt(var)
        periods_per_year = 365 * 86400 / cfg.granularity
        if std > 0:
            sharpe = mean / std * math.sqrt(periods_per_year)

    years = max((timeline[-1] - timeline[0]) / (365 * 86400), 1e-9)
    cagr = (final_equity / cfg.starting_cash) ** (1 / years) - 1

    return BacktestResult(
        start=timeline[0], end=timeline[-1], starting_cash=cfg.starting_cash,
        final_equity=final_equity, buy_hold_equity=bh_value,
        n_trades=broker.n_trades, n_wins=broker.n_wins,
        total_fees=broker.total_fees, max_drawdown=max_dd,
        buy_hold_max_drawdown=bh_max_dd, sharpe=sharpe,
        cagr=cagr, equity_curve=equity_curve, trades=trades,
    )


def fetch_history(cfg: Config, days: int) -> dict[str, list[Candle]]:
    end = int(time.time()) // cfg.granularity * cfg.granularity - cfg.granularity
    start = end - days * 86400
    return {p: get_candles(p, cfg.granularity, start, end) for p in cfg.products}
