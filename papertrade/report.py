"""Performance metrics, go-live check and the dashboard data export."""
from __future__ import annotations

import json
from pathlib import Path

from . import costs as C
from .engine import META, PROPOSALS, ROOT, load_cfg
from .portfolio import STATE, Account

DASHBOARD = ROOT / "reports" / "dashboard.json"


def _twr(equity: list[dict]) -> list[float]:
    """Time-weighted return index, so monthly top-ups don't count as gains."""
    if not equity:
        return []
    idx, out = 1.0, []
    prev = {"equity": equity[0]["contributed"], "contributed": equity[0]["contributed"]}
    for e in equity:
        flow = e["contributed"] - prev["contributed"]
        base = prev["equity"] + flow
        if base > 0:
            idx *= e["equity"] / base
        out.append(idx)
        prev = e
    return out


def metrics(a: Account, cfg: dict, bench: dict[str, float]) -> dict:
    eq = a.equity
    closed = a.closed
    wins = [t for t in closed if t["net_pnl"] > 0]
    losses = [t for t in closed if t["net_pnl"] <= 0]
    twr = _twr(eq)
    peak, mdd = 0.0, 0.0
    for v in twr:
        peak = max(peak, v)
        mdd = max(mdd, 1 - v / peak if peak else 0)
    first = eq[0]["date"] if eq else ""
    before = [k for k in sorted(bench) if k < first]
    b = ([bench[before[-1]]] if before else []) + [bench[e["date"]] for e in eq if bench.get(e["date"])]
    bench_ret = (b[-1] / b[0] - 1) if len(b) > 1 else 0.0
    last = eq[-1] if eq else {"equity": a.cash, "contributed": a.contributed}
    rs = [t["r_multiple"] for t in closed if t["r_multiple"] is not None]
    tax = C.estimate_tax(closed + a.core_realised, cfg["tax"])
    liquid_gain = sum(x["gain"] for x in a.liquid_realised)
    slab = cfg.get("cash_management", {}).get("slab_tax_pct", 30.0)
    tax["liquid_gain"] = round(liquid_gain, 2)
    tax["liquid_tax"] = round(max(0.0, liquid_gain) * slab / 100, 2)
    tax["total_tax"] = round(tax["total_tax"] + tax["liquid_tax"], 2)
    charges = (sum(t["charges"] for t in closed) + sum(p.entry_charges for p in a.positions)
               + sum(e["charges"]["total"] for e in a.events if e["kind"] in ("park", "unpark", "core")))
    return {
        "equity": last["equity"], "contributed": a.contributed, "cash": round(a.cash, 2),
        "liquid": last.get("liquid", 0.0), "liquid_units": a.liquid_units,
        "core": last.get("core", 0.0), "core_units": a.core_units,
        "pnl": round(last["equity"] - a.contributed, 2),
        "return_twr": round(twr[-1] - 1, 4) if twr else 0.0,
        "benchmark_return": round(bench_ret, 4),
        "max_drawdown": round(mdd, 4),
        "trades": len(closed), "open": len(a.positions),
        "win_rate": round(len(wins) / len(closed), 3) if closed else None,
        "avg_win": round(sum(t["net_pnl"] for t in wins) / len(wins), 2) if wins else None,
        "avg_loss": round(sum(t["net_pnl"] for t in losses) / len(losses), 2) if losses else None,
        "expectancy_r": round(sum(rs) / len(rs), 2) if rs else None,
        "charges_paid": round(charges, 2),
        "est_tax": tax,
        "days": len(eq),
    }


def go_live(m: dict, cfg: dict) -> dict:
    g = cfg["go_live"]
    checks = {
        f"{g['min_days']}+ trading days": m["days"] >= g["min_days"] * 5 / 7,
        f"max drawdown < {g['max_drawdown_pct']}%": m["max_drawdown"] * 100 < g["max_drawdown_pct"],
        "beats NIFTY 500 after costs": m["return_twr"] > m["benchmark_return"],
    }
    return {"checks": checks, "ready": all(checks.values())}


def export() -> dict:
    cfg = load_cfg()
    meta = json.loads(META.read_text()) if META.exists() else {}
    bench = meta.get("benchmark", {})
    props = json.loads(PROPOSALS.read_text()) if PROPOSALS.exists() else []
    accounts = {}
    for name in cfg["accounts"]:
        if not (STATE / f"{name}.json").exists():
            continue
        a = Account.load(name)
        m = metrics(a, cfg, bench)
        accounts[name] = {
            "label": a.label, "metrics": m, "go_live": go_live(m, cfg),
            "positions": [p.__dict__ for p in a.positions],
            "closed": a.closed, "events": a.events[-200:], "equity": a.equity,
        }
    out = {"asof": meta.get("last_processed"), "live_from": cfg["live_from"],
           "regime": meta.get("regime"), "watchlist": meta.get("watchlist", []),
           "accounts": accounts, "benchmark": bench,
           "pending": [p for p in props if p["status"] == "pending"],
           "recent_proposals": props[-150:]}
    DASHBOARD.parent.mkdir(exist_ok=True)
    DASHBOARD.write_text(json.dumps(out, indent=1, ensure_ascii=False, default=str))
    return out


def text_summary(d: dict) -> str:
    lines = [f"As of {d['asof']}"]
    for name, a in d["accounts"].items():
        m = a["metrics"]
        lines.append(
            f"\n{a['label']}: equity ₹{m['equity']:,.0f} (deposited ₹{m['contributed']:,.0f}, "
            f"P&L ₹{m['pnl']:,.0f}) | return {m['return_twr']:+.2%} vs NIFTY 500 "
            f"{m['benchmark_return']:+.2%} | max drawdown {m['max_drawdown']:.1%} | "
            f"{m['trades']} closed, {m['open']} open | win rate "
            f"{(m['win_rate'] or 0):.0%} | charges ₹{m['charges_paid']:,.0f}")
        for p in a["positions"]:
            lines.append(f"   open: {p['qty']} {p['symbol']} @ {p['entry_price']} "
                         f"stop {p['stop']} target {p['target']}")
    r = d.get("regime") or {}
    lines.append(f"\nMarket ({r.get('date')}): {r.get('text', '')}")
    if d.get("watchlist"):
        lines.append("Watchlist (would qualify if the filter were on): " + ", ".join(
            f"{w['symbol']} ({w['setup']})" for w in d["watchlist"]))
    if d["pending"]:
        lines.append("\nPending proposals:")
        for p in d["pending"]:
            sz = ", ".join(f"{k}: {v.get('qty', 0)}" for k, v in p.get("sizing", {}).items())
            lines.append(f"  {p['type']} {p['symbol']} {p.get('setup', '')} ref {p.get('ref_price', '')} "
                         f"stop {p.get('stop', '')} target {p.get('target', '')} [{sz}]")
    return "\n".join(lines)
