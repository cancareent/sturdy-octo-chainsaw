"""Technical indicators. Pure functions over candle lists.

Each function returns a list aligned with the input; entries are None
until enough data exists to compute the indicator.
"""

from __future__ import annotations

from .data import Candle


def ema(values: list[float], period: int) -> list[float | None]:
    """Exponential moving average, seeded with the SMA of the first `period` values."""
    if period < 1:
        raise ValueError("period must be >= 1")
    out: list[float | None] = [None] * len(values)
    if len(values) < period:
        return out
    k = 2.0 / (period + 1)
    seed = sum(values[:period]) / period
    out[period - 1] = seed
    prev = seed
    for i in range(period, len(values)):
        prev = values[i] * k + prev * (1 - k)
        out[i] = prev
    return out


def atr(candles: list[Candle], period: int) -> list[float | None]:
    """Average True Range using Wilder's smoothing."""
    if period < 1:
        raise ValueError("period must be >= 1")
    n = len(candles)
    out: list[float | None] = [None] * n
    if n < period + 1:
        return out
    true_ranges: list[float] = []
    for i in range(1, n):
        c, prev_close = candles[i], candles[i - 1].close
        tr = max(c.high - c.low, abs(c.high - prev_close), abs(c.low - prev_close))
        true_ranges.append(tr)
    # true_ranges[j] corresponds to candles[j + 1]
    seed = sum(true_ranges[:period]) / period
    out[period] = seed
    prev = seed
    for j in range(period, len(true_ranges)):
        prev = (prev * (period - 1) + true_ranges[j]) / period
        out[j + 1] = prev
    return out


def crossed_above(fast: list[float | None], slow: list[float | None], i: int) -> bool:
    """True if fast crossed above slow at index i (was <= at i-1, is > at i)."""
    if i < 1:
        return False
    f0, s0, f1, s1 = fast[i - 1], slow[i - 1], fast[i], slow[i]
    if None in (f0, s0, f1, s1):
        return False
    return f0 <= s0 and f1 > s1


def crossed_below(fast: list[float | None], slow: list[float | None], i: int) -> bool:
    if i < 1:
        return False
    f0, s0, f1, s1 = fast[i - 1], slow[i - 1], fast[i], slow[i]
    if None in (f0, s0, f1, s1):
        return False
    return f0 >= s0 and f1 < s1
