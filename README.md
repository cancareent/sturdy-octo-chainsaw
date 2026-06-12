# tradebot — automated crypto paper-trading bot

A fully automated, zero-dependency Python bot that trades a long-only
**trend-following strategy** (EMA 20/100 crossover with a 3×ATR trailing
stop) on BTC-USD and ETH-USD daily candles, using **simulated money**
against **real live Coinbase market data**. No API keys, no accounts, no
signups — it runs out of the box.

## ⚠️ Read this first — the honest part

- **This bot trades simulated money.** It exists so you can evaluate a
  strategy with real market prices and zero financial risk.
- **No strategy guarantees profit.** Most retail trading bots lose money
  to fees, slippage, and whipsaws. Anyone promising automated passive
  profit is selling something.
- **Real backtest results** (fees of 0.6%/side + slippage included), run
  on 2026-06-12 against real Coinbase data:

  | Window | Strategy | Buy & hold | Strategy max DD | B&H max DD |
  |---|---|---|---|---|
  | 3 years (2023–2026) | **+18.9%** | +71.3% | 12.3% | — |
  | 5 years (2021–2026) | **+9.6%** | +7.5% | **12.4%** | **76.8%** |

  The pattern matches published research on trend following: it
  *underperforms* buy-and-hold in strong bull runs but avoids the
  catastrophic drawdowns of full crypto cycles. Its value is risk
  control, not outsized returns. Reproduce these numbers yourself with
  `python3 -m tradebot backtest`.
- Past performance does not predict future results. Nothing here is
  financial advice.

## Quick start

Requires Python 3.10+. No third-party packages.

```bash
# 1. See how the strategy would have performed historically (~1 min)
python3 -m tradebot backtest --days 1095

# 2. Start paper trading (checks for newly closed daily candles every 15 min)
python3 -m tradebot run

# 3. In another terminal, check on it any time
python3 -m tradebot status
```

To keep it running unattended on a server:

```bash
nohup python3 -m tradebot run >> state/bot.log 2>&1 &
```

or run one cycle from cron (state persists between runs):

```cron
*/15 * * * * cd /path/to/repo && python3 -m tradebot run --once >> state/bot.log 2>&1
```

### Stopping it

Create a file named `STOP` in the state directory; the loop halts on the
next cycle:

```bash
touch state/STOP
```

### Resetting the paper account

```bash
python3 -m tradebot reset --confirm
```

## The Staked Trend Rotator (`rotator-*` commands)

The second strategy in this repo, built for a small SOL holding: capital is
**always earning yield** — staked SOL (~6.5% APY, in SOL terms) during
uptrends, yield-bearing USDC (~5% APY) during downtrends — and the trend
engine (same EMA 20/100 + ATR stop, daily candles) decides which state.
Rotations cost 0.25% each (conservative vs measured Jupiter swap costs)
and happen a handful of times per year.

Real backtest results, run 2026-06-12 on $400 (fees, slippage, rotation
costs included; yields are conservative net estimates):

| Window | Rotator | Hold SOL | Hold staked SOL | Rotator max DD | Hold max DD |
|---|---|---|---|---|---|
| 5 years | **$1,526 (+282%)** | $197 (−51%) | $264 (−34%) | 39.9% | 96.3% |
| 3 years | **$1,868 (+367%)** | $1,371 (+243%) | $1,627 (+307%) | 39.9% | 76.3% |

The 5-year edge comes almost entirely from sidestepping SOL's 2022
collapse — that is the strategy's actual job. Do not extrapolate these
CAGRs forward; a repeat of 2022 is what it protects against, not a
promise of +30%/year.

```bash
python3 -m tradebot rotator-backtest          # reproduce the table
python3 -m tradebot rotator-run               # paper-trade it live
python3 -m tradebot rotator-status            # check state any time
```

## The memecoin experiment (`meme-*` commands)

A measurement instrument, not a profit machine. It paper-trades trending
Solana memecoins (GeckoTerminal data) on a fast tape: entries require
liquidity/volume/age filters PLUS 5-minute momentum, 5-minute buy/sell
pressure (1.5+ buys per sell), and accelerating volume; exits are a 10%
stop-loss, 12% trailing stop, a momentum-flip profit take (in profit and
the 5-minute tape turns red), a 4h time stop, and a rug detector that
marks positions to zero when pool liquidity collapses. 1% per-side costs,
20-second polling.

**Resolution floor:** upstream price data is cached ~10-30s and paper
fills assume the quoted price, so holds below a few minutes would be
fiction, not measurement. This is as fast as an honest simulation goes —
and note the cost wall: 2% round-trip means a 1-minute trade must move
+2% per minute just to break even.

**Interpretation rule, agreed in advance:** paper results OVERSTATE live
memecoin returns, because honeypots (tokens you can never sell), failed
exits during rugs, and sandwich attacks cannot be simulated. If this loses
on paper, the live version loses more. If it wins on paper, that is
necessary but not sufficient evidence. Context: CoinGecko wallet data
shows fewer than half of pump.fun traders were profitable in most of
2024–2025 (bottom: 30% in June 2025), and most winners in the best months
made under $500.

```bash
python3 -m tradebot meme-run        # run the experiment (poll every 2 min)
python3 -m tradebot meme-status     # equity, win rate, rug count, costs
```

State and a full trade log persist in `state/meme_state.json` and
`state/meme_trades.csv`.

## How it works

- **Strategy** (`tradebot/strategy.py`): enter long when the 20-day EMA
  crosses above the 100-day EMA; exit on the reverse cross or when the
  close falls below a trailing stop set 3×ATR(14) below the highest close
  since entry. Signals are computed **only on fully closed candles** —
  never on partial data.
- **Risk controls** (`tradebot/broker.py`): position size targets 2% of
  equity at risk between entry and stop, capped at 45% of equity per
  product; new entries halt if account drawdown exceeds 25%; cash can
  never go negative.
- **Realistic fills**: every simulated order pays a 0.6% taker fee (the
  Coinbase retail rate for small accounts) plus 5 bps slippage.
- **No lookahead in backtests** (`tradebot/backtest.py`): a signal on
  candle *i*'s close fills at candle *i+1*'s open. The backtester calls
  the exact same `Strategy.evaluate()` the live loop uses.
- **State** (`state/paper_state.json`, `state/trades.csv`): every action
  is persisted atomically; the bot resumes cleanly after restarts.
- **Data** (`tradebot/data.py`): Coinbase Exchange public REST API,
  unauthenticated, with retry/backoff and rate-limit pauses.

All knobs live in `config.json` (markets, EMA periods, fees, risk
limits, starting cash). Delete it to fall back to defaults.

## Tests

```bash
python3 -m unittest discover -s tests -v
```

24 tests cover indicator math against hand-computed values, fee/slippage
accounting, position sizing caps, stop behavior, and the no-lookahead
property of the backtester.

## What about real money?

Deliberately not implemented. If, after **months** of paper results, you
wanted to trade live, you would need to add an authenticated exchange
client behind the same `broker.py` interface — and you should only do
that with money you can afford to lose entirely. Given the backtest
numbers above, also compare the simplest alternative first: periodic
buy-and-hold has historically out-returned this strategy, just with much
deeper drawdowns.
