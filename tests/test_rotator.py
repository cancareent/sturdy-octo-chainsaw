import unittest

from tradebot.config import Config
from tradebot.data import Candle
from tradebot.rotator import (RotatorState, SECONDS_PER_YEAR, accrue,
                              run_rotator_backtest)


def make_config(**overrides) -> Config:
    cfg = Config(ema_fast=2, ema_slow=4, atr_period=2, atr_stop_mult=3.0,
                 rotator_cash=400.0, staking_apy=0.065, stable_apy=0.05,
                 rotation_cost=0.0025, slippage=0.0)
    for k, v in overrides.items():
        setattr(cfg, k, v)
    cfg.validate()
    return cfg


def candles_from_closes(closes):
    out, prev = [], closes[0]
    for i, close in enumerate(closes):
        out.append(Candle(time=i * 86400, open=prev,
                          high=max(prev, close) + 1.0,
                          low=min(prev, close) - 1.0, close=close, volume=1.0))
        prev = close
    return out


class TestAccrual(unittest.TestCase):
    def test_one_year_compounds_to_apy(self):
        self.assertAlmostEqual(accrue(100.0, 0.065, SECONDS_PER_YEAR), 106.5)

    def test_daily_amount_roughly_apy_over_365(self):
        daily = accrue(400.0, 0.05, 86400) - 400.0
        self.assertAlmostEqual(daily, 400 * 0.05 / 365, delta=0.002)

    def test_zero_elapsed_or_zero_apy(self):
        self.assertEqual(accrue(100.0, 0.05, 0), 100.0)
        self.assertEqual(accrue(100.0, 0.0, 86400), 100.0)


class TestRotationAccounting(unittest.TestCase):
    def test_round_trip_pays_cost_both_ways(self):
        cfg = make_config()
        state = RotatorState(mode="usd", usd=400.0, last_accrual=0)
        state.to_sol(100.0, cfg, stop=90.0)
        self.assertAlmostEqual(state.sol, 400 * 0.9975 / 100)
        self.assertEqual(state.usd, 0.0)
        state.to_usd(100.0, cfg)  # flat price: lose exactly two rotation costs
        self.assertAlmostEqual(state.usd, 400 * 0.9975 * 0.9975)
        self.assertEqual(state.rotations, 2)

    def test_yield_accrues_in_current_mode_only(self):
        cfg = make_config()
        state = RotatorState(mode="sol", sol=4.0, last_accrual=0)
        state.accrue_to(SECONDS_PER_YEAR, cfg, sol_price=100.0)
        self.assertAlmostEqual(state.sol, 4.0 * 1.065)
        self.assertAlmostEqual(state.yield_earned_usd, 4.0 * 0.065 * 100.0)
        self.assertEqual(state.usd, 0.0)


class TestRotatorBacktest(unittest.TestCase):
    def test_flat_market_earns_stable_yield_only(self):
        cfg = make_config()
        candles = candles_from_closes([100.0] * 200)
        result = run_rotator_backtest(cfg, candles)
        self.assertEqual(result.rotations, 0)
        expected = accrue(400.0, cfg.stable_apy, candles[-1].time)
        self.assertAlmostEqual(result.final_equity, expected, places=4)

    def test_uptrend_rotates_in_and_tracks_sol(self):
        closes = [100.0 - i * 0.5 for i in range(20)]
        closes += [closes[-1] * 1.03 ** i for i in range(1, 100)]
        cfg = make_config()
        result = run_rotator_backtest(cfg, candles_from_closes(closes))
        self.assertGreaterEqual(result.rotations, 1)
        self.assertGreater(result.final_equity, 400.0)

    def test_crash_protection_beats_holding(self):
        # Rally then a deep crash: the rotator should exit and end above
        # buy-and-hold.
        closes = [100.0 + 2 * i for i in range(60)]          # 100 -> 218
        closes += [closes[-1] * 0.97 ** i for i in range(1, 80)]  # -91%
        cfg = make_config()
        result = run_rotator_backtest(cfg, candles_from_closes(closes))
        self.assertGreater(result.final_equity, result.hold_sol_equity)
        self.assertLess(result.max_drawdown, result.hold_sol_max_drawdown)

    def test_staked_benchmark_exceeds_plain_hold(self):
        closes = [100.0 + i for i in range(200)]
        result = run_rotator_backtest(make_config(), candles_from_closes(closes))
        self.assertGreater(result.staked_sol_equity, result.hold_sol_equity)


if __name__ == "__main__":
    unittest.main()
