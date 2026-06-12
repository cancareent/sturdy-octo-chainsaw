"""Staked Trend Rotator (paper).

Two states, always earning yield:
  - RISK_ON:  capital held as staked SOL, accruing `staking_apy` in SOL terms
  - RISK_OFF: capital held as USDC, accruing `stable_apy`

The trend engine (Strategy: EMA crossover + ATR trailing stop on daily
SOL-USD closes) decides the state. Rotations pay `rotation_cost` one-way.
A decision on candle i's close fills at candle i+1's open (no lookahead).
"""

from __future__ import annotations

import json
import logging
import math
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .broker import append_trade_log
from .config import Config
from .data import Candle, DataError, get_candles, get_recent_closed_candles, get_spot_price
from .strategy import ENTER, EXIT, HOLD, PositionState, Strategy

log = logging.getLogger(__name__)

SECONDS_PER_YEAR = 365 * 86400


def accrue(amount: float, apy: float, seconds: float) -> float:
    """Compound `amount` at `apy` for `seconds` (continuous daily compounding)."""
    if seconds <= 0 or apy <= 0:
        return amount
    return amount * (1 + apy) ** (seconds / SECONDS_PER_YEAR)


@dataclass
class RotatorState:
    mode: str = "usd"            # "usd" or "sol"
    usd: float = 0.0             # USDC balance when mode == "usd"
    sol: float = 0.0             # staked-SOL units when mode == "sol"
    stop: float = 0.0            # trailing stop (USD per SOL) when in SOL
    highest_close: float = 0.0
    last_accrual: int = 0        # epoch seconds of last yield accrual
    last_processed: int = 0      # newest closed candle already acted on
    rotations: int = 0
    yield_earned_usd: float = 0.0  # cumulative yield, valued at accrual time

    def accrue_to(self, now: int, cfg: Config, sol_price: float) -> None:
        dt = now - self.last_accrual
        if dt <= 0:
            return
        if self.mode == "sol":
            new_sol = accrue(self.sol, cfg.staking_apy, dt)
            self.yield_earned_usd += (new_sol - self.sol) * sol_price
            self.sol = new_sol
        else:
            new_usd = accrue(self.usd, cfg.stable_apy, dt)
            self.yield_earned_usd += new_usd - self.usd
            self.usd = new_usd
        self.last_accrual = now

    def equity(self, sol_price: float) -> float:
        return self.sol * sol_price if self.mode == "sol" else self.usd

    def to_sol(self, price: float, cfg: Config, stop: float) -> None:
        assert self.mode == "usd"
        self.sol = self.usd * (1 - cfg.rotation_cost) / price
        self.usd = 0.0
        self.mode = "sol"
        self.stop = stop
        self.highest_close = price
        self.rotations += 1

    def to_usd(self, price: float, cfg: Config) -> None:
        assert self.mode == "sol"
        self.usd = self.sol * price * (1 - cfg.rotation_cost)
        self.sol = 0.0
        self.mode = "usd"
        self.stop = 0.0
        self.rotations += 1


# ---------------------------------------------------------------------------
# Backtest
# ---------------------------------------------------------------------------

@dataclass
class RotatorBacktestResult:
    start: int
    end: int
    starting_cash: float
    final_equity: float
    hold_sol_equity: float
    staked_sol_equity: float
    rotations: int
    yield_earned_usd: float
    max_drawdown: float
    hold_sol_max_drawdown: float
    cagr: float
    equity_curve: list[tuple[int, float]] = field(default_factory=list)

    def summary(self) -> str:
        years = max((self.end - self.start) / SECONDS_PER_YEAR, 1e-9)
        fmt_date = lambda t: datetime.fromtimestamp(t, timezone.utc).date()
        rel = lambda v: v / self.starting_cash - 1
        return "\n".join([
            f"Period:             {fmt_date(self.start)} .. {fmt_date(self.end)} "
            f"({years:.2f} years)",
            f"Starting cash:      ${self.starting_cash:,.2f}",
            f"Rotator:            ${self.final_equity:,.2f}  "
            f"({rel(self.final_equity):+.1%} total, {self.cagr:+.1%} CAGR)",
            f"  max drawdown:     {self.max_drawdown:.1%}",
            f"  rotations:        {self.rotations}",
            f"  yield collected:  ${self.yield_earned_usd:,.2f}",
            f"Hold SOL:           ${self.hold_sol_equity:,.2f}  "
            f"({rel(self.hold_sol_equity):+.1%} total, "
            f"max drawdown {self.hold_sol_max_drawdown:.1%})",
            f"Hold staked SOL:    ${self.staked_sol_equity:,.2f}  "
            f"({rel(self.staked_sol_equity):+.1%} total)",
        ])


def run_rotator_backtest(cfg: Config, candles: list[Candle]) -> RotatorBacktestResult:
    strategy = Strategy(cfg)
    warmup = strategy.min_history()
    if len(candles) <= warmup + 1:
        raise ValueError(f"not enough history: {len(candles)}, need > {warmup + 1}")

    state = RotatorState(mode="usd", usd=cfg.rotator_cash,
                         last_accrual=candles[0].time)
    pending: str | None = None
    pending_stop = 0.0
    equity_curve: list[tuple[int, float]] = []
    peak = max_dd = 0.0

    for i, candle in enumerate(candles):
        # Yield accrues up to this candle's open, then orders fill at the open.
        state.accrue_to(candle.time, cfg, candle.open)
        if pending == ENTER and state.mode == "usd":
            price = candle.open * (1 + cfg.slippage)
            state.to_sol(price, cfg, stop=pending_stop)
        elif pending == EXIT and state.mode == "sol":
            price = candle.open * (1 - cfg.slippage)
            state.to_usd(price, cfg)
        pending = None

        if warmup <= i < len(candles) - 1:
            pos = (PositionState(stop=state.stop, highest_close=state.highest_close)
                   if state.mode == "sol" else None)
            decision = strategy.evaluate(candles, i, pos)
            if decision.action == ENTER:
                pending, pending_stop = ENTER, decision.initial_stop
            elif decision.action == EXIT:
                pending = EXIT
            elif decision.action == HOLD:
                state.stop = decision.new_stop
                state.highest_close = max(state.highest_close, candle.close)

        eq = state.equity(candle.close)
        equity_curve.append((candle.time, eq))
        peak = max(peak, eq)
        max_dd = max(max_dd, 1 - eq / peak)

    # Benchmarks from the first candle the rotator could have acted on.
    first = warmup + 1
    entry = candles[first].open * (1 + cfg.slippage)
    hold_qty = cfg.rotator_cash * (1 - cfg.rotation_cost) / entry
    hold_final = hold_qty * candles[-1].close
    bh_peak = bh_dd = 0.0
    for c in candles[first:]:
        v = hold_qty * c.close
        bh_peak = max(bh_peak, v)
        bh_dd = max(bh_dd, 1 - v / bh_peak)
    staked_seconds = candles[-1].time - candles[first].time
    staked_final = accrue(hold_qty, cfg.staking_apy, staked_seconds) * candles[-1].close

    final = equity_curve[-1][1]
    years = max((candles[-1].time - candles[0].time) / SECONDS_PER_YEAR, 1e-9)
    cagr = (final / cfg.rotator_cash) ** (1 / years) - 1
    return RotatorBacktestResult(
        start=candles[0].time, end=candles[-1].time,
        starting_cash=cfg.rotator_cash, final_equity=final,
        hold_sol_equity=hold_final, staked_sol_equity=staked_final,
        rotations=state.rotations, yield_earned_usd=state.yield_earned_usd,
        max_drawdown=max_dd, hold_sol_max_drawdown=bh_dd, cagr=cagr,
        equity_curve=equity_curve,
    )


def fetch_rotator_history(cfg: Config, days: int) -> list[Candle]:
    end = int(time.time()) // 86400 * 86400 - 86400
    return get_candles(cfg.rotator_product, 86400, end - days * 86400, end)


# ---------------------------------------------------------------------------
# Live paper runner
# ---------------------------------------------------------------------------

STATE_FILE = "rotator_state.json"


def _load(cfg: Config) -> RotatorState:
    path = Path(cfg.state_dir) / STATE_FILE
    if path.exists():
        state = RotatorState(**json.loads(path.read_text()))
        log.info("resumed rotator: mode=%s equity-ish usd=%.2f sol=%.6f",
                 state.mode, state.usd, state.sol)
        return state
    now = int(time.time())
    state = RotatorState(mode="usd", usd=cfg.rotator_cash, last_accrual=now)
    _save(cfg, state)
    log.info("fresh rotator account: $%.2f in USDC", cfg.rotator_cash)
    return state


def _save(cfg: Config, state: RotatorState) -> None:
    path = Path(cfg.state_dir) / STATE_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(vars(state), indent=2))
    tmp.replace(path)


class RotatorRunner:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.strategy = Strategy(cfg)
        self.state = _load(cfg)

    def check_once(self) -> bool:
        cfg, state = self.cfg, self.state
        try:
            candles = get_recent_closed_candles(cfg.rotator_product, 86400,
                                                self.strategy.min_history() + 5)
            spot = get_spot_price(cfg.rotator_product)
        except DataError as err:
            log.error("data fetch failed: %s", err)
            return False
        now = int(time.time())
        state.accrue_to(now, cfg, spot)
        latest = candles[-1]
        if latest.time <= state.last_processed:
            _save(cfg, state)  # persist accrual even when no new candle
            return False

        pos = (PositionState(stop=state.stop, highest_close=state.highest_close)
               if state.mode == "sol" else None)
        decision = self.strategy.evaluate(candles, len(candles) - 1, pos)
        log.info("closed candle %s close=%.2f decision=%s %s",
                 datetime.fromtimestamp(latest.time, timezone.utc).date(),
                 latest.close, decision.action, decision.reason)
        if decision.action == ENTER and state.mode == "usd":
            price = spot * (1 + cfg.slippage)
            state.to_sol(price, cfg, stop=decision.initial_stop)
            log.info("ROTATE -> staked SOL: %.6f SOL @ $%.2f (stop $%.2f)",
                     state.sol, price, state.stop)
            append_trade_log(cfg.state_dir, {
                "timestamp": now, "product": cfg.rotator_product, "side": "buy",
                "qty": state.sol, "price": price, "cash_after": 0.0,
                "reason": f"rotator: {decision.reason}"})
        elif decision.action == EXIT and state.mode == "sol":
            price = spot * (1 - cfg.slippage)
            state.to_usd(price, cfg)
            log.info("ROTATE -> USDC: $%.2f @ $%.2f", state.usd, price)
            append_trade_log(cfg.state_dir, {
                "timestamp": now, "product": cfg.rotator_product, "side": "sell",
                "qty": 0.0, "price": price, "cash_after": state.usd,
                "reason": f"rotator: {decision.reason}"})
        elif decision.action == HOLD:
            state.stop = decision.new_stop
            state.highest_close = max(state.highest_close, latest.close)
        state.last_processed = latest.time
        _save(cfg, state)
        return True

    def run_forever(self) -> None:
        stop_file = Path(self.cfg.state_dir) / "STOP"
        log.info("rotator running on %s every %ds (touch %s to halt)",
                 self.cfg.rotator_product, self.cfg.poll_seconds, stop_file)
        while True:
            if stop_file.exists():
                log.info("STOP file found, halting.")
                return
            try:
                self.check_once()
            except Exception:
                log.exception("poll cycle failed; will retry")
            time.sleep(self.cfg.poll_seconds)

    def status(self) -> str:
        state = self.state
        try:
            spot = get_spot_price(self.cfg.rotator_product)
        except DataError:
            spot = state.highest_close or 0.0
        eq = state.equity(spot)
        held = (f"{state.sol:.6f} staked SOL (stop ${state.stop:,.2f})"
                if state.mode == "sol" else f"${state.usd:,.2f} USDC")
        daily_yield = eq * ((1 + (self.cfg.staking_apy if state.mode == "sol"
                                  else self.cfg.stable_apy)) ** (1 / 365) - 1)
        return "\n".join([
            f"Mode:        {'RISK ON (staked SOL)' if state.mode == 'sol' else 'RISK OFF (USDC)'}",
            f"Holding:     {held}",
            f"Equity:      ${eq:,.2f}  ({eq / self.cfg.rotator_cash - 1:+.2%} vs start)",
            f"Yield:       ${state.yield_earned_usd:,.4f} collected, "
            f"~${daily_yield:,.4f}/day at current mode",
            f"Rotations:   {state.rotations}",
        ])
