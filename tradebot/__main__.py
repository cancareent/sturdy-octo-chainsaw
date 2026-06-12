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

    p_rbt = sub.add_parser("rotator-backtest",
                           help="backtest the staked trend rotator on SOL")
    p_rbt.add_argument("--days", type=int, default=1825,
                       help="history length in days (default: 5 years)")
    p_rrun = sub.add_parser("rotator-run", help="start the rotator paper loop")
    p_rrun.add_argument("--once", action="store_true",
                        help="single poll cycle (for cron), then exit")
    sub.add_parser("rotator-status", help="show rotator paper state")

    p_meme = sub.add_parser("meme-run",
                            help="run the memecoin paper EXPERIMENT (measurement, "
                                 "not a profit machine)")
    p_meme.add_argument("--once", action="store_true",
                        help="single poll cycle, then exit")
    sub.add_parser("meme-status", help="show meme experiment results")

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

    if args.command == "rotator-backtest":
        from .rotator import fetch_rotator_history, run_rotator_backtest
        print(DISCLAIMER)
        print(f"Fetching {args.days} days of daily {cfg.rotator_product} candles ...")
        candles = fetch_rotator_history(cfg, args.days)
        print(f"  {cfg.rotator_product}: {len(candles)} candles")
        print()
        print(run_rotator_backtest(cfg, candles).summary())
        return 0

    if args.command == "rotator-run":
        from .rotator import RotatorRunner
        print(DISCLAIMER)
        runner = RotatorRunner(cfg)
        if args.once:
            acted = runner.check_once()
            print("acted on new candle" if acted else "no new closed candle")
            print(runner.status())
        else:
            runner.run_forever()
        return 0

    if args.command == "rotator-status":
        from .rotator import RotatorRunner
        print(RotatorRunner(cfg).status())
        return 0

    if args.command == "meme-run":
        from .memebot import MemeBot
        print(DISCLAIMER)
        print("MEMECOIN EXPERIMENT: paper results OVERSTATE live returns —\n"
              "honeypots, failed exits and sandwich attacks are not simulated.\n")
        bot = MemeBot(cfg)
        if args.once:
            bot.check_once()
            print(bot.status())
        else:
            bot.run_forever()
        return 0

    if args.command == "meme-status":
        from .memebot import MemeBot
        print(MemeBot(cfg).status())
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
