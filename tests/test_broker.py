import unittest

from tradebot.broker import PaperBroker
from tradebot.config import Config


def make_config(**overrides) -> Config:
    cfg = Config(starting_cash=1000.0, fee_rate=0.01, slippage=0.0,
                 risk_per_trade=0.02, max_alloc_per_product=0.5)
    for k, v in overrides.items():
        setattr(cfg, k, v)
    cfg.validate()
    return cfg


class TestSizing(unittest.TestCase):
    def test_risk_based_size(self):
        broker = PaperBroker.new(make_config())
        # equity 1000, risk 2% = $20; entry 100, stop 90 -> risk/unit 10
        # -> qty 2 -> notional 200 (under the $500 alloc cap)
        notional = broker.size_entry(equity=1000, entry_price=100, stop=90)
        self.assertAlmostEqual(notional, 200.0)

    def test_alloc_cap(self):
        broker = PaperBroker.new(make_config())
        # tight stop would size huge; capped at 50% of equity
        notional = broker.size_entry(equity=1000, entry_price=100, stop=99.5)
        self.assertAlmostEqual(notional, 500.0)

    def test_cash_cap_keeps_cash_nonnegative(self):
        cfg = make_config(max_alloc_per_product=1.0)
        broker = PaperBroker.new(cfg)
        broker.cash = 100.0
        notional = broker.size_entry(equity=1000, entry_price=100, stop=99.9)
        self.assertLessEqual(notional * (1 + cfg.fee_rate), 100.0)

    def test_invalid_stop(self):
        broker = PaperBroker.new(make_config())
        self.assertEqual(broker.size_entry(1000, 100, 100), 0.0)
        self.assertEqual(broker.size_entry(1000, 100, 110), 0.0)


class TestFills(unittest.TestCase):
    def test_round_trip_pnl_and_fees(self):
        broker = PaperBroker.new(make_config())
        pos = broker.open_position("BTC-USD", 100.0, 0, stop=90.0, equity=1000.0)
        self.assertIsNotNone(pos)
        # notional 200, fee 2 -> cash 1000 - 202 = 798
        self.assertAlmostEqual(broker.cash, 798.0)
        self.assertAlmostEqual(pos.qty, 2.0)

        fill = broker.close_position("BTC-USD", 110.0)
        # proceeds 220, fee 2.20 -> cash 798 + 217.80 = 1015.80
        self.assertAlmostEqual(broker.cash, 1015.80)
        # pnl = 220 - 2.20 - 200 - 2 = 15.80
        self.assertAlmostEqual(fill["pnl"], 15.80)
        self.assertEqual(broker.n_trades, 1)
        self.assertEqual(broker.n_wins, 1)
        self.assertNotIn("BTC-USD", broker.positions)

    def test_slippage_applied(self):
        cfg = make_config(slippage=0.01, fee_rate=0.0)
        broker = PaperBroker.new(cfg)
        self.assertAlmostEqual(broker.buy_price(100.0), 101.0)
        self.assertAlmostEqual(broker.sell_price(100.0), 99.0)

    def test_tiny_size_rejected(self):
        broker = PaperBroker.new(make_config())
        # equity so small the notional falls below the $10 exchange minimum
        pos = broker.open_position("BTC-USD", 100.0, 0, stop=99.0, equity=4.0)
        self.assertIsNone(pos)
        self.assertAlmostEqual(broker.cash, 1000.0)  # untouched


class TestPersistence(unittest.TestCase):
    def test_round_trip(self):
        cfg = make_config()
        broker = PaperBroker.new(cfg)
        broker.open_position("ETH-USD", 50.0, 123, stop=45.0, equity=1000.0)
        restored = PaperBroker.from_dict(cfg, broker.to_dict())
        self.assertAlmostEqual(restored.cash, broker.cash)
        self.assertIn("ETH-USD", restored.positions)
        self.assertAlmostEqual(restored.positions["ETH-USD"].qty,
                               broker.positions["ETH-USD"].qty)


class TestEquityAndDrawdown(unittest.TestCase):
    def test_equity_marks_positions(self):
        broker = PaperBroker.new(make_config())
        broker.open_position("BTC-USD", 100.0, 0, stop=90.0, equity=1000.0)
        # cash 798 + 2 units * 120 = 1038
        self.assertAlmostEqual(broker.equity({"BTC-USD": 120.0}), 1038.0)

    def test_drawdown_tracks_peak(self):
        broker = PaperBroker.new(make_config())
        broker.open_position("BTC-USD", 100.0, 0, stop=90.0, equity=1000.0)
        self.assertAlmostEqual(broker.drawdown({"BTC-USD": 120.0}), 0.0)
        dd = broker.drawdown({"BTC-USD": 80.0})  # equity 798 + 160 = 958
        self.assertAlmostEqual(dd, 1 - 958.0 / 1038.0)


if __name__ == "__main__":
    unittest.main()
