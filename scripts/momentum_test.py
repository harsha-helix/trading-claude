"""Monthly momentum portfolio on the point-in-time universe (survivorship-free).

Score (NIFTY 200 Momentum 30 style): z(6m return / 1y vol) + z(12m return / 1y vol), averaged.
Hold the top N members equally weighted; rebalance at the first open of each month (or quarter).
Charges are simulated per order on a real account size, so small accounts pay
the per-order minimum brokerage and the ₹20 DP charge per sell.

  python scripts/momentum_test.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from papertrade import costs as C  # noqa: E402

CFG = json.loads((ROOT / "config.json").read_text())
SLIP = CFG["fills"]["slippage_pct"] / 100


def load():
    import os
    pit = Path(os.environ.get("PIT_DIR", ROOT / "data/pit"))
    px = pd.read_csv(pit / "prices.csv.gz", parse_dates=["date"])
    members = pd.read_csv(pit / "members.csv")
    mem = {m: set(g["symbol"]) for m, g in members.groupby("month")}
    close = px.pivot(index="date", columns="symbol", values="close").sort_index()
    opn = px.pivot(index="date", columns="symbol", values="open").reindex(close.index)
    y = pd.read_csv(ROOT / "data/prices.csv.gz", parse_dates=["date"])
    special = y[y["symbol"].isin([CFG["benchmark"], "NIFTYBEES", "GROWWLIQID"])].pivot(
        index="date", columns="symbol", values="close").reindex(close.index).ffill()
    return close, opn, mem, special


def scores(close: pd.DataFrame) -> pd.DataFrame:
    lr = np.log(close).diff()
    vol = lr.rolling(252, min_periods=200).std() * np.sqrt(252)
    r6, r12 = close / close.shift(126) - 1, close / close.shift(252) - 1
    z = lambda df: df.sub(df.mean(axis=1), axis=0).div(df.std(axis=1), axis=0)
    return (z(r6 / vol) + z(r12 / vol)) / 2


def run(close, opn, mem, special, score, start, end, top_n=20, every=1, capital=100_000.0,
        filter_fn=None, liquid_yield=0.06, lazy=False, vol_target=None, vol_lookback=126) -> dict:
    """lazy=True: keep a holding while it ranks in the top 2N (buffer), never trim or top up
    existing holdings; only sell drop-outs and buy new names. Cuts per-order charges."""
    days = close.loc[start:end].index
    month_first = days.to_series().groupby(days.to_period("M")).first()
    rebal = month_first.iloc[::every]
    cash, units, last_px, charges, eq_hist = capital, {}, {}, 0.0, []
    bench = special[CFG["benchmark"]]
    cc = CFG["costs"]
    prev_d = None
    for d in days:
        o, c = opn.loc[d], close.loc[d]
        # idle cash earns a liquid-fund yield
        if prev_d is not None:
            cash *= (1 + liquid_yield) ** ((d - prev_d).days / 365)
        # holdings that stopped trading for 10+ sessions: write off at 20% haircut
        for s in list(units):
            if pd.isna(c.get(s)):
                last = last_px[s]
                if close.loc[last[1]:d].index.size > 11:
                    v = units.pop(s) * last[0] * 0.8
                    ch = C.charges("SELL", v, cc).total
                    cash += v - ch; charges += ch
            else:
                last_px[s] = (c[s], d)
        if d in rebal.values:
            sig_day = close.index[close.index.get_loc(d) - 1]
            m = str(d.to_period("M") - 1)
            elig = [s for s in mem.get(m, ()) if s in score.columns]
            sc = score.loc[sig_day, elig].dropna()
            exposure = 1.0 if filter_fn is None else filter_fn(sig_day)
            if vol_target and len(eq_hist) > vol_lookback // 2:
                # Barroso & Santa-Clara (2015): scale exposure by the strategy's own recent volatility
                r = pd.Series(eq_hist[-vol_lookback:]).pct_change().dropna()
                realised = r.std() * np.sqrt(252)
                exposure *= min(1.0, vol_target / realised) if realised > 0 else 1.0
            value = cash + sum(u * last_px[s][0] for s, u in units.items())

            def sell(s, n):
                nonlocal cash, charges
                v = n * o[s] * (1 - SLIP)
                ch = C.charges("SELL", v, cc).total
                cash += v - ch; charges += ch
                units[s] -= n
                if not units[s]:
                    units.pop(s)

            def buy(s, amount):
                nonlocal cash, charges
                n = int(min(amount, cash) / (o[s] * (1 + SLIP) * 1.003))
                if n > 0:
                    v = n * o[s] * (1 + SLIP)
                    ch = C.charges("BUY", v, cc).total
                    cash -= v + ch; charges += ch
                    units[s] = units.get(s, 0) + n
                    last_px[s] = (c.get(s, o[s]), d)

            tradable = lambda s: not pd.isna(o.get(s))
            if exposure <= 0:
                for s in [s for s in units if tradable(s)]:
                    sell(s, units[s])
            elif lazy:
                # keep holdings still ranked in the top 2N; fill empty slots with the best new names
                buffer = set(sc.nlargest(2 * top_n).index)
                for s in [s for s in units if s not in buffer and tradable(s)]:
                    sell(s, units[s])
                slots = top_n - len(units)
                new = [s for s in sc.nlargest(top_n * 2).index if s not in units and tradable(s)][:max(0, slots)]
                each = min(value * exposure / top_n, cash / max(1, len(new)))
                for s in new:
                    buy(s, each)
                # exposure management: scale all holdings when invested share is >10 pts off target
                invested = sum(u * o[s] for s, u in units.items() if tradable(s)) / value
                if invested > exposure + 0.10:
                    f = 1 - exposure / invested
                    for s in [s for s in units if tradable(s)]:
                        n = int(units[s] * f)
                        if n:
                            sell(s, n)
                elif invested < exposure - 0.10 and units:
                    gap = (exposure - invested) * value / len(units)
                    for s in [s for s in list(units) if tradable(s)]:
                        if gap > o[s]:
                            buy(s, gap)
            else:
                want = [s for s in sc.nlargest(top_n).index if tradable(s)]
                target_each = value * exposure / max(1, len(want))
                for s in [s for s in units if tradable(s)]:
                    tgt = int(target_each / o[s]) if s in want else 0
                    if tgt < units[s]:
                        sell(s, units[s] - tgt)
                for s in want:
                    gap = target_each - units.get(s, 0) * o[s]
                    if gap > o[s]:
                        buy(s, gap)
        eq = cash + sum(u * (c[s] if not pd.isna(c.get(s)) else last_px[s][0]) for s, u in units.items())
        eq_hist.append(eq)
        prev_d = d
    eq = pd.Series(eq_hist, index=days)
    yrs = len(days) / 252
    b = bench.loc[days]
    b0 = bench[bench.index < days[0]].iloc[-1]
    return {"cagr": (eq.iloc[-1] / capital) ** (1 / yrs) - 1,
            "bench_cagr": (b.iloc[-1] / b0) ** (1 / yrs) - 1,
            "maxdd": float((eq / eq.cummax() - 1).min()),
            "charges_pct_yr": charges / capital / yrs, "final": eq.iloc[-1]}


if __name__ == "__main__":
    close, opn, mem, special = load()
    score = scores(close)
    b = special[CFG["benchmark"]]
    sma200 = b.rolling(200).mean()
    lt = pd.concat([b > sma200, sma200 > sma200.shift(20), b > b.shift(252)], axis=1).mean(axis=1)
    filters = {
        "always invested": None,
        "200-day on/off": lambda d: 1.0 if b[d] > sma200[d] else 0.0,
        "long-term score: 100% / 50%": lambda d: 1.0 if lt[d] >= 0.5 else 0.5,
    }
    lazy = "--lazy" in sys.argv
    for lab, s, e in [("IS 2011-2018", "2011-01-01", "2018-12-31"), ("OOS 2019-2026", "2019-01-01", "2026-09-30"),
                      ("2024-2026", "2024-01-01", "2026-09-30")]:
        for fname, f in filters.items():
            for top_n, every, cap in [(20, 1, 100_000), (10, 1, 100_000), (20, 3, 100_000), (10, 3, 10_000)]:
                r = run(close, opn, mem, special, score, s, e, top_n, every, cap, f, lazy=lazy)
                print(f"{'lazy ' if lazy else ''}{lab:14s} {fname:28s} top{top_n:<3d} every {every}m  ₹{cap:>7,.0f}: "
                      f"{r['cagr']:+6.1%}/yr  NIFTY500 {r['bench_cagr']:+6.1%}/yr  maxDD {r['maxdd']:6.1%}  "
                      f"charges {r['charges_pct_yr']:.2%}/yr", flush=True)
