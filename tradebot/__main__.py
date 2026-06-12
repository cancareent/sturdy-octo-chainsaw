"""CLI entry point: python -m tradebot {backtest,run,status,reset}"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from .backtest import fetch_history, run_backtest
from .config import Config
from .runner import Runner

DISCLAIMER = (
    "PAPER TRADING ONLY — simulated money against real market data.\n"
    "Past performance does not predict future results. Nothing here is\n"
    "financial advice, and no strategy guarantees profit.\n"
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="tradebot", description=DISCLAIMER)
    parser.add_argument("--config", default="config.json")
    sub = parser.add_subparsers(dest="command", required=True)

    p_bt = sub.add_parser("backtest", help="run the strategy on historical data")
    p_bt.add_argument("--days", type=int, default=1095,
                      help="history length in days (default: 3 years)")

    p_run = sub.add_parser("run", help="start the live paper-trading loop")
    p_run.add_argument("--once", action="store_true",
                       help="single poll cycle (for cron), then exit")

    sub.add_parser("status", help="show paper account status")

    p_reset = sub.add_parser("reset", help="wipe paper account state")
    p_reset.add_argument("--confirm", action="store_true")

    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    cfg = Config.load(args.config)

    if args.command == "backtest":
        print(DISCLAIMER)
        print(f"Fetching {args.days} days of {cfg.granularity}s candles for "
              f"{cfg.products} ...")
        candles = fetch_history(cfg, args.days)
        for product, cs in candles.items():
            print(f"  {product}: {len(cs)} candles")
        result = run_backtest(cfg, candles)
        print()
        print(result.summary())
        return 0

    if args.command == "run":
        print(DISCLAIMER)
        runner = Runner(cfg)
        if args.once:
            acted = runner.check_once()
            print("acted on new candle(s)" if acted else "no new closed candles")
            print(runner.status())
        else:
            runner.run_forever()
        return 0

    if args.command == "status":
        print(Runner(cfg).status())
        return 0

    if args.command == "reset":
        state = Path(cfg.state_dir) / "paper_state.json"
        if not args.confirm:
            print(f"would delete {state}; re-run with --confirm")
            return 1
        state.unlink(missing_ok=True)
        print("paper account state deleted")
        return 0

    return 1


if __name__ == "__main__":
    sys.exit(main())
