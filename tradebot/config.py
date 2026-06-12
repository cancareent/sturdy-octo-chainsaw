"""Configuration loading and defaults."""

from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from pathlib import Path

VALID_GRANULARITIES = {60, 300, 900, 3600, 21600, 86400}


@dataclass
class Config:
    # Markets to trade (Coinbase product ids).
    products: list[str] = field(default_factory=lambda: ["BTC-USD", "ETH-USD"])
    # Candle size in seconds. 86400 = daily. Slow timeframes keep fee drag low.
    granularity: int = 86400

    # Strategy parameters (periods are in candles).
    ema_fast: int = 20
    ema_slow: int = 100
    atr_period: int = 14
    atr_stop_mult: float = 3.0

    # Paper account.
    starting_cash: float = 1000.0
    # Coinbase retail taker fee for small accounts is ~0.6% per side.
    fee_rate: float = 0.006
    # Extra execution slippage assumption, as a fraction (0.0005 = 5 bps).
    slippage: float = 0.0005

    # Risk limits.
    risk_per_trade: float = 0.02     # fraction of equity risked between entry and stop
    max_alloc_per_product: float = 0.45  # max fraction of equity in one product
    max_drawdown_halt: float = 0.25  # stop opening new positions past this drawdown

    # Live runner.
    poll_seconds: int = 900
    state_dir: str = "state"

    # Staked trend rotator: hold staked SOL in uptrends, yield-bearing
    # stables in downtrends. Yields are conservative net-of-fee estimates
    # (research 2026-06: JitoSOL ~5.4-7% APY, Kamino USDC ~4-7%).
    rotator_product: str = "SOL-USD"
    rotator_cash: float = 400.0
    staking_apy: float = 0.065
    stable_apy: float = 0.05
    # One-way cost of rotating between staked SOL and USDC (DEX swap fee +
    # slippage + tx fees). Jupiter round trips on SOL measured ~0-5 bps;
    # 25 bps per rotation is deliberately conservative.
    rotation_cost: float = 0.0025

    # Memecoin paper experiment. Costs are deliberately pessimistic, but
    # paper results still OVERSTATE live returns (honeypots, failed exits
    # and sandwiching cannot be simulated).
    # SIMULATION RESOLUTION FLOOR: upstream price data is cached ~10-30s and
    # paper fills assume the quoted price, so holds shorter than a few
    # minutes produce fiction, not measurement. 20s polling with
    # minutes-scale holds is as fast as this instrument can honestly go.
    meme_cash: float = 400.0
    meme_max_positions: int = 5
    meme_min_liquidity_usd: float = 100_000.0
    meme_min_volume24h_usd: float = 250_000.0
    meme_min_age_hours: float = 24.0   # skip the sniper-dominated launch window
    meme_entry_change_h1: float = 3.0  # percent 1h momentum required to enter
    meme_entry_change_m5: float = 2.0  # percent 5-minute momentum trigger
    meme_min_buys_m5: int = 5          # minimum 5-minute buy count (activity)
    meme_min_buy_ratio: float = 1.5    # 5-minute buys per sell (buy pressure)
    meme_vol_accel: float = 1.0        # m5 volume *12 must beat h1 volume * this
    meme_stop_loss: float = 0.10
    meme_trail: float = 0.12
    meme_take_profit_min: float = 0.08  # profit needed to arm momentum-flip exit
    meme_max_hold_hours: float = 4.0
    meme_rug_liquidity_drop: float = 0.7  # liquidity falls this much => rugged
    meme_cost_per_side: float = 0.01      # pool fee + price impact + tx fees
    meme_cooldown_hours: float = 6.0
    meme_poll_seconds: int = 20

    def validate(self) -> None:
        if self.granularity not in VALID_GRANULARITIES:
            raise ValueError(f"granularity must be one of {sorted(VALID_GRANULARITIES)}")
        if not (0 < self.ema_fast < self.ema_slow):
            raise ValueError("require 0 < ema_fast < ema_slow")
        if self.atr_period < 2:
            raise ValueError("atr_period must be >= 2")
        if self.atr_stop_mult <= 0:
            raise ValueError("atr_stop_mult must be positive")
        if self.starting_cash <= 0:
            raise ValueError("starting_cash must be positive")
        if not (0 <= self.fee_rate < 0.05):
            raise ValueError("fee_rate must be in [0, 0.05)")
        if not (0 <= self.slippage < 0.05):
            raise ValueError("slippage must be in [0, 0.05)")
        if not (0 < self.risk_per_trade <= 0.1):
            raise ValueError("risk_per_trade must be in (0, 0.1]")
        if not (0 < self.max_alloc_per_product <= 1):
            raise ValueError("max_alloc_per_product must be in (0, 1]")
        if not self.products:
            raise ValueError("products must not be empty")
        if self.rotator_cash <= 0:
            raise ValueError("rotator_cash must be positive")
        if not (0 <= self.staking_apy < 0.5 and 0 <= self.stable_apy < 0.5):
            raise ValueError("yield assumptions must be in [0, 0.5)")
        if not (0 <= self.rotation_cost < 0.05):
            raise ValueError("rotation_cost must be in [0, 0.05)")
        if self.meme_cash <= 0 or self.meme_max_positions < 1:
            raise ValueError("meme_cash must be positive, meme_max_positions >= 1")
        if not (0 < self.meme_stop_loss < 1 and 0 < self.meme_trail < 1):
            raise ValueError("meme_stop_loss and meme_trail must be in (0, 1)")
        if not (0 < self.meme_rug_liquidity_drop < 1):
            raise ValueError("meme_rug_liquidity_drop must be in (0, 1)")
        if not (0 <= self.meme_cost_per_side < 0.1):
            raise ValueError("meme_cost_per_side must be in [0, 0.1)")
        if self.meme_min_buy_ratio < 1 or self.meme_min_buys_m5 < 0:
            raise ValueError("meme_min_buy_ratio must be >= 1, meme_min_buys_m5 >= 0")
        if not (0 < self.meme_take_profit_min < 1):
            raise ValueError("meme_take_profit_min must be in (0, 1)")
        if self.meme_poll_seconds < 15:
            raise ValueError("meme_poll_seconds < 15 exceeds API rate limits "
                             "and the simulation's honest resolution")

    @classmethod
    def load(cls, path: str | Path = "config.json") -> "Config":
        path = Path(path)
        cfg = cls()
        if path.exists():
            data = json.loads(path.read_text())
            unknown = set(data) - set(cfg.__dataclass_fields__)
            if unknown:
                raise ValueError(f"unknown config keys: {sorted(unknown)}")
            for key, value in data.items():
                setattr(cfg, key, value)
        cfg.validate()
        return cfg

    def save(self, path: str | Path = "config.json") -> None:
        Path(path).write_text(json.dumps(asdict(self), indent=2) + "\n")
