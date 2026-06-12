"""Market data client for the Coinbase Exchange public API.

Uses only public, unauthenticated endpoints — no API keys required.
Docs: https://docs.cdp.coinbase.com/exchange/reference/exchangerestapi_getproductcandles
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

log = logging.getLogger(__name__)

BASE_URL = "https://api.exchange.coinbase.com"
MAX_CANDLES_PER_REQUEST = 300
USER_AGENT = "paper-tradebot/0.1 (educational paper trading)"


@dataclass(frozen=True)
class Candle:
    time: int  # unix epoch seconds, start of the candle
    open: float
    high: float
    low: float
    close: float
    volume: float


class DataError(Exception):
    pass


def _http_get_json(url: str, retries: int = 4, timeout: int = 20):
    delay = 2.0
    last_err: Exception | None = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode())
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as err:
            last_err = err
            if attempt < retries:
                log.warning("request failed (%s), retrying in %.0fs: %s", url, delay, err)
                time.sleep(delay)
                delay *= 2
    raise DataError(f"request failed after {retries + 1} attempts: {url}: {last_err}")


def _parse_candles(raw) -> list[Candle]:
    if not isinstance(raw, list):
        raise DataError(f"unexpected candles response: {raw!r}")
    candles = []
    for row in raw:
        # Coinbase order: [time, low, high, open, close, volume]
        t, low, high, open_, close, volume = row
        candles.append(Candle(int(t), float(open_), float(high), float(low),
                              float(close), float(volume)))
    candles.sort(key=lambda c: c.time)  # API returns newest first
    return candles


def get_candles(product: str, granularity: int, start: int, end: int) -> list[Candle]:
    """Fetch candles for [start, end] epoch seconds, paginating as needed."""
    out: list[Candle] = []
    span = granularity * MAX_CANDLES_PER_REQUEST
    window_start = start
    while window_start <= end:
        window_end = min(window_start + span - granularity, end)
        params = urllib.parse.urlencode({
            "granularity": granularity,
            "start": window_start,
            "end": window_end,
        })
        url = f"{BASE_URL}/products/{product}/candles?{params}"
        out.extend(_parse_candles(_http_get_json(url)))
        window_start = window_end + granularity
        time.sleep(0.25)  # stay well under public rate limits
    # De-duplicate (windows can overlap at edges) and sort.
    unique = {c.time: c for c in out}
    return [unique[t] for t in sorted(unique)]


def get_recent_closed_candles(product: str, granularity: int, count: int,
                              now: int | None = None) -> list[Candle]:
    """Fetch the most recent `count` candles that are fully closed.

    A candle starting at T with granularity G is closed once now >= T + G.
    The in-progress candle is excluded so signals never use partial data.
    """
    now = int(time.time()) if now is None else now
    last_closed_start = ((now - granularity) // granularity) * granularity
    start = last_closed_start - (count - 1) * granularity
    candles = get_candles(product, granularity, start, last_closed_start)
    return [c for c in candles if c.time + granularity <= now]


def get_spot_price(product: str) -> float:
    data = _http_get_json(f"{BASE_URL}/products/{product}/ticker")
    try:
        return float(data["price"])
    except (KeyError, TypeError, ValueError) as err:
        raise DataError(f"unexpected ticker response for {product}: {data!r}") from err
