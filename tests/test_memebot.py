import unittest

from tradebot.config import Config
from tradebot.memebot import (MemePosition, MemeState, PoolSnapshot,
                              decide_exit, passes_entry)


def make_config(**overrides) -> Config:
    cfg = Config()
    for k, v in overrides.items():
        setattr(cfg, k, v)
    cfg.validate()
    return cfg


def make_pool(**overrides) -> PoolSnapshot:
    base = dict(address="Pool1111", name="DOGZ / SOL", symbol="DOGZ",
                price_usd=0.001, liquidity_usd=200_000.0,
                volume24h_usd=500_000.0, change_h1=8.0, change_m5=3.0,
                vol_m5_usd=5_000.0, vol_h1_usd=30_000.0,
                buys_m5=20, sells_m5=8, created_at=1_000_000)
    base.update(overrides)
    return PoolSnapshot(**base)


NOW = 1_000_000 + 48 * 3600  # pool is 48h old by default


class TestEntryFilter(unittest.TestCase):
    def setUp(self):
        self.cfg = make_config()

    def test_accepts_qualifying_pool(self):
        self.assertTrue(passes_entry(make_pool(), self.cfg, NOW, set(), {}))

    def test_rejects_majors_held_and_cooldown(self):
        cfg = self.cfg
        self.assertFalse(passes_entry(make_pool(symbol="SOL"), cfg, NOW, set(), {}))
        self.assertFalse(passes_entry(make_pool(), cfg, NOW, {"Pool1111"}, {}))
        self.assertFalse(passes_entry(make_pool(), cfg, NOW, set(),
                                      {"Pool1111": NOW + 100}))

    def test_rejects_thin_or_quiet_pools(self):
        cfg = self.cfg
        self.assertFalse(passes_entry(make_pool(liquidity_usd=50_000), cfg,
                                      NOW, set(), {}))
        self.assertFalse(passes_entry(make_pool(volume24h_usd=10_000), cfg,
                                      NOW, set(), {}))
        self.assertFalse(passes_entry(make_pool(change_h1=1.0), cfg,
                                      NOW, set(), {}))

    def test_rejects_fresh_launches(self):
        # 2h-old pool: still in the sniper-dominated launch window
        pool = make_pool(created_at=NOW - 2 * 3600)
        self.assertFalse(passes_entry(pool, self.cfg, NOW, set(), {}))
        self.assertFalse(passes_entry(make_pool(created_at=None), self.cfg,
                                      NOW, set(), {}))

    def test_rejects_weak_fast_tape(self):
        cfg = self.cfg
        # 5-minute momentum too low
        self.assertFalse(passes_entry(make_pool(change_m5=0.5), cfg,
                                      NOW, set(), {}))
        # sellers dominate the last 5 minutes
        self.assertFalse(passes_entry(make_pool(buys_m5=6, sells_m5=10), cfg,
                                      NOW, set(), {}))
        # too few buys to mean anything
        self.assertFalse(passes_entry(make_pool(buys_m5=2, sells_m5=1), cfg,
                                      NOW, set(), {}))
        # volume decelerating: m5 pace (x12) below the hourly volume
        self.assertFalse(passes_entry(make_pool(vol_m5_usd=1_000.0,
                                                vol_h1_usd=30_000.0), cfg,
                                      NOW, set(), {}))


class TestExitRules(unittest.TestCase):
    def setUp(self):
        self.cfg = make_config()
        self.entry = dict(entry_price=0.001, entry_liquidity=200_000.0,
                          entry_time=NOW - 3600, peak_price=0.001)

    def test_holds_when_nothing_triggers(self):
        self.assertIsNone(decide_exit(**self.entry, pool=make_pool(),
                                      now=NOW, cfg=self.cfg))

    def test_stop_loss(self):
        pool = make_pool(price_usd=0.001 * 0.84)  # below 15% stop
        self.assertEqual(decide_exit(**self.entry, pool=pool, now=NOW,
                                     cfg=self.cfg), "stop loss")

    def test_trailing_stop_after_run_up(self):
        entry = dict(self.entry, peak_price=0.002)  # token doubled
        pool = make_pool(price_usd=0.002 * 0.74)    # fell >25% off the peak
        self.assertEqual(decide_exit(**entry, pool=pool, now=NOW,
                                     cfg=self.cfg), "trailing stop")

    def test_time_stop(self):
        entry = dict(self.entry, entry_time=NOW - 49 * 3600)
        self.assertEqual(decide_exit(**entry, pool=make_pool(), now=NOW,
                                     cfg=self.cfg), "time stop")

    def test_momentum_flip_takes_profit(self):
        # Up 12% from entry and the 5-minute tape just turned red.
        pool = make_pool(price_usd=0.00112, change_m5=-1.0)
        entry = dict(self.entry, peak_price=0.00112)
        self.assertEqual(decide_exit(**entry, pool=pool, now=NOW,
                                     cfg=self.cfg), "momentum flip")

    def test_momentum_flip_not_armed_below_profit_threshold(self):
        # Tape turns red but position is only +2%: hold (no flip exit).
        pool = make_pool(price_usd=0.00102, change_m5=-1.0)
        entry = dict(self.entry, peak_price=0.00102)
        self.assertIsNone(decide_exit(**entry, pool=pool, now=NOW,
                                      cfg=self.cfg))

    def test_rug_on_liquidity_collapse_or_missing_pool(self):
        pool = make_pool(liquidity_usd=200_000 * 0.25)  # -75% liquidity
        self.assertEqual(decide_exit(**self.entry, pool=pool, now=NOW,
                                     cfg=self.cfg), "RUG")
        self.assertEqual(decide_exit(**self.entry, pool=None, now=NOW,
                                     cfg=self.cfg), "RUG")


class TestAccounting(unittest.TestCase):
    def test_equity_marks_positions_at_last_price(self):
        state = MemeState(cash=100.0)
        state.positions["P"] = MemePosition(
            address="P", symbol="DOGZ", qty=1000.0, entry_price=0.1,
            entry_time=0, entry_liquidity=200_000, peak_price=0.1,
            cost_paid=1.0)
        self.assertAlmostEqual(state.equity({"P": 0.2}), 300.0)
        self.assertAlmostEqual(state.equity({}), 200.0)  # falls back to entry


if __name__ == "__main__":
    unittest.main()
