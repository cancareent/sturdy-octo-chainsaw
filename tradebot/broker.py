"""Paper broker: simulated fills with fees and slippage, persistent state."""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .config import Config


@dataclass
class Position:
    qty: float
    entry_price: float
    entry_time: int
    stop: float
    highest_close: float
    entry_fees: float = 0.0


@dataclass
class PaperBroker:
    cfg: Config
    cash: float
    positions: dict[str, Position] = field(default_factory=dict)
    peak_equity: float = 0.0
    total_fees: float = 0.0
    n_trades: int = 0
    n_wins: int = 0

    @classmethod
    def new(cls, cfg: Config) -> "PaperBroker":
        return cls(cfg=cfg, cash=cfg.starting_cash, peak_equity=cfg.starting_cash)

    # ---- accounting -------------------------------------------------------

    def equity(self, prices: dict[str, float]) -> float:
        value = self.cash
        for product, pos in self.positions.items():
            value += pos.qty * prices[product]
        return value

    def drawdown(self, prices: dict[str, float]) -> float:
        eq = self.equity(prices)
        self.peak_equity = max(self.peak_equity, eq)
        return 1.0 - eq / self.peak_equity if self.peak_equity > 0 else 0.0

    # ---- order simulation --------------------------------------------------

    def buy_price(self, market_price: float) -> float:
        return market_price * (1 + self.cfg.slippage)

    def sell_price(self, market_price: float) -> float:
        return market_price * (1 - self.cfg.slippage)

    def size_entry(self, equity: float, entry_price: float, stop: float) -> float:
        """Risk-based position size in quote currency (USD notional)."""
        risk_per_unit = entry_price - stop
        if risk_per_unit <= 0:
            return 0.0
        qty_by_risk = (equity * self.cfg.risk_per_trade) / risk_per_unit
        notional = min(qty_by_risk * entry_price,
                       equity * self.cfg.max_alloc_per_product,
                       # keep fee headroom so cash never goes negative
                       self.cash / (1 + self.cfg.fee_rate) * 0.999)
        return max(notional, 0.0)

    def open_position(self, product: str, market_price: float, time_s: int,
                      stop: float, equity: float) -> Position | None:
        if product in self.positions:
            raise ValueError(f"already in a position for {product}")
        price = self.buy_price(market_price)
        notional = self.size_entry(equity, price, stop)
        if notional < 10.0:  # below typical exchange minimum order size
            return None
        qty = notional / price
        fee = notional * self.cfg.fee_rate
        self.cash -= notional + fee
        self.total_fees += fee
        pos = Position(qty=qty, entry_price=price, entry_time=time_s,
                       stop=stop, highest_close=market_price, entry_fees=fee)
        self.positions[product] = pos
        return pos

    def close_position(self, product: str, market_price: float) -> dict:
        pos = self.positions.pop(product)
        price = self.sell_price(market_price)
        proceeds = pos.qty * price
        fee = proceeds * self.cfg.fee_rate
        self.cash += proceeds - fee
        self.total_fees += fee
        pnl = proceeds - fee - pos.qty * pos.entry_price - pos.entry_fees
        self.n_trades += 1
        if pnl > 0:
            self.n_wins += 1
        return {"qty": pos.qty, "entry_price": pos.entry_price,
                "exit_price": price, "pnl": pnl, "fees": fee + pos.entry_fees}

    # ---- persistence -------------------------------------------------------

    def to_dict(self) -> dict:
        return {
            "cash": self.cash,
            "peak_equity": self.peak_equity,
            "total_fees": self.total_fees,
            "n_trades": self.n_trades,
            "n_wins": self.n_wins,
            "positions": {p: vars(pos) for p, pos in self.positions.items()},
        }

    @classmethod
    def from_dict(cls, cfg: Config, data: dict) -> "PaperBroker":
        broker = cls(cfg=cfg, cash=data["cash"], peak_equity=data["peak_equity"],
                     total_fees=data.get("total_fees", 0.0),
                     n_trades=data.get("n_trades", 0), n_wins=data.get("n_wins", 0))
        broker.positions = {p: Position(**pos)
                            for p, pos in data.get("positions", {}).items()}
        return broker


def append_trade_log(state_dir: str | Path, row: dict) -> None:
    path = Path(state_dir) / "trades.csv"
    fields = ["timestamp", "product", "side", "qty", "price", "pnl",
              "fees", "cash_after", "reason"]
    new_file = not path.exists()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        if new_file:
            writer.writeheader()
        writer.writerow({k: row.get(k, "") for k in fields})


def save_state(state_dir: str | Path, broker: PaperBroker, extra: dict) -> None:
    path = Path(state_dir) / "paper_state.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"saved_at": datetime.now(timezone.utc).isoformat(),
               "broker": broker.to_dict(), **extra}
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2))
    tmp.replace(path)


def load_state(state_dir: str | Path) -> dict | None:
    path = Path(state_dir) / "paper_state.json"
    if not path.exists():
        return None
    return json.loads(path.read_text())
