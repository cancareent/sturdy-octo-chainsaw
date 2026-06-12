"""Trend-following strategy: long-only EMA crossover with ATR trailing stop.

Rules (evaluated only on fully closed candles):
  - Enter long when EMA(fast) crosses above EMA(slow).
  - Exit when EMA(fast) crosses below EMA(slow), or when the close falls
    below the trailing stop (highest close since entry minus k * ATR).

The same evaluate() is used by the backtester and the live paper runner,
so backtest results reflect exactly the logic that runs live.
"""

from __future__ import annotations

from dataclasses import dataclass

from .config import Config
from .data import Candle
from .indicators import atr, crossed_above, crossed_below, ema

ENTER = "enter"
EXIT = "exit"
HOLD = "hold"
NONE = "none"


@dataclass
class PositionState:
    stop: float
    highest_close: float


@dataclass
class Decision:
    action: str  # ENTER, EXIT, HOLD (in position, no exit) or NONE (flat)
    reason: str = ""
    initial_stop: float | None = None   # set when action == ENTER
    new_stop: float | None = None       # updated trailing stop when HOLD


class Strategy:
    def __init__(self, cfg: Config):
        self.cfg = cfg

    def min_history(self) -> int:
        return max(self.cfg.ema_slow, self.cfg.atr_period + 1) + 1

    def evaluate(self, candles: list[Candle], i: int,
                 pos: PositionState | None) -> Decision:
        """Decide based on candle i (which must be a closed candle)."""
        closes = [c.close for c in candles]
        fast = ema(closes, self.cfg.ema_fast)
        slow = ema(closes, self.cfg.ema_slow)
        atr_vals = atr(candles, self.cfg.atr_period)
        close = closes[i]

        if pos is None:
            if crossed_above(fast, slow, i) and atr_vals[i] is not None:
                stop = close - self.cfg.atr_stop_mult * atr_vals[i]
                if stop <= 0:
                    return Decision(NONE, "stop would be non-positive")
                return Decision(ENTER, f"EMA{self.cfg.ema_fast} crossed above "
                                       f"EMA{self.cfg.ema_slow}", initial_stop=stop)
            return Decision(NONE)

        # In a position: stop is checked against the previous stop level
        # before the trailing update, so a candle can't raise its own stop
        # and then trigger it.
        if close < pos.stop:
            return Decision(EXIT, f"close {close:.2f} below stop {pos.stop:.2f}")
        if crossed_below(fast, slow, i):
            return Decision(EXIT, f"EMA{self.cfg.ema_fast} crossed below "
                                  f"EMA{self.cfg.ema_slow}")

        highest = max(pos.highest_close, close)
        new_stop = pos.stop
        if atr_vals[i] is not None:
            new_stop = max(pos.stop, highest - self.cfg.atr_stop_mult * atr_vals[i])
        return Decision(HOLD, new_stop=new_stop)
