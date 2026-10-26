# trading-claude

A paper-trading desk for NSE equities. Every weekday evening Claude scans the NIFTY 500 and
proposes swing and positional trades. The owner approves or rejects them on a private dashboard,
and a simulator fills them at the next day's open with realistic Groww charges and taxes.

> **Paper trading only.** No broker is connected and no real orders are ever placed. The point
> is to build a track record, and the discipline, before risking real money.

- **Dashboard:** https://claude.ai/artifact/Y7LzhYmPBiYMZVAZJMkcoZ (private to the owner)
- **Runbook for Claude:** [`CLAUDE.md`](CLAUDE.md)
- **Settings:** [`config.json`](config.json)

---

## How a day works

| When (IST) | What happens | Who |
|---|---|---|
| Weekdays, 18:20 | Fetch end-of-day prices. Fill yesterday's approved trades at today's open. Run stops and targets. Scan for new setups, news-check each one, update the dashboard, commit the ledger. | Claude (scheduled) |
| Evening | Review proposals and tap **Approve** or **Reject**. | You |
| 09:15 next day | Decision deadline. Undecided proposals expire, so a stale setup is never traded. | Simulator |
| Saturday, 10:20 | Weekly review: performance, lessons learned, and at most two backtested strategy changes, as proposals for you to decide on. | Claude (scheduled) |

Entries always need your approval. Exits at the stop, target or time limit run on their own, as a
real stop order would.

## Accounts

Both accounts run the same strategy, so you can see how account size changes the results.

| Account | Start (1 Oct 2026) | Top-up |
|---|---|---|
| `A_lumpsum` | ₹1,00,000 | none |
| `B_monthly` | ₹10,000 | +₹10,000 on the first trading day of each month |

Returns are **time-weighted**, so monthly top-ups don't count as profit.

From 1 to 26 Oct 2026 the simulator replayed each day in turn, using only the data available on
that day. Claude approved its own proposals for this stretch, and there was no news check because
past news can't be read without hindsight. Your approvals start on 27 Oct 2026.

## Strategy (v2)

The code is in [`papertrade/strategy.py`](papertrade/strategy.py). It trades long only, holding
stocks in a delivery account.

**1. Market filter.** New entries only while the NIFTY 500 closes above its 200-day average. When
the filter is off, the dashboard shows a *watchlist* of stocks that pass every other rule.

**2. Universe.** NIFTY 500 stocks with:
- a price of at least ₹50,
- 20-day average turnover of at least ₹10 crore,
- at least 210 days of history.

**3. Stock trend.** The close is above the 200-day average, the 50-day average is above the
200-day, and the 50-day is rising.

**4. Setups.**

| Setup | Trigger | Initial stop |
|---|---|---|
| Breakout | Close above the prior 55-day high on at least 1.5× average volume | Close − 3×ATR |
| Pullback | Touched the 20-day EMA in the last 3 days, then closed above the prior day's high, with RSI between 40 and 65 | Lower of the 5-day low and the 20-day EMA, minus 1×ATR |

A stop is never more than 4×ATR below the close.

**5. Fundamental filter.** ROE of at least 12% and debt-to-equity of at most 150%. The filter only
applies where Yahoo has data. It is not used in backtests, because today's figures would leak
the future into the past.

**6. News check.** Claude web-searches each proposal for upcoming results, corporate actions,
regulatory trouble and promoter pledges. A clear red flag is marked `VETO`.

**7. Ranking.** By 3-month return relative to the NIFTY 500. At most 10 candidates per day.

**8. Exits.**
- Target at 2.5× the initial risk (2.5R).
- The stop moves to breakeven once the trade is up 1R.
- Time stop after 40 trading days.

## Risk rules

| Rule | Value |
|---|---|
| Risk per trade | At most 2% of account equity |
| Single position | At most 25% of equity |
| Open positions | At most 8 |
| Reward to risk | At least 2:1 |
| Charges | Skip the trade if round-trip charges exceed 15% of the expected gain |

The charges rule matters mostly for the ₹10k account. Paying ₹40–60 in charges on a ₹2,500
position eats most of a small win, and the journal records every skipped trade with the reason.

## Fills, charges and tax

**Entries** fill at the next open plus 0.10% slippage. The entry is skipped if the stock opens at
or below the stop, or more than 3% above the signal close (no chasing).

**Exits** follow these rules:
- A gap through the stop or target fills at the open.
- Otherwise the exit fills at the stop or target price.
- If both levels are hit on the same day, the stop is assumed to have come first.

All prices are rounded to the ₹0.05 tick.

**Groww delivery charges**, configurable in `config.json`:

| Charge | Rate |
|---|---|
| Brokerage | Lower of ₹20 and 0.1% of the order, minimum ₹5 |
| STT | 0.1% on buy and sell |
| NSE transaction charge | 0.00297% |
| SEBI fee | ₹10 per crore |
| Stamp duty | 0.015% on buys |
| GST | 18% on brokerage, exchange and SEBI fees, and DP charge |
| DP charge | ₹20 per sell |

**Estimated tax.** Short-term gains at 20%, long-term gains at 12.5% above ₹1.25 lakh, and
liquid-ETF gains at the owner's slab rate (30% assumed).

## Idle cash

Uninvested cash is parked in **GROWWLIQID**, Groww's liquid ETF. It returned about 5% over the
past year, and debt ETFs pay no STT.

- Cash is swept at the open when at least ₹5,000 is idle. While the market filter is on, one
  full position's worth of cash stays free, so a new trade doesn't immediately pay to sell
  ETF units.
- Units are sold automatically when a buy needs the cash.
- Buys fill at the higher of the open and the previous close, and sells at the lower. Thin ETFs
  often print stray opening trades, and this rule stops the account profiting from them.

## Backtest results

The backtest ran over 2024-01 to 2026-09 on account A (₹1L), including all charges.
`IS` is the first half of the data (Jan 2024 – Jun 2025), used to choose between variants. `OOS`
is the second half (Jul 2025 – Sep 2026), held back to check the choice.

| Version | IS return | OOS return | Full period | NIFTY 500 (IS / OOS / full) |
|---|---|---|---|---|
| v1: 2×ATR stops, forced 50-day exit, no market filter | −21.4% | −4.3% | about −37% | +21.6% / −6.5% / +13.7% |
| v2 | +3.2% | +18.1% | +11.8% (max drawdown 20.7%) | same |

**v2 has not proven an edge.** Over the full period it roughly tracks the index after costs, and
results swing a lot depending on which trades happened to be taken. Months of paper trading are
what will show whether it works.

To reproduce:

```bash
python scripts/backtest.py --start 2024-01-01 --end 2026-09-30
python scripts/backtest.py --start 2024-01-01 --end 2025-06-30 --set target_r=3.0 regime_filter=False
```

## Go-live checklist

Tracked on the dashboard and in each weekly review:

- [ ] At least 3 months of paper trading
- [ ] Beating the NIFTY 500 after all costs
- [ ] Maximum drawdown under 15%

## Repository layout

```
papertrade/
  data.py        NSE / Yahoo end-of-day prices, dividends and splits, fundamentals (cached in data/)
  strategy.py    indicators, market filter, setups, watchlist
  risk.py        position sizing and per-account checks
  costs.py       Groww charges and capital-gains tax
  portfolio.py   account ledger: cash, positions, closed trades, liquid ETF, events
  engine.py      the daily loop: corporate actions, top-ups, fills, exits, proposals
  report.py      metrics, go-live checks, reports/dashboard.json
  __main__.py    command-line interface
scripts/backtest.py   walk-forward backtest in a throwaway state directory
dashboard/index.html  the dashboard page (approvals stored in its database)
state/                ledgers, proposals and approvals (JSON, committed after every run)
reports/              dashboard data and weekly reviews
tests/                unit tests
```

## Usage

```bash
pip install -r requirements.txt
python -m pytest                                    # 10 tests: charges, sizing, no-lookahead fills, expiry, top-ups, liquid ETF

python -m papertrade update-data                    # prices, actions, fundamentals
python -m papertrade run                            # process new trading days (live mode)
python -m papertrade approve 2026-10-27-TCS         # record a decision by hand (--reject to reject)
python -m papertrade note 2026-10-27-TCS "No results due; clean news"
python -m papertrade report                         # summary + reports/dashboard.json
python -m papertrade backfill --until 2026-10-26    # one-off walk-forward replay (already done)
```

`data/prices.csv.gz` (about 8 MB) is not committed. `update-data` rebuilds it in about a minute.

## Data sources

- **Prices:** Yahoo Finance end-of-day bars, cross-checked against NSE's official bhavcopy (they
  matched exactly on 23 and 26 Oct 2026). NSE bhavcopy is the fallback.
- **Index members:** the NIFTY 500 list from NSE archives.
- **Fundamentals:** Yahoo Finance, refreshed weekly. Coverage is partial because Yahoo
  rate-limits requests.

## Known limitations

- **Survivorship bias.** The universe is today's NIFTY 500, so backtests miss stocks that dropped
  out of the index and look slightly better than reality.
- **Price-only fills.** Fills use daily open, high, low and close prices. Order-book depth and
  impact cost aren't modelled. At these position sizes, in liquid stocks, they're small.
- **Approximate deadline.** The dashboard's deadline assumes the next weekday is a trading day.
  The engine applies the real calendar.
- **Charges change.** Rates are taken from Groww's and the exchanges' published schedules. Check
  them against [groww.in/p/pricing](https://groww.in/p/pricing) from time to time and update
  `config.json`.
