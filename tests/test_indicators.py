import unittest

from tradebot.data import Candle
from tradebot.indicators import atr, crossed_above, crossed_below, ema


def make_candle(t, o, h, l, c):
    return Candle(time=t, open=o, high=h, low=l, close=c, volume=1.0)


class TestEMA(unittest.TestCase):
    def test_too_short(self):
        self.assertEqual(ema([1.0, 2.0], 3), [None, None])

    def test_known_values(self):
        # period 3 -> k = 0.5, seeded with SMA(1,2,3) = 2.
        # ema then equals the index for this arithmetic sequence:
        # ema[3] = 4*0.5 + 2*0.5 = 3, ema[4] = 5*0.5 + 3*0.5 = 4, ...
        values = [float(v) for v in range(1, 11)]
        result = ema(values, 3)
        self.assertEqual(result[:2], [None, None])
        for i in range(2, 10):
            self.assertAlmostEqual(result[i], float(i))

    def test_constant_series(self):
        result = ema([5.0] * 20, 7)
        for v in result[6:]:
            self.assertAlmostEqual(v, 5.0)


class TestATR(unittest.TestCase):
    def test_known_values(self):
        # Constant 2-point ranges, closes inside the range: every TR is 2.
        candles = [make_candle(i, 10, 11, 9, 10) for i in range(10)]
        result = atr(candles, 3)
        self.assertEqual(result[:3], [None, None, None])
        for v in result[3:]:
            self.assertAlmostEqual(v, 2.0)

    def test_gap_uses_prev_close(self):
        # Second candle gaps up: TR = high - prev_close = 20 - 10 = 10.
        candles = [
            make_candle(0, 10, 11, 9, 10),
            make_candle(1, 19, 20, 18.5, 19),  # plain range would be only 1.5
            make_candle(2, 19, 20, 18, 19),
        ]
        result = atr(candles, 2)
        # seed = mean(TR1, TR2) = mean(10, 2) = 6
        self.assertAlmostEqual(result[2], 6.0)

    def test_too_short(self):
        candles = [make_candle(i, 10, 11, 9, 10) for i in range(3)]
        self.assertEqual(atr(candles, 3), [None, None, None])


class TestCrossovers(unittest.TestCase):
    def test_crossed_above(self):
        fast = [None, 1.0, 3.0]
        slow = [None, 2.0, 2.0]
        self.assertTrue(crossed_above(fast, slow, 2))
        self.assertFalse(crossed_above(fast, slow, 1))  # prior value is None
        self.assertFalse(crossed_below(fast, slow, 2))

    def test_crossed_below(self):
        fast = [3.0, 1.0]
        slow = [2.0, 2.0]
        self.assertTrue(crossed_below(fast, slow, 1))
        self.assertFalse(crossed_above(fast, slow, 1))

    def test_touch_then_cross(self):
        # Equality counts as "not yet above", so the move off equality crosses.
        fast = [2.0, 2.5]
        slow = [2.0, 2.0]
        self.assertTrue(crossed_above(fast, slow, 1))


if __name__ == "__main__":
    unittest.main()
