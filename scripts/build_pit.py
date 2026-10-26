"""Build a survivorship-free price panel from NSE daily bhavcopies.

Downloads every daily bhavcopy (all listed stocks, including ones later delisted),
back-adjusts prices for splits/bonuses using NSE's adjusted previous close, and
builds a point-in-time universe: at each month end, the 500 stocks with the
highest 6-month average traded value (a liquidity proxy for the NIFTY 500).

  python scripts/build_pit.py download --start 2010-01-01
  python scripts/build_pit.py build

Outputs (gitignored, rebuildable):
  data/pit/raw/<year>.csv.gz     parsed bhavcopy rows (EQ/BE equities only)
  data/pit/prices.csv.gz         adjusted OHLCV, same columns as data/prices.csv.gz
  data/pit/members.csv           month, symbol: point-in-time universe
"""
from __future__ import annotations

import argparse
import io
import os
import sys
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import requests

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "data" / "pit" / "raw"
# PIT_DIR lets a robustness build write elsewhere (e.g. data/pit_pessimistic)
PIT = Path(os.environ.get("PIT_DIR", ROOT / "data" / "pit"))
HEAD = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/124.0 Safari/537.36",
        "Referer": "https://www.nseindia.com/"}
NEW_FORMAT_FROM = date(2024, 7, 8)
# price ratios of common Indian corporate actions: bonus 1:4 (1.25), 1:3, 1:2 (1.5), 2:3,
# 1:1 (2), 3:2 (2.5), 2:1 (3), splits 1:2 .. 1:100
RATIOS = [1.1, 1.2, 1.25, 4 / 3, 1.5, 5 / 3, 2, 2.5, 3, 4, 5, 6, 8, 10, 20, 25, 50, 100]
MON = "JAN FEB MAR APR MAY JUN JUL AUG SEP OCT NOV DEC".split()


def url(d: date) -> str:
    if d >= NEW_FORMAT_FROM:
        return f"https://nsearchives.nseindia.com/content/cm/BhavCopy_NSE_CM_0_0_0_{d:%Y%m%d}_F_0000.csv.zip"
    return (f"https://nsearchives.nseindia.com/content/historical/EQUITIES/{d.year}/{MON[d.month - 1]}/"
            f"cm{d:%d}{MON[d.month - 1]}{d.year}bhav.csv.zip")


def parse(d: date, blob: bytes) -> pd.DataFrame:
    z = zipfile.ZipFile(io.BytesIO(blob))
    df = pd.read_csv(z.open(z.namelist()[0]))
    if d >= NEW_FORMAT_FROM:
        df = df.rename(columns={"TckrSymb": "symbol", "SctySrs": "series", "OpnPric": "open", "HghPric": "high",
                                "LwPric": "low", "ClsPric": "close", "PrvsClsgPric": "prevclose",
                                "TtlTradgVol": "volume", "TtlTrfVal": "value"})
    else:
        df = df.rename(columns={"SYMBOL": "symbol", "SERIES": "series", "OPEN": "open", "HIGH": "high",
                                "LOW": "low", "CLOSE": "close", "PREVCLOSE": "prevclose",
                                "TOTTRDQTY": "volume", "TOTTRDVAL": "value"})
    df["series"] = df["series"].astype(str).str.strip()
    # files before ~2011 have no ISIN; build() drops funds there by symbol
    df["ISIN"] = df["ISIN"].astype(str) if "ISIN" in df else ""
    # ordinary equity only: EQ/BE series and company ISINs (INE...), not ETFs/funds (INF...)
    df = df[df["series"].isin(["EQ", "BE"]) & (df["ISIN"].str.startswith("INE") | (df["ISIN"] == ""))]
    df["date"] = pd.Timestamp(d)
    df["symbol"] = df["symbol"].astype(str).str.strip()
    return df[["date", "symbol", "ISIN", "open", "high", "low", "close", "prevclose", "volume", "value"]]


def fetch(d: date) -> pd.DataFrame | None:
    for i in range(4):
        try:
            r = requests.get(url(d), headers=HEAD, timeout=30)
        except requests.RequestException:
            time.sleep(2 ** i)
            continue
        if r.status_code == 404:
            return None  # holiday
        if r.status_code == 200:
            return parse(d, r.content)  # a parse error is a bug: let it raise
        time.sleep(2 ** i)
    print(f"failed {d}", file=sys.stderr)
    return None


def download(start: date, end: date) -> None:
    RAW.mkdir(parents=True, exist_ok=True)
    for year in range(start.year, end.year + 1):
        out = RAW / f"{year}.csv.gz"
        a, b = max(start, date(year, 1, 1)), min(end, date(year, 12, 31))
        have = pd.read_csv(out, parse_dates=["date"]) if out.exists() else pd.DataFrame()
        done = set(have["date"].dt.date) if not have.empty else set()
        days = [a + timedelta(i) for i in range((b - a).days + 1)]
        # weekends too: NSE holds special Saturday/Sunday sessions (budget day, Muhurat
        # trading, DR drills); missing one makes the next day's prevclose look like an adjustment
        days = [d for d in days if d not in done]
        if not days:
            continue
        with ThreadPoolExecutor(4) as ex:
            frames = [f for f in ex.map(fetch, days) if f is not None]
        if not frames:
            continue
        allf = pd.concat([have] + frames, ignore_index=True)
        allf.sort_values(["date", "symbol"]).to_csv(out, index=False, date_format="%Y-%m-%d")
        print(f"{year}: {len(frames)} new sessions, {allf['date'].nunique()} total", flush=True)


def build(top_n: int = 500, lookback_days: int = 126, min_history: int = 250) -> None:
    raw = pd.concat([pd.read_csv(f, parse_dates=["date"]) for f in sorted(RAW.glob("*.csv.gz"))],
                    ignore_index=True)
    raw = raw.drop_duplicates(["date", "symbol"]).sort_values(["symbol", "date"])
    raw["ISIN"] = raw["ISIN"].fillna("").astype(str)
    funds = set(raw.loc[raw["ISIN"].str.startswith("INF"), "symbol"])
    raw = raw[~raw["symbol"].isin(funds)]
    # Corporate actions (splits, bonuses, consolidations). The ratio is read off the
    # overnight gap (nearest standard ratio); the evidence that it IS a corporate action:
    #   1. Yahoo lists a split for that symbol on that day (stocks still listed), or
    #   2. NSE's PREVCLOSE was adjusted by about the same ratio, or
    #   3. a gap of 40%+ within 5% of a standard ratio (stocks Yahoo no longer has).
    # A real crash wrongly adjusted would hide a loss; a missed small bonus on a delisted
    # stock only makes results look worse. So rule 3 is deliberately strict.
    g = raw.groupby("symbol")
    prev_close = g["close"].shift()
    r_open = raw["open"] / prev_close
    ks = np.array([1 / k for k in RATIOS] + RATIOS)
    nearest = ks[np.abs(np.log(r_open.fillna(1).values[:, None] / ks[None, :])).argmin(axis=1)]
    off = (r_open / nearest - 1).abs()
    acts = pd.read_csv(ROOT / "data/actions.csv", parse_dates=["date"])
    ysplit = set(zip(acts.loc[acts["kind"] == "split", "symbol"], acts.loc[acts["kind"] == "split", "date"]))
    yahoo = pd.Series([(s, d) in ysplit for s, d in zip(raw["symbol"], raw["date"])], index=raw.index)
    nse = ((raw["prevclose"] / prev_close) / nearest - 1).abs() < 0.03
    gap = (r_open < 0.78) | (r_open > 1.28)
    strict = ((r_open < 0.6) | (r_open > 1.6)) & (off < 0.05)
    # a Yahoo-confirmed date allows a looser match: the stock can move sharply that day too
    # small bonuses (1:10, 1:5, 1:4) sit inside the daily price band, so they are only
    # taken on a Yahoo-confirmed date
    if os.environ.get("PIT_PESSIMISTIC"):
        strict = strict & False  # robustness: never infer a split without Yahoo/NSE evidence
    is_ca = ((off < 0.12) & yahoo & (np.abs(np.log(r_open)) > 0.06)) | (gap & (((off < 0.08) & nse) | strict))
    factor = pd.Series(np.where(is_ca, nearest, 1.0), index=raw.index)
    # adjust everything before day t by factor_t: cumulative product of later factors
    raw["adj"] = factor.groupby(raw["symbol"]).transform(
        lambda f: f[::-1].cumprod()[::-1].shift(-1, fill_value=1.0))
    for c in ("open", "high", "low", "close"):
        raw[c] = raw[c] * raw["adj"]
    raw["volume"] = raw["volume"] / raw["adj"]
    # point-in-time universe at each month end
    tv = raw.pivot_table(index="date", columns="symbol", values="value", aggfunc="sum")
    hist = tv.notna().cumsum()
    avg = tv.fillna(0).rolling(lookback_days, min_periods=lookback_days // 2).mean()
    rows = []
    for m, idx in avg.groupby(avg.index.to_period("M")).groups.items():
        d = idx.max()
        s = avg.loc[d][hist.loc[d] >= min_history].dropna().nlargest(top_n)
        rows += [{"month": str(m), "symbol": sym} for sym in s.index]
    PIT.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows).to_csv(PIT / "members.csv", index=False)
    ever = {r["symbol"] for r in rows}  # only stocks that were ever in the universe are needed
    prices = raw.loc[raw["symbol"].isin(ever), ["date", "symbol", "open", "high", "low", "close", "volume"]]
    prices.to_csv(PIT / "prices.csv.gz", index=False, date_format="%Y-%m-%d")
    n_events = int((factor != 1.0).sum())
    print(f"prices: {len(prices):,} rows, {prices['symbol'].nunique()} symbols; "
          f"{n_events} corporate-action adjustments; members for {len(rows) // top_n} months")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["download", "build"])
    ap.add_argument("--start", default="2010-01-01")
    ap.add_argument("--end", default=date.today().isoformat())
    a = ap.parse_args()
    if a.cmd == "download":
        download(date.fromisoformat(a.start), date.fromisoformat(a.end))
    else:
        build()
