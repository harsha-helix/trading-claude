# trading-claude

Paper trading for NSE equities, running end-of-day swing and positional trades. Claude scans the market
each evening and proposes trades, you approve them on a dashboard, and the simulator fills
them at the next open with realistic Groww charges.

**No real money and no broker connection.** This is practice before live trading.

## Accounts (same strategy for both)
| Account | Start | Top-up |
|---|---|---|
| `A_lumpsum` | ₹1,00,000 | – |
| `B_monthly` | ₹10,000 | +₹10,000 on the first trading day of each month |

The start date is 1 Oct 2026. 1–26 Oct is a walk-forward backfill in which Claude auto-approved its
own proposals and no data after each day was used. Live approvals start on 27 Oct.

## Strategy (v1, `papertrade/strategy.py`)
- **Universe:** NIFTY 500. Price must be at least ₹50 and 20-day average turnover at least ₹10 cr.
- **Trend filter:** close above the 200-day average, 50-day above 200-day, and the 50-day rising.
- **Setups:**
  - *Breakout:* close above the prior 55-day high on at least 1.5× average volume. Stop at 2×ATR.
  - *Pullback:* touches the 20-day EMA, then closes above the prior day's high with RSI between 40 and 65. Stop below the 5-day low.
- **Fundamental filter:** ROE of at least 12% and D/E of at most 150%, when the data is available.
- **News check:** Claude web-searches each proposal and vetoes red flags.
- **Target:** 2.5R. The stop moves to breakeven at +1R. Positions close after at most 40 trading days.
  A close below the 50-day average triggers an *exit proposal*, which needs your approval.
- **Ranking:** 3-month return relative to the NIFTY 500.

## Risk (moderate)
- At most 2% of equity at risk per trade.
- At most 25% of equity in one stock.
- At most 8 open positions.
- Reward:risk of at least 2.
- Skip the trade if round-trip charges exceed 15% of the expected gain. This matters for the ₹10k account.

## Fills and costs
- Entry at the next open plus 0.10% slippage.
- Skip the entry if the stock gaps more than 3% above the reference price or opens below the stop.
- Stops and targets run automatically against the day's high and low, and a gap through the level fills at the open.
  If both are hit on the same day, the stop is assumed to come first.
- Groww delivery charges: brokerage is the lower of ₹20 and 0.1%, with a ₹5 minimum. Also STT 0.1% on both sides, NSE
  transaction charge 0.00297%, SEBI ₹10/crore, stamp duty 0.015% on buys, GST 18%, and a DP charge of ₹20 per sell.
- Tax is estimated at 20% STCG and 12.5% LTCG above ₹1.25L.
- Returns are time-weighted, so top-ups don't count as gains.

## Go-live checklist
At least 3 months of trading, beating the NIFTY 500 after costs, and maximum drawdown under 15%.

## Usage
```bash
pip install -r requirements.txt
python -m papertrade update-data
python -m papertrade backfill --until 2026-10-26   # one-off
python -m papertrade run                           # daily
python -m papertrade approve 2026-10-27-TCS        # usually synced from the dashboard
python -m papertrade report
python -m pytest
```
State lives in `state/` (JSON ledgers, committed after every run), and the dashboard feed is in `reports/dashboard.json`.
