# trading-claude

A paper-trading desk for NSE equities. Claude runs a **monthly momentum portfolio** on the
NIFTY 500 and proposes each month's buys and sells. The owner approves them on a private
dashboard, and a simulator fills them at the next open with realistic Groww charges and taxes.

> **Paper trading only.** No broker is connected and no real orders are ever placed. The point
> is to build a real track record, and the discipline, before risking money.

- **Dashboard:** https://claude.ai/artifact/Y7LzhYmPBiYMZVAZJMkcoZ (private to the owner)
- **Runbook for Claude:** [`CLAUDE.md`](CLAUDE.md)
- **Settings:** [`config.json`](config.json)

---

## How it works

| When (IST) | What happens | Who |
|---|---|---|
| Weekdays, 18:20 | Fetch end-of-day prices, fill approved orders, update the momentum ranking and the dashboard, commit the ledger. | Claude (scheduled) |
| Evening before a month's first session | Rebalance proposals: sell holdings that dropped out of the top 40, buy the best new names to refill the top 20. Each buy gets a news check. | Claude |
| That evening | **Approve** or **Reject** each proposal. | You |
| 09:15 next day | Decision deadline. Undecided proposals expire. | Simulator |
| Saturday, 10:20 | Weekly review in plain language, with at most two backtested change proposals. | Claude (scheduled) |

On a normal weekday nothing trades. You'll see the ranking move, and the trades happen once a month.

## Accounts

| Account | Start (1 Oct 2026) | How it holds momentum |
|---|---|---|
| `A_lumpsum` | ₹1,00,000 | The top 20 stocks directly, about ₹5,000 each |
| `B_monthly` | ₹10,000, plus ₹10,000 each month | **MOM30IETF** (NIFTY 200 Momentum 30 ETF) until the account reaches ₹1L, then the top 20 directly |

Account B uses the ETF because at ₹10k, 20 stocks would mean ₹500 positions, and charges would
eat 7–11% a year. Each ETF purchase is a single order. Idle cash in both accounts sits in the
**GROWWLIQID** liquid ETF. Returns are time-weighted, so top-ups don't count as profit.

## The strategy

The code is `strategy.momentum_ranking` and `Engine.propose_momentum`.

**Score.** For each NIFTY 500 stock, the 6-month and 12-month returns are each divided by 1-year
volatility. Both are z-scored across the universe and averaged. This is the method of NSE's
NIFTY 200 Momentum 30 index, applied to the NIFTY 500.

**Each month:**
1. Sell holdings that fell out of the top 40 (a 2× buffer, so stocks hovering around 20th place
   aren't traded back and forth every month).
2. Buy the best-ranked stocks not already held until there are 20, each at 1/20 of the account.
3. Never trim or top up a holding. That halved charges in testing.

There are **no stop-losses and no market-timing switch.** Both were tested and both made results
worse (see below). The strategy stays invested through bad markets. Over 2011–2026 the market model's
"bear" readings were followed by *above-average* returns, and filters sold near the bottoms.

## Evidence

All tests are on **survivorship-free** data: every stock in each month's universe as it was at
the time, rebuilt from 4,000+ daily NSE bhavcopies (`scripts/build_pit.py`), including stocks that
later collapsed or were delisted. Results include all charges.

Momentum, top 20, monthly, ₹1L, in % per year:

| | 2011–2018 | 2019–Sep 2026 | 2024–Sep 2026 |
|---|---|---|---|
| **Momentum** (standalone test) | **+23.0** (worst fall −33%) | **+24.0** (worst fall −40%) | **+10.3** (worst fall −40%) |
| Momentum (full engine, cross-check) | +26.4 (−30%) | +25.0 (−41%) | |
| Momentum, pessimistic data* | +14.4 (−34%) | +20.7 (−50%) | +4.6 (−46%) |
| NIFTY 500 index | +8.2 | +12.2 | +4.8 |
| Swing strategy v2 (the previous version) | −5.3 | +3.8 | −13.9 |
| Swing + multi-timeframe market model | −8.4 | −9.5 | −22.7 |
| Momentum with a 200-day on/off filter | +18.8 | +14.3 | +2.4 |

\* Pessimistic: any overnight gap without Yahoo or NSE evidence of a split is treated as a real
crash, never as a split.

**What this means:**
- Momentum beat the index by roughly 6–15 points a year in each long period, with deeper falls.
  Expect drawdowns of **30–50%** at some point. That's the price of the extra return.
- Results agree in direction and size with NSE's published NIFTY 200 Momentum 30 index history.
- The earlier swing strategies only looked good on biased data. Testing on today's NIFTY 500 list
  inflated returns by 9–15% a year, because 62% of the 2011 universe isn't in today's list.
- Months like October 2026 happen: momentum fell 8% while the index fell 2%.

## Charges and tax

- **Fills:** next open plus 0.10% slippage, rounded to the ₹0.05 tick.
- **Groww delivery charges:** brokerage is the lower of ₹20 and 0.1% (minimum ₹5). STT 0.1% on each
  side, NSE transaction charge 0.00297%, SEBI ₹10 per crore, stamp duty 0.015% on buys, GST 18%,
  and ₹20 DP charge per sell. Debt ETFs (GROWWLIQID) pay no STT.
- **Estimated tax:** 20% short-term and 12.5% long-term above ₹1.25L, lot by lot, first in first
  out. Liquid ETF gains are taxed at your slab rate (assumed 30%).

## Go-live checklist (in `config.json`)

- [ ] At least 3 months of paper trading
- [ ] Beating the NIFTY 500 after all costs
- [ ] Maximum drawdown under 15%. Momentum's normal drawdowns are deeper than this, so this
  threshold will probably need to change.

## Repository layout

```
papertrade/
  data.py        Yahoo/NSE end-of-day prices, dividends, splits (cached in data/)
  strategy.py    indicators, momentum ranking, swing setups (kept for reference)
  regime.py      multi-timeframe market model (long/short-term, India/global); display only
  risk.py        swing position sizing
  costs.py       Groww charges and capital-gains tax
  portfolio.py   ledger: cash, positions, liquid ETF, core/ETF lots, events
  engine.py      daily loop: fills, exits, ETF sleeves, cash parking, proposals (momentum or swing)
  report.py      metrics, go-live checks, reports/dashboard.json
scripts/
  build_pit.py       survivorship-free dataset from NSE bhavcopies (point-in-time universe)
  momentum_test.py   standalone momentum backtest
  backtest.py        engine backtest (Yahoo cache, or --pit data)
  compare.py         side-by-side comparison of strategy variants
dashboard/index.html the dashboard page (approvals stored in its database)
state/               ledgers, proposals, approvals (committed after every run)
reports/             dashboard data, backtest results, weekly reviews
tests/               14 unit tests
```

## Usage

```bash
pip install -r requirements.txt
python -m pytest
python -m papertrade update-data --no-fundamentals
python -m papertrade run
python -m papertrade approve 2026-10-30-STLTECH        # usually synced from the dashboard
python -m papertrade report

python scripts/build_pit.py download && python scripts/build_pit.py build   # ~25 min the first time
python scripts/momentum_test.py
```

## Known limitations

- **Corporate actions.** Splits and bonuses are detected from price gaps, confirmed by Yahoo or NSE
  where possible. 88% of stocks match Yahoo's adjusted prices within 3%. Demergers and rights
  issues show up as real price drops.
- **Dividends** are not included, in either the strategy or the NIFTY 500 price index it is
  compared against.
- **The universe in backtests** is the 500 most-traded stocks each month, a close stand-in for
  the NIFTY 500. Live trading uses the actual NIFTY 500 list.
- **Whole shares.** With ₹1L split 20 ways, an expensive stock can't be bought in an exact ₹5,000
  slot, so sometimes only 19 of 20 slots are filled.
