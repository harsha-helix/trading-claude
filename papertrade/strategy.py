"""Technical scanner with a fundamental filter.

Two long-only setups on daily bars, both only in established uptrends:

  breakout  - close above the prior 55-day high on >=1.5x average volume
  pullback  - dip to the 20-day EMA, then a close above the prior day's high

Stops are ATR-based, targets are a fixed R-multiple. Candidates are ranked
by 3-month relative strength against the benchmark.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

PARAMS = {
    "min_history": 210,
    "min_avg_traded_value": 10e7,   # ₹10 crore/day
    "min_price": 50.0,
    "breakout_lookback": 55,
    "breakout_vol_mult": 1.5,
    "breakout_atr_stop": 2.0,
    "pullback_touch_days": 3,
    "pullback_rsi": (40, 65),
    "pullback_atr_buffer": 0.5,
    "max_stop_atr": 3.0,
    "target_r": 2.5,
    "max_candidates": 10,
    # fundamental filter (applied only when data exists)
    "min_roe": 0.12,
    "max_debt_to_equity": 150.0,
}


def indicators(g: pd.DataFrame) -> pd.DataFrame:
    g = g.sort_values("date").copy()
    c, h, l = g["close"], g["high"], g["low"]
    g["sma50"] = c.rolling(50).mean()
    g["sma200"] = c.rolling(200).mean()
    g["ema20"] = c.ewm(span=20, adjust=False).mean()
    prev = c.shift()
    tr = pd.concat([h - l, (h - prev).abs(), (l - prev).abs()], axis=1).max(axis=1)
    g["atr14"] = tr.ewm(alpha=1 / 14, adjust=False).mean()
    delta = c.diff()
    up = delta.clip(lower=0).ewm(alpha=1 / 14, adjust=False).mean()
    dn = (-delta.clip(upper=0)).ewm(alpha=1 / 14, adjust=False).mean()
    g["rsi14"] = 100 - 100 / (1 + up / dn.replace(0, np.nan))
    g["hh55"] = h.shift().rolling(PARAMS["breakout_lookback"]).max()
    g["vol20"] = g["volume"].shift().rolling(20).mean()
    g["tv20"] = (c * g["volume"]).rolling(20).mean()
    g["ret63"] = c / c.shift(63) - 1
    g["low5"] = l.rolling(5).min()
    g["touched_ema"] = (l <= g["ema20"] * 1.01).rolling(PARAMS["pullback_touch_days"]).max()
    g["sma50_rising"] = g["sma50"] > g["sma50"].shift(10)
    return g


def _fundamental_ok(f: dict | None) -> tuple[bool, str]:
    if not f:
        return True, "no fundamentals cached"
    roe, de = f.get("returnOnEquity"), f.get("debtToEquity")
    if roe is not None and roe < PARAMS["min_roe"]:
        return False, f"ROE {roe:.0%} < {PARAMS['min_roe']:.0%}"
    if de is not None and de > PARAMS["max_debt_to_equity"]:
        return False, f"D/E {de:.0f}% too high"
    bits = []
    if roe is not None:
        bits.append(f"ROE {roe:.0%}")
    if de is not None:
        bits.append(f"D/E {de:.0f}%")
    if f.get("trailingPE"):
        bits.append(f"P/E {f['trailingPE']:.1f}")
    if f.get("earningsGrowth") is not None:
        bits.append(f"EPS growth {f['earningsGrowth']:.0%}")
    return True, ", ".join(bits) or "fundamentals n/a"


def scan(prices: pd.DataFrame, asof: pd.Timestamp, benchmark: str,
         fundamentals: dict | None = None, exclude: set[str] = frozenset()) -> list[dict]:
    """Return ranked trade candidates using only data up to and including `asof`."""
    p = prices[prices["date"] <= asof]
    bench = p[p["symbol"] == benchmark].sort_values("date")
    bench_ret63 = (bench["close"].iloc[-1] / bench["close"].iloc[-64] - 1) if len(bench) > 64 else 0.0
    fundamentals = fundamentals or {}
    out = []
    for sym, g in p.groupby("symbol"):
        if sym == benchmark or sym in exclude or len(g) < PARAMS["min_history"]:
            continue
        g = indicators(g.tail(300))
        t = g.iloc[-1]
        if t["date"] != asof:
            continue  # not traded today (suspended / holiday mismatch)
        if t["close"] < PARAMS["min_price"] or t["tv20"] < PARAMS["min_avg_traded_value"]:
            continue
        uptrend = t["close"] > t["sma200"] and t["sma50"] > t["sma200"] and t["sma50_rising"]
        if not uptrend:
            continue
        y = g.iloc[-2]
        setup, stop, why = None, None, ""
        atr = t["atr14"]
        if t["close"] > t["hh55"] and t["volume"] >= PARAMS["breakout_vol_mult"] * t["vol20"]:
            setup = "breakout"
            stop = t["close"] - PARAMS["breakout_atr_stop"] * atr
            why = (f"Closed at {t['close']:.2f}, above its 55-day high of {t['hh55']:.2f}, "
                   f"on {t['volume'] / t['vol20']:.1f}x average volume.")
        elif (t["touched_ema"] == 1 and t["close"] > y["high"]
              and PARAMS["pullback_rsi"][0] <= t["rsi14"] <= PARAMS["pullback_rsi"][1]):
            setup = "pullback"
            stop = min(t["low5"], t["ema20"]) - PARAMS["pullback_atr_buffer"] * atr
            why = (f"Dipped to its 20-day EMA ({t['ema20']:.2f}) in an uptrend, then closed above "
                   f"the prior day's high ({y['high']:.2f}). RSI {t['rsi14']:.0f}.")
        if not setup:
            continue
        stop = max(stop, t["close"] - PARAMS["max_stop_atr"] * atr)
        risk = t["close"] - stop
        if risk <= 0:
            continue
        ok, fnote = _fundamental_ok(fundamentals.get(sym))
        if not ok:
            continue
        rs = t["ret63"] - bench_ret63
        out.append({
            "symbol": sym, "setup": setup, "asof": asof.strftime("%Y-%m-%d"),
            "ref_price": round(float(t["close"]), 2),
            "stop": round(float(stop), 2),
            "target": round(float(t["close"] + PARAMS["target_r"] * risk), 2),
            "atr": round(float(atr), 2), "rsi": round(float(t["rsi14"]), 1),
            "rel_strength_63d": round(float(rs), 4),
            "trend": (f"Above its 50-day ({t['sma50']:.0f}) and 200-day ({t['sma200']:.0f}) averages, "
                      f"and the 50-day is rising. 3-month return is {t['ret63']:.1%} against "
                      f"{bench_ret63:.1%} for the index."),
            "technical": why,
            "fundamental": fnote,
        })
    out.sort(key=lambda x: x["rel_strength_63d"], reverse=True)
    return out[:PARAMS["max_candidates"]]


def exit_signals(prices: pd.DataFrame, asof: pd.Timestamp, symbols: list[str]) -> dict[str, str]:
    """Discretionary exit suggestions (need approval): trend broken."""
    p = prices[(prices["date"] <= asof) & prices["symbol"].isin(symbols)]
    out = {}
    for sym, g in p.groupby("symbol"):
        g = indicators(g.tail(260))
        t = g.iloc[-1]
        if t["date"] == asof and t["close"] < t["sma50"]:
            out[sym] = f"Closed at {t['close']:.2f}, below its 50-day average ({t['sma50']:.2f}). The trend is weakening."
    return out
