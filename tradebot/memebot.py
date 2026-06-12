"""Memecoin paper-trading EXPERIMENT.

Purpose: measure, with real market data and zero financial risk, whether a
disciplined momentum strategy on trending Solana memecoins is profitable.

This is a measurement instrument, not a profit machine. Two biases pull in
opposite directions and are deliberately documented:

  OVERSTATES returns (paper can't model): honeypots (tokens you can never
  sell), failed/blocked exits during rugs, sandwich attacks on entry,
  quote staleness on illiquid pools.
  UNDERSTATES returns: nothing known.

Therefore: if this experiment loses money on paper, the live version loses
more. If it wins on paper, that is necessary but NOT sufficient evidence.

Strategy: enter trending pools (GeckoTerminal) passing liquidity/volume/age
filters on 1h momentum; exit on stop-loss, trailing stop, time stop, or
rug detection (liquidity collapse -> position marked to zero).
"""

from __future__ import annotations

import csv
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .config import Config
from .data import DataError, _http_get_json

log = logging.getLogger(__name__)

GT_BASE = "https://api.geckoterminal.com/api/v2"
MAJORS = {"SOL", "WSOL", "USDC", "USDT", "JITOSOL", "MSOL", "BSOL", "JUPSOL",
          "CBBTC", "WBTC", "WETH", "ETH"}


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PoolSnapshot:
    address: str
    name: str
    symbol: str
    price_usd: float
    liquidity_usd: float
    volume24h_usd: float
    change_h1: float        # percent, e.g. 7.5 means +7.5%
    change_m5: float        # percent over the last 5 minutes
    vol_m5_usd: float
    vol_h1_usd: float
    buys_m5: int
    sells_m5: int
    created_at: int | None  # epoch seconds


def _parse_pool(item: dict) -> PoolSnapshot | None:
    try:
        attrs = item["attributes"]
        name = attrs.get("name") or ""
        symbol = name.split("/")[0].strip().upper()
        created = attrs.get("pool_created_at")
        created_ts = None
        if created:
            created_ts = int(datetime.fromisoformat(
                created.replace("Z", "+00:00")).timestamp())
        changes = attrs.get("price_change_percentage") or {}
        volumes = attrs.get("volume_usd") or {}
        txns_m5 = (attrs.get("transactions") or {}).get("m5") or {}
        return PoolSnapshot(
            address=attrs["address"],
            name=name,
            symbol=symbol,
            price_usd=float(attrs["base_token_price_usd"]),
            liquidity_usd=float(attrs.get("reserve_in_usd") or 0),
            volume24h_usd=float(volumes.get("h24") or 0),
            change_h1=float(changes.get("h1") or 0),
            change_m5=float(changes.get("m5") or 0),
            vol_m5_usd=float(volumes.get("m5") or 0),
            vol_h1_usd=float(volumes.get("h1") or 0),
            buys_m5=int(txns_m5.get("buys") or 0),
            sells_m5=int(txns_m5.get("sells") or 0),
            created_at=created_ts,
        )
    except (KeyError, TypeError, ValueError) as err:
        log.debug("skipping unparsable pool: %s", err)
        return None


def fetch_trending_pools() -> list[PoolSnapshot]:
    data = _http_get_json(f"{GT_BASE}/networks/solana/trending_pools?page=1")
    pools = [p for item in data.get("data", []) if (p := _parse_pool(item))]
    if not pools:
        raise DataError("no trending pools parsed")
    return pools


def fetch_pools(addresses: list[str]) -> dict[str, PoolSnapshot]:
    if not addresses:
        return {}
    joined = ",".join(addresses)
    data = _http_get_json(f"{GT_BASE}/networks/solana/pools/multi/{joined}")
    out = {}
    for item in data.get("data", []):
        pool = _parse_pool(item)
        if pool:
            out[pool.address] = pool
    return out


# ---------------------------------------------------------------------------
# Pure decision logic (unit tested)
# ---------------------------------------------------------------------------

def passes_entry(pool: PoolSnapshot, cfg: Config, now: int,
                 held: set[str], cooldown: dict[str, int]) -> bool:
    if pool.address in held:
        return False
    if pool.symbol in MAJORS or pool.price_usd <= 0:
        return False
    if now < cooldown.get(pool.address, 0):
        return False
    if pool.liquidity_usd < cfg.meme_min_liquidity_usd:
        return False
    if pool.volume24h_usd < cfg.meme_min_volume24h_usd:
        return False
    if pool.created_at is None or now - pool.created_at < cfg.meme_min_age_hours * 3600:
        return False  # the launch window belongs to bundle snipers; skip it
    if pool.change_h1 < cfg.meme_entry_change_h1:
        return False
    # Fast-tape analysis: fresh 5-minute momentum, real buy pressure, and
    # volume running ahead of its hourly pace.
    if pool.change_m5 < cfg.meme_entry_change_m5:
        return False
    if pool.buys_m5 < cfg.meme_min_buys_m5:
        return False
    if pool.buys_m5 / max(pool.sells_m5, 1) < cfg.meme_min_buy_ratio:
        return False
    if pool.vol_m5_usd * 12 < cfg.meme_vol_accel * pool.vol_h1_usd:
        return False
    return True


def decide_exit(entry_price: float, entry_liquidity: float, entry_time: int,
                peak_price: float, pool: PoolSnapshot | None, now: int,
                cfg: Config) -> str | None:
    """Return exit reason, "RUG" for liquidity collapse, or None to hold."""
    if pool is None:
        return "RUG"  # pool vanished from the API
    if pool.liquidity_usd < entry_liquidity * (1 - cfg.meme_rug_liquidity_drop):
        return "RUG"
    if pool.price_usd <= entry_price * (1 - cfg.meme_stop_loss):
        return "stop loss"
    # Momentum-flip profit take: in profit and the 5-minute tape turned red.
    if (pool.price_usd >= entry_price * (1 + cfg.meme_take_profit_min)
            and pool.change_m5 < 0):
        return "momentum flip"
    if pool.price_usd <= peak_price * (1 - cfg.meme_trail):
        return "trailing stop"
    if now - entry_time >= cfg.meme_max_hold_hours * 3600:
        return "time stop"
    return None


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

@dataclass
class MemePosition:
    address: str
    symbol: str
    qty: float
    entry_price: float
    entry_time: int
    entry_liquidity: float
    peak_price: float
    cost_paid: float


@dataclass
class MemeState:
    cash: float
    positions: dict[str, MemePosition] = field(default_factory=dict)
    cooldown: dict[str, int] = field(default_factory=dict)
    n_trades: int = 0
    n_wins: int = 0
    n_rugs: int = 0
    gross_wins: float = 0.0
    gross_losses: float = 0.0
    costs_paid: float = 0.0

    def equity(self, prices: dict[str, float]) -> float:
        value = self.cash
        for addr, pos in self.positions.items():
            value += pos.qty * prices.get(addr, pos.entry_price)
        return value


STATE_FILE = "meme_state.json"
TRADES_FILE = "meme_trades.csv"


class MemeBot:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.state = self._load()

    # -- persistence --------------------------------------------------------

    def _load(self) -> MemeState:
        path = Path(self.cfg.state_dir) / STATE_FILE
        if path.exists():
            raw = json.loads(path.read_text())
            state = MemeState(**{k: v for k, v in raw.items()
                                 if k not in ("positions",)})
            state.positions = {a: MemePosition(**p)
                               for a, p in raw.get("positions", {}).items()}
            log.info("resumed meme experiment: cash=$%.2f, %d position(s), "
                     "%d trades closed", state.cash, len(state.positions),
                     state.n_trades)
            return state
        state = MemeState(cash=self.cfg.meme_cash)
        log.info("fresh meme experiment account: $%.2f", state.cash)
        self._save(state)
        return state

    def _save(self, state: MemeState | None = None) -> None:
        state = state or self.state
        path = Path(self.cfg.state_dir) / STATE_FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = dict(vars(state))
        payload["positions"] = {a: vars(p) for a, p in state.positions.items()}
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, indent=2))
        tmp.replace(path)

    def _log_trade(self, row: dict) -> None:
        path = Path(self.cfg.state_dir) / TRADES_FILE
        fields = ["timestamp", "symbol", "pool", "side", "qty", "price",
                  "pnl", "reason", "cash_after"]
        new = not path.exists()
        with path.open("a", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=fields)
            if new:
                writer.writeheader()
            writer.writerow({k: row.get(k, "") for k in fields})

    # -- trading ------------------------------------------------------------

    def _close(self, pos: MemePosition, price: float, reason: str,
               now: int) -> None:
        state, cfg = self.state, self.cfg
        if reason == "RUG":
            proceeds, cost = 0.0, 0.0
            state.n_rugs += 1
        else:
            gross = pos.qty * price
            cost = gross * cfg.meme_cost_per_side
            proceeds = gross - cost
        state.cash += proceeds
        state.costs_paid += cost
        invested = pos.qty * pos.entry_price + pos.cost_paid
        pnl = proceeds - invested
        state.n_trades += 1
        if pnl > 0:
            state.n_wins += 1
            state.gross_wins += pnl
        else:
            state.gross_losses += -pnl
        state.cooldown[pos.address] = now + cfg.meme_cooldown_hours * 3600
        del state.positions[pos.address]
        log.info("EXIT %s (%s): pnl $%.2f, cash $%.2f", pos.symbol, reason,
                 pnl, state.cash)
        self._log_trade({"timestamp": now, "symbol": pos.symbol,
                         "pool": pos.address, "side": "sell", "qty": pos.qty,
                         "price": price, "pnl": round(pnl, 4),
                         "reason": reason, "cash_after": round(state.cash, 2)})

    def _open(self, pool: PoolSnapshot, now: int) -> None:
        state, cfg = self.state, self.cfg
        prices = {a: p.entry_price for a, p in state.positions.items()}
        target = state.equity(prices) / cfg.meme_max_positions
        spend = min(target, state.cash)
        if spend < 10.0:
            return
        cost = spend * cfg.meme_cost_per_side
        qty = (spend - cost) / pool.price_usd
        state.cash -= spend
        state.costs_paid += cost
        state.positions[pool.address] = MemePosition(
            address=pool.address, symbol=pool.symbol, qty=qty,
            entry_price=pool.price_usd, entry_time=now,
            entry_liquidity=pool.liquidity_usd, peak_price=pool.price_usd,
            cost_paid=cost)
        log.info("ENTER %s: $%.2f @ %.10f (1h %+0.1f%%, liq $%.0fk)",
                 pool.symbol, spend, pool.price_usd, pool.change_h1,
                 pool.liquidity_usd / 1000)
        self._log_trade({"timestamp": now, "symbol": pool.symbol,
                         "pool": pool.address, "side": "buy", "qty": qty,
                         "price": pool.price_usd, "reason": "momentum entry",
                         "cash_after": round(state.cash, 2)})

    def check_once(self) -> None:
        cfg, state, now = self.cfg, self.state, int(time.time())
        # 1) manage open positions
        if state.positions:
            try:
                pools = fetch_pools(list(state.positions))
            except DataError as err:
                log.error("position poll failed: %s", err)
                return
            for pos in list(state.positions.values()):
                pool = pools.get(pos.address)
                if pool:
                    pos.peak_price = max(pos.peak_price, pool.price_usd)
                reason = decide_exit(pos.entry_price, pos.entry_liquidity,
                                     pos.entry_time, pos.peak_price, pool,
                                     now, cfg)
                if reason:
                    self._close(pos, pool.price_usd if pool else 0.0,
                                reason, now)
        # 2) look for entries
        if len(state.positions) < cfg.meme_max_positions:
            try:
                trending = fetch_trending_pools()
            except DataError as err:
                log.error("trending poll failed: %s", err)
                self._save()
                return
            held = set(state.positions)
            for pool in trending:
                if len(state.positions) >= cfg.meme_max_positions:
                    break
                if passes_entry(pool, cfg, now, held, state.cooldown):
                    self._open(pool, now)
        self._save()

    def run_forever(self) -> None:
        stop_file = Path(self.cfg.state_dir) / "STOP"
        log.info("meme experiment polling every %ds (touch %s to halt)",
                 self.cfg.meme_poll_seconds, stop_file)
        while True:
            if stop_file.exists():
                log.info("STOP file found, halting.")
                return
            try:
                self.check_once()
            except Exception:
                log.exception("poll cycle failed; will retry")
            time.sleep(self.cfg.meme_poll_seconds)

    def status(self) -> str:
        state = self.state
        prices: dict[str, float] = {}
        if state.positions:
            try:
                pools = fetch_pools(list(state.positions))
                prices = {a: p.price_usd for a, p in pools.items()}
            except DataError:
                pass
        eq = state.equity(prices)
        win_rate = state.n_wins / state.n_trades if state.n_trades else 0.0
        pf = (state.gross_wins / state.gross_losses
              if state.gross_losses > 0 else float("inf"))
        lines = [
            f"Equity:      ${eq:,.2f}  ({eq / self.cfg.meme_cash - 1:+.2%} vs start)",
            f"Cash:        ${state.cash:,.2f}",
            f"Closed:      {state.n_trades} trades, win rate {win_rate:.0%}, "
            f"rugs {state.n_rugs}, profit factor {pf:.2f}",
            f"Costs paid:  ${state.costs_paid:,.2f}",
            "NOTE: paper results OVERSTATE live memecoin returns "
            "(honeypots/failed exits/sandwiching not modeled).",
        ]
        for pos in state.positions.values():
            cur = prices.get(pos.address, pos.entry_price)
            lines.append(f"  {pos.symbol}: {pos.qty:,.0f} @ {pos.entry_price:.10f} "
                         f"now {cur:.10f} ({cur / pos.entry_price - 1:+.1%})")
        if not state.positions:
            lines.append("  no open positions")
        return "\n".join(lines)
