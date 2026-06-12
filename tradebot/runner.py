"""Live paper-trading loop.

Polls for newly closed candles, applies the strategy, simulates fills at
the current spot price, and persists state after every action. Creating a
file named STOP in the state directory halts the bot (kill switch).
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from pathlib import Path

from .broker import PaperBroker, append_trade_log, load_state, save_state
from .config import Config
from .data import DataError, get_recent_closed_candles, get_spot_price
from .strategy import ENTER, EXIT, HOLD, PositionState, Strategy

log = logging.getLogger(__name__)


class Runner:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.strategy = Strategy(cfg)
        state = load_state(cfg.state_dir)
        if state:
            self.broker = PaperBroker.from_dict(cfg, state["broker"])
            self.last_processed: dict[str, int] = state.get("last_processed", {})
            log.info("resumed state: cash=$%.2f, %d open position(s)",
                     self.broker.cash, len(self.broker.positions))
        else:
            self.broker = PaperBroker.new(cfg)
            self.last_processed = {}
            log.info("fresh paper account: $%.2f", cfg.starting_cash)
            self._save()

    def _save(self) -> None:
        save_state(self.cfg.state_dir, self.broker,
                   {"last_processed": self.last_processed})

    def _spot_prices(self) -> dict[str, float]:
        return {p: get_spot_price(p) for p in self.cfg.products}

    def check_once(self) -> bool:
        """Process any newly closed candles. Returns True if anything happened."""
        acted = False
        history = self.strategy.min_history() + 5
        for product in self.cfg.products:
            try:
                candles = get_recent_closed_candles(product, self.cfg.granularity,
                                                    history)
            except DataError as err:
                log.error("data fetch failed for %s: %s", product, err)
                continue
            if len(candles) < self.strategy.min_history():
                log.warning("%s: only %d candles, need %d", product,
                            len(candles), self.strategy.min_history())
                continue
            latest = candles[-1]
            if latest.time <= self.last_processed.get(product, 0):
                continue  # no new closed candle yet

            acted = True
            bpos = self.broker.positions.get(product)
            pos_state = (PositionState(stop=bpos.stop,
                                       highest_close=bpos.highest_close)
                         if bpos else None)
            decision = self.strategy.evaluate(candles, len(candles) - 1, pos_state)
            log.info("%s: closed candle %s, close=%.2f, decision=%s %s",
                     product,
                     datetime.fromtimestamp(latest.time, timezone.utc).isoformat(),
                     latest.close, decision.action, decision.reason)
            try:
                self._execute(product, decision, latest.close)
            except DataError as err:
                log.error("execution failed for %s: %s", product, err)
                continue  # do not mark processed; retry next poll
            self.last_processed[product] = latest.time
            self._save()
        return acted

    def _execute(self, product: str, decision, candle_close: float) -> None:
        now = int(time.time())
        if decision.action == ENTER:
            prices = self._spot_prices()
            if self.broker.drawdown(prices) > self.cfg.max_drawdown_halt:
                log.warning("%s: entry skipped, drawdown beyond %.0f%% halt",
                            product, self.cfg.max_drawdown_halt * 100)
                return
            eq = self.broker.equity(prices)
            pos = self.broker.open_position(product, prices[product], now,
                                            stop=decision.initial_stop, equity=eq)
            if pos:
                log.info("%s: BUY %.6f @ $%.2f (stop $%.2f)", product, pos.qty,
                         pos.entry_price, pos.stop)
                append_trade_log(self.cfg.state_dir, {
                    "timestamp": now, "product": product, "side": "buy",
                    "qty": pos.qty, "price": pos.entry_price,
                    "fees": pos.entry_fees, "cash_after": self.broker.cash,
                    "reason": decision.reason})
            else:
                log.info("%s: entry signal but size below minimum, skipped", product)
        elif decision.action == EXIT and product in self.broker.positions:
            price = get_spot_price(product)
            fill = self.broker.close_position(product, price)
            log.info("%s: SELL %.6f @ $%.2f, pnl $%.2f", product, fill["qty"],
                     fill["exit_price"], fill["pnl"])
            append_trade_log(self.cfg.state_dir, {
                "timestamp": now, "product": product, "side": "sell",
                "qty": fill["qty"], "price": fill["exit_price"],
                "pnl": fill["pnl"], "fees": fill["fees"],
                "cash_after": self.broker.cash, "reason": decision.reason})
        elif decision.action == HOLD:
            bpos = self.broker.positions[product]
            bpos.stop = decision.new_stop
            bpos.highest_close = max(bpos.highest_close, candle_close)

    def run_forever(self) -> None:
        stop_file = Path(self.cfg.state_dir) / "STOP"
        log.info("paper trading %s every %ds (touch %s to halt)",
                 self.cfg.products, self.cfg.poll_seconds, stop_file)
        while True:
            if stop_file.exists():
                log.info("STOP file found, halting.")
                return
            try:
                self.check_once()
            except Exception:
                log.exception("poll cycle failed; will retry")
            time.sleep(self.cfg.poll_seconds)

    def status(self) -> str:
        try:
            prices = self._spot_prices()
        except DataError:
            prices = {p: pos.entry_price for p, pos in self.broker.positions.items()}
        eq = self.broker.equity(prices)
        lines = [
            f"Equity:    ${eq:,.2f}  "
            f"({eq / self.cfg.starting_cash - 1:+.2%} vs start)",
            f"Cash:      ${self.broker.cash:,.2f}",
            f"Trades:    {self.broker.n_trades} closed, "
            f"{self.broker.n_wins} wins, fees ${self.broker.total_fees:,.2f}",
        ]
        if self.broker.positions:
            for p, pos in self.broker.positions.items():
                cur = prices.get(p, pos.entry_price)
                lines.append(f"  {p}: {pos.qty:.6f} @ ${pos.entry_price:,.2f} "
                             f"now ${cur:,.2f} ({cur / pos.entry_price - 1:+.2%}) "
                             f"stop ${pos.stop:,.2f}")
        else:
            lines.append("  no open positions")
        return "\n".join(lines)
