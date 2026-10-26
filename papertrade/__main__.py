"""Command line entry point:  python -m papertrade <command>

  update-data            refresh universe, prices, actions, fundamentals
  backfill --until DATE  walk-forward run from start_date with proposals auto-approved
  run                    process new trading days (live mode, needs approvals)
  approve ID [ID ...]    record approval(s); --reject to reject
  note ID TEXT           attach a news/event check to a proposal
  report                 print summary and write reports/dashboard.json
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime

import pandas as pd

from . import data, report
from .engine import APPROVALS, IST, PROPOSALS, Engine, _read, _write, load_cfg


def _engine() -> Engine:
    return Engine(data.load_prices(), data.load_actions(), data.load_fundamentals())


def main() -> None:
    ap = argparse.ArgumentParser(prog="papertrade")
    sub = ap.add_subparsers(dest="cmd", required=True)
    u = sub.add_parser("update-data")
    u.add_argument("--no-fundamentals", action="store_true")
    b = sub.add_parser("backfill")
    b.add_argument("--until", required=True)
    sub.add_parser("run")
    a = sub.add_parser("approve")
    a.add_argument("ids", nargs="+")
    a.add_argument("--reject", action="store_true")
    a.add_argument("--by", default="you")
    a.add_argument("--at", help="ISO time of the decision (default: now, IST)")
    n = sub.add_parser("note")
    n.add_argument("id")
    n.add_argument("text")
    sub.add_parser("report")
    args = ap.parse_args()
    cfg = load_cfg()

    if args.cmd == "update-data":
        data.refresh_universe()
        prices = data.update_prices([cfg["benchmark"], cfg.get("cash_management", {}).get("liquid_etf"),
                                     cfg.get("core", {}).get("etf")],
                                    raw=list(cfg.get("global_symbols", {}).values()))
        print(f"prices: {len(prices):,} rows, {prices['symbol'].nunique()} symbols, "
              f"latest {prices['date'].max():%Y-%m-%d}")
        if not args.no_fundamentals:
            data.update_fundamentals(list(data.load_universe()["symbol"]))
    elif args.cmd == "backfill":
        days = _engine().run(until=pd.Timestamp(args.until), auto_approve=True)
        print(f"backfilled {len(days)} trading days")
        print(report.text_summary(report.export()))
    elif args.cmd == "run":
        days = _engine().run()
        print(f"processed {len(days)} new trading day(s)")
        print(report.text_summary(report.export()))
    elif args.cmd == "approve":
        apr = _read(APPROVALS, {})
        at = args.at or datetime.now(IST).isoformat(timespec="seconds")
        known = {p["id"] for p in _read(PROPOSALS, [])}
        for i in args.ids:
            if i not in known:
                print(f"unknown proposal {i}")
                continue
            apr[i] = {"decision": "reject" if args.reject else "approve", "by": args.by, "at": at}
        _write(APPROVALS, apr)
    elif args.cmd == "note":
        props = _read(PROPOSALS, [])
        for p in props:
            if p["id"] == args.id:
                p["news"] = args.text
        _write(PROPOSALS, props)
    elif args.cmd == "report":
        print(report.text_summary(report.export()))


if __name__ == "__main__":
    main()
