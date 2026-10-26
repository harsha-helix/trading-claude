"""Compare strategy variants on an in-sample and an out-of-sample period.

  python scripts/compare.py > reports/compare.txt
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import backtest  # noqa: E402
from papertrade import data, strategy  # noqa: E402

PERIODS = [("IS 2011-2018", "2011-01-01", "2018-12-31"), ("OOS 2019-2026", "2019-01-01", "2026-09-30")]
Q = {"market_model": "quadrant"}
VARIANTS = {
    "v2: 200-day on/off, no core": {},
    "Q: multi-timeframe model, no core": dict(Q),
    "Q + core 60% constant": dict(Q, core_enabled=True, core_mode="constant"),
    "Q + core scaled by regime": dict(Q, core_enabled=True, core_mode="regime"),
    "Q + core 60% + sector tilt": dict(Q, core_enabled=True, core_mode="constant", sector_tilt=True),
    "Q (India only) + core 60%": dict(Q, core_enabled=True, core_mode="constant", use_global=False),
}

if __name__ == "__main__":
    pit = "--pit" in sys.argv
    if pit:  # survivorship-free: point-in-time universe, split-adjusted bhavcopy prices
        import pandas as pd
        P, members = backtest.load_pit()
        A = pd.DataFrame(columns=["date", "symbol", "kind", "value"])
        PERIODS.append(("2024-2026", "2024-01-01", "2026-09-30"))
    else:
        P, A, members = data.load_prices(), data.load_actions(), None
    ind = strategy.compute_all(P)
    out = {}
    for lab, s, e in PERIODS:
        for name, ov in VARIANTS.items():
            r = backtest.run(s, e, dict(ov), P, A, quiet=True, ind=ind, members=members)
            out[f"{lab} | {name}"] = r
            for acct, m in r.items():
                yrs = m["days"] / 252
                cagr = (1 + m["return_twr"]) ** (1 / yrs) - 1
                bcagr = (1 + m["benchmark_return"]) ** (1 / yrs) - 1
                print(f"{lab:14s} {name:36s} {acct:9s} {cagr:+6.1%}/yr  NIFTY500 {bcagr:+6.1%}/yr  "
                      f"maxDD {m['max_drawdown']:5.1%}  trades {m['trades']:4d}  win {m['win_rate'] or 0:4.0%}  "
                      f"expR {m['expectancy_r']}  charges {m['charges_paid']:,.0f}", flush=True)
    Path(__file__).resolve().parent.parent.joinpath("reports", "compare_pit.json" if pit else "compare.json").write_text(json.dumps(out, indent=1, default=str))
