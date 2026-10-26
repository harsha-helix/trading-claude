"""Free end-of-day market data for NSE.

Sources, in order of preference:
  * NSE archives  - NIFTY 500 constituents, daily bhavcopy (raw EOD prices)
  * Yahoo Finance - split-adjusted history, dividends/splits, fundamentals

Everything is cached under data/ so a run only fetches what is new.
Prices are stored long-format: date, symbol, open, high, low, close, volume.
"""
from __future__ import annotations

import io
import json
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd
import requests

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
PRICES = DATA / "prices.csv.gz"
ACTIONS = DATA / "actions.csv"
UNIVERSE = DATA / "nifty500.csv"
FUNDAMENTALS = DATA / "fundamentals.json"

NSE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept": "*/*",
    "Referer": "https://www.nseindia.com/",
}
NIFTY500_URL = "https://nsearchives.nseindia.com/content/indices/ind_nifty500list.csv"
BHAV_URL = "https://nsearchives.nseindia.com/products/content/sec_bhavdata_full_{d:%d%m%Y}.csv"


def _get(url: str, retries: int = 3) -> requests.Response:
    last = None
    for i in range(retries):
        try:
            r = requests.get(url, headers=NSE_HEADERS, timeout=30)
            if r.status_code == 200:
                return r
            last = RuntimeError(f"HTTP {r.status_code} for {url}")
        except requests.RequestException as e:
            last = e
        time.sleep(2 ** i)
    raise last


# ---------------------------------------------------------------- universe
def refresh_universe() -> pd.DataFrame:
    r = _get(NIFTY500_URL)
    df = pd.read_csv(io.StringIO(r.text))
    df = df.rename(columns={"Company Name": "name", "Industry": "industry",
                            "Symbol": "symbol", "Series": "series", "ISIN Code": "isin"})
    DATA.mkdir(exist_ok=True)
    df.to_csv(UNIVERSE, index=False)
    return df


def load_universe() -> pd.DataFrame:
    if not UNIVERSE.exists():
        return refresh_universe()
    return pd.read_csv(UNIVERSE)


# ------------------------------------------------------------------ prices
def load_prices() -> pd.DataFrame:
    if not PRICES.exists():
        return pd.DataFrame(columns=["date", "symbol", "open", "high", "low", "close", "volume"])
    df = pd.read_csv(PRICES, parse_dates=["date"])
    return df


def save_prices(df: pd.DataFrame) -> None:
    DATA.mkdir(exist_ok=True)
    df = df.drop_duplicates(["date", "symbol"], keep="last").sort_values(["symbol", "date"])
    df.to_csv(PRICES, index=False, date_format="%Y-%m-%d")


def fetch_yahoo(symbols: list[str], start: str, end: str | None = None,
                raw: frozenset = frozenset()) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Split-adjusted OHLCV plus dividend/split actions from Yahoo.
    NSE symbols get the .NS suffix; indices (^...) and `raw` symbols are used as-is."""
    import yfinance as yf

    tickers = [s if s.startswith("^") or s in raw else f"{s}.NS" for s in symbols]
    raw = yf.download(tickers, start=start, end=end, auto_adjust=False, actions=True,
                      group_by="ticker", progress=False, threads=True)
    rows, acts = [], []
    for sym, t in zip(symbols, tickers):
        if t not in raw.columns.get_level_values(0):
            continue
        d = raw[t].dropna(subset=["Close"])
        if d.empty:
            continue
        out = pd.DataFrame({
            "date": d.index.tz_localize(None) if d.index.tz is not None else d.index,
            "symbol": sym, "open": d["Open"].values, "high": d["High"].values,
            "low": d["Low"].values, "close": d["Close"].values,
            "volume": d["Volume"].values})
        rows.append(out)
        for col, kind in (("Dividends", "dividend"), ("Stock Splits", "split")):
            if col in d:
                ev = d[d[col] > 0][col]
                for ts, v in ev.items():
                    acts.append({"date": pd.Timestamp(ts).tz_localize(None) if pd.Timestamp(ts).tz else pd.Timestamp(ts),
                                 "symbol": sym, "kind": kind, "value": float(v)})
    prices = pd.concat(rows, ignore_index=True) if rows else pd.DataFrame()
    return prices, pd.DataFrame(acts, columns=["date", "symbol", "kind", "value"])


def fetch_bhavcopy(d: date) -> pd.DataFrame:
    """Raw NSE EOD prices for one day (EQ/BE series). Empty on holidays."""
    try:
        r = _get(BHAV_URL.format(d=d), retries=2)
    except Exception:
        return pd.DataFrame()
    df = pd.read_csv(io.StringIO(r.text))
    df.columns = [c.strip() for c in df.columns]
    df["SERIES"] = df["SERIES"].str.strip()
    df = df[df["SERIES"].isin(["EQ", "BE"])]
    return pd.DataFrame({
        "date": pd.Timestamp(d), "symbol": df["SYMBOL"].str.strip(),
        "open": pd.to_numeric(df["OPEN_PRICE"], errors="coerce"),
        "high": pd.to_numeric(df["HIGH_PRICE"], errors="coerce"),
        "low": pd.to_numeric(df["LOW_PRICE"], errors="coerce"),
        "close": pd.to_numeric(df["CLOSE_PRICE"], errors="coerce"),
        "volume": pd.to_numeric(df["TTL_TRD_QNTY"], errors="coerce"),
    }).dropna()


def update_prices(extra: list[str], history_start: str = "2010-01-01",
                  raw: list[str] = ()) -> pd.DataFrame:
    """Bring the local price cache up to date. Yahoo first, bhavcopy fallback.
    `extra` adds symbols outside the universe (benchmark, liquid ETF)."""
    uni = load_universe()
    raw = frozenset(raw)
    symbols = list(uni["symbol"]) + [s for s in list(extra) + list(raw) if s]
    have = load_prices()
    start = history_start if have.empty else (have["date"].max() - timedelta(days=10)).strftime("%Y-%m-%d")
    end = (date.today() + timedelta(days=1)).isoformat()
    fresh = [] if have.empty else sorted(set(symbols) - set(have["symbol"]))
    if not have.empty and have["date"].min() > pd.Timestamp(history_start) + timedelta(days=30):
        # history_start moved earlier: backfill the gap for everyone
        old, old_acts = fetch_yahoo(symbols, history_start, have["date"].min().strftime("%Y-%m-%d"), raw)
        have = pd.concat([old, have], ignore_index=True)
        if not old_acts.empty:
            prev = pd.read_csv(ACTIONS, parse_dates=["date"]) if ACTIONS.exists() else pd.DataFrame()
            pd.concat([old_acts, prev]).drop_duplicates(["date", "symbol", "kind"]).to_csv(
                ACTIONS, index=False, date_format="%Y-%m-%d")
    try:
        new, acts = fetch_yahoo(symbols, start, end, raw)
        if fresh:  # symbols added since the last run need full history
            n2, a2 = fetch_yahoo(fresh, history_start, end, raw)
            new, acts = pd.concat([new, n2]), pd.concat([acts, a2])
    except Exception as e:  # Yahoo down/blocked: fall back to NSE bhavcopy
        print(f"Yahoo fetch failed ({e}); falling back to NSE bhavcopy")
        new, acts = _bhav_range(pd.Timestamp(start).date(), date.today()), pd.DataFrame()
        new = new[new["symbol"].isin(symbols)]
    if not acts.empty and not have.empty:
        # Yahoo split-adjusts the bars it just sent; older cached bars must be adjusted too
        for _, e in acts[(acts["kind"] == "split") & (acts["date"] >= pd.Timestamp(start))].iterrows():
            m = (have["symbol"] == e["symbol"]) & (have["date"] < pd.Timestamp(start))
            have.loc[m, ["open", "high", "low", "close"]] /= e["value"]
            have.loc[m, "volume"] *= e["value"]
    merged = pd.concat([have, new], ignore_index=True)
    save_prices(merged)
    if not acts.empty:
        old = pd.read_csv(ACTIONS, parse_dates=["date"]) if ACTIONS.exists() else pd.DataFrame()
        pd.concat([old, acts]).drop_duplicates(["date", "symbol", "kind"]).to_csv(
            ACTIONS, index=False, date_format="%Y-%m-%d")
    return load_prices()


def _bhav_range(a: date, b: date) -> pd.DataFrame:
    frames, d = [], a
    while d <= b:
        if d.weekday() < 5:
            f = fetch_bhavcopy(d)
            if not f.empty:
                frames.append(f)
        d += timedelta(days=1)
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def load_actions() -> pd.DataFrame:
    if not ACTIONS.exists():
        return pd.DataFrame(columns=["date", "symbol", "kind", "value"])
    return pd.read_csv(ACTIONS, parse_dates=["date"])


# ------------------------------------------------------------ fundamentals
FUND_FIELDS = ["trailingPE", "priceToBook", "returnOnEquity", "debtToEquity",
               "earningsGrowth", "revenueGrowth", "marketCap", "sector"]


def update_fundamentals(symbols: list[str], max_age_days: int = 7) -> dict:
    """Cached Yahoo fundamentals; refreshed weekly per symbol."""
    import yfinance as yf

    cache = json.loads(FUNDAMENTALS.read_text()) if FUNDAMENTALS.exists() else {}
    cutoff = (datetime.now() - timedelta(days=max_age_days)).isoformat()
    for s in symbols:
        if s in cache and cache[s].get("_asof", "") > cutoff:
            continue
        try:
            info = yf.Ticker(f"{s}.NS").info
            cache[s] = {k: info.get(k) for k in FUND_FIELDS}
            cache[s]["_asof"] = datetime.now().isoformat(timespec="seconds")
        except Exception as e:
            print(f"fundamentals {s}: {e}")
    DATA.mkdir(exist_ok=True)
    FUNDAMENTALS.write_text(json.dumps(cache, indent=1))
    return cache


def load_fundamentals() -> dict:
    return json.loads(FUNDAMENTALS.read_text()) if FUNDAMENTALS.exists() else {}
