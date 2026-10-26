"""Walk-forward backtest over cached history, in a throwaway state dir.

  python scripts/backtest.py --start 2024-01-01 --end 2026-09-30 [--set target_r=3 ...]

Fundamentals are NOT used: today's snapshot would leak the future into the past.
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
from collections import defaultdict
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from papertrade import data, engine, portfolio, report, strategy  # noqa: E402


def run(start: str, end: str, overrides: dict, prices=None, actions=None, quiet=False,
        ind=None) -> dict:
    tmp = Path(tempfile.mkdtemp())
    portfolio.STATE = engine.STATE = tmp
    engine.PROPOSALS, engine.APPROVALS, engine.META = tmp / "p.json", tmp / "a.json", tmp / "m.json"
    saved = dict(strategy.PARAMS)
    cfg = engine.load_cfg()
    for k, v in overrides.items():
        if k in strategy.PARAMS:
            cur = strategy.PARAMS[k]
            if isinstance(cur, bool):
                v = v if isinstance(v, bool) else str(v).lower() in ("1", "true", "yes")
            elif not isinstance(cur, tuple):
                v = type(cur)(v)
            strategy.PARAMS[k] = v
        elif k in cfg["risk"]:
            cfg["risk"][k] = float(v)
        elif k.startswith("core_"):
            cur = cfg["core"][k[5:]]
            cfg["core"][k[5:]] = (str(v).lower() in ("1", "true", "yes")) if isinstance(cur, bool) else type(cur)(v)
    cfg["start_date"] = start
    prices = data.load_prices() if prices is None else prices
    actions = data.load_actions() if actions is None else actions
    uni = data.load_universe()
    e = engine.Engine(prices, actions, {}, cfg, universe=set(uni["symbol"]),
                      sectors=dict(zip(uni["symbol"], uni["industry"])), ind=ind)
    e.run(until=pd.Timestamp(end), auto_approve=True)
    strategy.PARAMS.clear()
    strategy.PARAMS.update(saved)
    bench = e.meta.get("benchmark", {})
    res = {}
    for name, a in e.accounts.items():
        m = report.metrics(a, cfg, bench)
        by = defaultdict(list)
        for t in a.closed:
            by[t["setup"]].append(t)
        m["by_setup"] = {
            s: {"n": len(ts), "win": round(sum(t["net_pnl"] > 0 for t in ts) / len(ts), 2),
                "avg_r": round(sum(t["r_multiple"] or 0 for t in ts) / len(ts), 2),
                "net": round(sum(t["net_pnl"] for t in ts))}
            for s, ts in by.items()}
        by_reason = defaultdict(int)
        for t in a.closed:
            by_reason[t["reason"]] += 1
        m["exits"] = dict(by_reason)
        m.pop("est_tax", None)
        res[name] = m
    if not quiet:
        print(json.dumps(res, indent=1))
    return res


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", required=True)
    ap.add_argument("--end", required=True)
    ap.add_argument("--set", nargs="*", default=[])
    a = ap.parse_args()
    run(a.start, a.end, dict(kv.split("=") for kv in a.set))
