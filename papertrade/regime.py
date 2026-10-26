"""Multi-timeframe market model.

Two trend readings, each a 0..1 score made of equally weighted yes/no checks:

  long-term (LT)  - is the market in a durable uptrend?   (months)
  short-term (ST) - is the market rising right now?       (days to weeks)

Each reading blends local (India) and global inputs, 75/25. Global series are
lagged one trading day because US markets close after India. Macro stress flags
(oil spike, rupee slide, volatility spike) then scale risk down.

LT and ST together pick one of four regimes. The regime sets how much risk the
stock model may take, which setups it may use, and the size of the core index
holding:

                    ST up          ST down
      LT up         BULL           DIP        (buy pullbacks in an uptrend)
      LT down       RECOVERY       BEAR       (small, leaders only)

The weights are fixed in advance, not fitted to the backtest.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

GLOBAL_WEIGHT = 0.25

REGIMES = {
    #            risk %/trade, max positions, setups allowed,          min rel. strength, core %
    "BULL":     {"risk_pct": 2.0, "max_pos": 8, "setups": ("breakout", "pullback"), "min_rs": None, "core": 0.60},
    "DIP":      {"risk_pct": 1.5, "max_pos": 6, "setups": ("pullback",),            "min_rs": None, "core": 0.60},
    "RECOVERY": {"risk_pct": 1.0, "max_pos": 4, "setups": ("breakout", "pullback"), "min_rs": 0.0,  "core": 0.40},
    "BEAR":     {"risk_pct": 0.5, "max_pos": 2, "setups": ("breakout", "pullback"), "min_rs": 0.10, "core": 0.20},
}


def _close(prices: pd.DataFrame, sym: str, days: pd.DatetimeIndex, lag: int = 0) -> pd.Series | None:
    s = prices.loc[prices["symbol"] == sym, ["date", "close"]].set_index("date")["close"].sort_index()
    if s.empty:
        return None
    s = s[~s.index.duplicated(keep="last")]
    # align to Indian trading days using the last value known on each day
    s = s.reindex(s.index.union(days)).ffill().reindex(days)
    return s.shift(lag) if lag else s


def compute(prices: pd.DataFrame, ind: pd.DataFrame, benchmark: str, universe: set[str],
            global_symbols: dict | None = None, use_global: bool = True) -> pd.DataFrame:
    """One row per benchmark trading day. Every column uses data up to that day only."""
    days = pd.DatetimeIndex(sorted(prices.loc[prices["symbol"] == benchmark, "date"].unique()))
    b = _close(prices, benchmark, days)
    sma200, sma50, ema20 = b.rolling(200).mean(), b.rolling(50).mean(), b.ewm(span=20, adjust=False).mean()

    if "member" in ind.columns:   # point-in-time universe
        stocks = ind[ind["member"]]
    else:
        stocks = ind[ind.index.get_level_values("symbol").isin(universe)]
    has200 = stocks["sma200"].notna()
    above200 = (stocks["close"] > stocks["sma200"])[has200].groupby(level="date").mean().reindex(days)
    above20 = (stocks["close"] > stocks["ema20"]).groupby(level="date").mean().reindex(days)

    lt_local = pd.concat([
        b > sma200,
        sma200 > sma200.shift(20),
        b > b.shift(252),
        above200 > 0.5,
    ], axis=1).mean(axis=1)
    st_local = pd.concat([
        b > ema20,
        b > b.shift(10),
        above20 > 0.5,
        above20 > above20.shift(10),          # breadth improving
    ], axis=1).mean(axis=1)

    out = pd.DataFrame({"close": b, "breadth200": above200, "breadth20": above20,
                        "lt_local": lt_local, "st_local": st_local}, index=days)

    g = global_symbols or {}
    lt_g, st_g, stress = [], [], []
    if use_global:
        for key in ("spx", "em"):
            s = _close(prices, g.get(key, ""), days, lag=1)
            if s is not None:
                lt_g.append(s > s.rolling(200).mean())
                st_g.append(s > s.shift(10))
        oil = _close(prices, g.get("oil", ""), days, lag=1)
        if oil is not None:
            out["stress_oil"] = oil / oil.shift(63) - 1 > 0.25          # crude up >25% in 3 months
            stress.append(out["stress_oil"])
        fx = _close(prices, g.get("usdinr", ""), days, lag=1)
        if fx is not None:
            out["stress_inr"] = fx / fx.shift(21) - 1 > 0.02            # rupee down >2% in a month
            stress.append(out["stress_inr"])
    vix = _close(prices, g.get("vix", ""), days)
    if vix is not None:
        out["stress_vix"] = (vix > 25) | (vix / vix.shift(20) - 1 > 0.5)  # high or spiking volatility
        stress.append(out["stress_vix"])

    if lt_g:
        out["lt_global"] = pd.concat(lt_g, axis=1).mean(axis=1)
        out["st_global"] = pd.concat(st_g, axis=1).mean(axis=1)
        out["lt_score"] = (1 - GLOBAL_WEIGHT) * lt_local + GLOBAL_WEIGHT * out["lt_global"]
        out["st_score"] = (1 - GLOBAL_WEIGHT) * st_local + GLOBAL_WEIGHT * out["st_global"]
    else:
        out["lt_score"], out["st_score"] = lt_local, st_local
    n_stress = pd.concat(stress, axis=1).fillna(False).sum(axis=1) if stress else pd.Series(0, index=days)
    out["stress"] = n_stress
    out["risk_mult"] = np.clip(1 - 0.25 * n_stress, 0.25, 1.0)

    lt_up, st_up = out["lt_score"] >= 0.5, out["st_score"] >= 0.5
    out["regime"] = np.select([lt_up & st_up, lt_up & ~st_up, ~lt_up & st_up], ["BULL", "DIP", "RECOVERY"], "BEAR")
    return out


def params(row: pd.Series) -> dict:
    """Trading limits for one day's regime, after the stress multiplier."""
    p = dict(REGIMES[row["regime"]])
    p["risk_pct"] = round(p["risk_pct"] * float(row["risk_mult"]), 3)
    p["regime"] = row["regime"]
    return p


def describe(row: pd.Series) -> str:
    """One plain-language line for the dashboard and journal."""
    names = {"BULL": "Bull: long-term and short-term trends both up",
             "DIP": "Dip in an uptrend: long-term up, short-term down",
             "RECOVERY": "Possible recovery: long-term down, short-term up",
             "BEAR": "Bear: long-term and short-term trends both down"}
    flags = [n for n, c in (("oil spike", "stress_oil"), ("rupee falling", "stress_inr"),
                            ("volatility spike", "stress_vix")) if bool(row.get(c, False))]
    s = (f"{names[row['regime']]}. Long-term score {row["lt_score"]:.2f}, short-term {row["st_score"]:.2f}. "
         f"{row['breadth200']:.0%} of stocks are above their 200-day average, "
         f"{row['breadth20']:.0%} above their 20-day.")
    if flags:
        s += " Stress: " + ", ".join(flags) + "."
    return s
