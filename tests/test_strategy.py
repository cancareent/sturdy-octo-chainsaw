import unittest

from tradebot.config import Config
from tradebot.data import Candle
from tradebot.strategy import ENTER, EXIT, HOLD, NONE, PositionState, Strategy


def make_config() -> Config:
    cfg = Config(ema_fast=2, ema_slow=4, atr_period=2, atr_stop_mult=3.0,
                 products=["TEST-USD"])
    cfg.validate()
    return cfg


def candles_from_closes(closes: list[float]) -> list[Candle]:
    out = []
    prev = closes[0]
    for i, close in enumerate(closes):
        open_ = prev
        out.append(Candle(time=i * 86400, open=open_,
                          high=max(open_, close) + 1.0,
                          low=min(open_, close) - 1.0,
                          close=close, volume=1.0))
        prev = close
    return out


def simulate(strategy: Strategy, candles: list[Candle]):
    """Walk the series like the backtester does; return decisions per index."""
    pos: PositionState | None = None
    decisions = []
    for i in range(len(candles)):
        d = strategy.evaluate(candles, i, pos)
        decisions.append(d)
        if d.action == ENTER:
            pos = PositionState(stop=d.initial_stop, highest_close=candles[i].close)
        elif d.action == EXIT:
            pos = None
        elif d.action == HOLD:
            pos.stop = d.new_stop
            pos.highest_close = max(pos.highest_close, candles[i].close)
    return decisions


class TestStrategySignals(unittest.TestCase):
    def test_enters_on_uptrend_and_exits_after_reversal(self):
        downtrend = [100 - i for i in range(10)]          # 100 .. 91
        rally = [91 + 4 * i for i in range(10)]           # 91 .. 127
        crash = [127 - 8 * i for i in range(8)]           # 127 .. 71
        closes = [float(v) for v in downtrend + rally[1:] + crash[1:]]
        candles = candles_from_closes(closes)
        decisions = simulate(Strategy(make_config()), candles)
        actions = [d.action for d in decisions]

        self.assertIn(ENTER, actions, "should enter during the rally")
        enter_i = actions.index(ENTER)
        self.assertGreaterEqual(enter_i, 10, "no entry during the downtrend")
        self.assertIn(EXIT, actions[enter_i:], "should exit after the crash")
        # Long-only, one position at a time: enter/exit strictly alternate.
        sequence = [a for a in actions if a in (ENTER, EXIT)]
        for j in range(1, len(sequence)):
            self.assertNotEqual(sequence[j], sequence[j - 1])

    def test_no_signals_without_enough_history(self):
        candles = candles_from_closes([100.0, 101.0, 102.0])
        decisions = simulate(Strategy(make_config()), candles)
        self.assertTrue(all(d.action == NONE for d in decisions))

    def test_trailing_stop_never_decreases(self):
        closes = [float(v) for v in
                  [100, 99, 98, 97, 96, 100, 104, 108, 112, 116, 120, 118, 119]]
        candles = candles_from_closes(closes)
        strategy = Strategy(make_config())
        pos: PositionState | None = None
        last_stop = None
        for i in range(len(candles)):
            d = strategy.evaluate(candles, i, pos)
            if d.action == ENTER:
                pos = PositionState(stop=d.initial_stop,
                                    highest_close=candles[i].close)
                last_stop = d.initial_stop
            elif d.action == HOLD:
                self.assertGreaterEqual(d.new_stop, last_stop)
                last_stop = d.new_stop
                pos.stop = d.new_stop
                pos.highest_close = max(pos.highest_close, candles[i].close)
            elif d.action == EXIT:
                pos = None

    def test_stop_exit_uses_pre_update_stop(self):
        cfg = make_config()
        strategy = Strategy(cfg)
        candles = candles_from_closes([float(v) for v in
                                       [100, 101, 102, 103, 104, 105, 50]])
        pos = PositionState(stop=95.0, highest_close=105.0)
        d = strategy.evaluate(candles, len(candles) - 1, pos)
        self.assertEqual(d.action, EXIT)
        self.assertIn("below stop", d.reason)


class TestStrategyAgainstBacktester(unittest.TestCase):
    def test_backtest_runs_on_synthetic_data(self):
        from tradebot.backtest import run_backtest
        cfg = make_config()
        # A noisy but rising series long enough to trade.
        closes = [100.0]
        for i in range(120):
            closes.append(closes[-1] * (1.02 if i % 3 else 0.985))
        candles = candles_from_closes(closes)
        result = run_backtest(cfg, {"TEST-USD": candles})
        self.assertGreater(result.final_equity, 0)
        self.assertEqual(len(result.equity_curve), len(candles))
        # Fills happen at the open of the candle AFTER the signal: every
        # trade timestamp must be strictly later than the warmup candle.
        warmup = Strategy(cfg).min_history()
        for trade in result.trades:
            self.assertGreater(trade["timestamp"], candles[warmup].time)


if __name__ == "__main__":
    unittest.main()
