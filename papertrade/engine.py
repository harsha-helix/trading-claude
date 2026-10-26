"""Daily simulation loop.

For each trading day D (in order), using only data available at that point:
  1. corporate actions with ex-date D (splits, dividends)
  2. monthly top-up on the first trading day of a new month
  3. proposals made on the previous evening: approved ones fill at D's open
     (plus slippage); everything else expires
  4. stop / target / time-stop exits against D's open, high, low and close
  5. mark to market at D's close
  6. new proposals for D+1, built from data up to D's close
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

import bisect
import math

from . import regime, risk, strategy
from .portfolio import STATE, Account

ROOT = Path(__file__).resolve().parent.parent
IST = timezone(timedelta(hours=5, minutes=30))
PROPOSALS = STATE / "proposals.json"
APPROVALS = STATE / "approvals.json"
META = STATE / "meta.json"


def load_cfg() -> dict:
    return json.loads((ROOT / "config.json").read_text())


def _read(path: Path, default):
    return json.loads(path.read_text()) if path.exists() else default


def _write(path: Path, obj) -> None:
    STATE.mkdir(exist_ok=True)
    path.write_text(json.dumps(obj, indent=1, ensure_ascii=False))


class Engine:
    def __init__(self, prices: pd.DataFrame, actions: pd.DataFrame, fundamentals: dict,
                 cfg: dict | None = None, universe: set[str] | None = None,
                 sectors: dict[str, str] | None = None, ind: pd.DataFrame | None = None):
        self.cfg = cfg or load_cfg()
        self.prices = prices
        self.actions = actions
        self.fund = fundamentals
        self.ind = strategy.compute_all(prices) if ind is None else ind
        g = self.cfg.get("global_symbols", {})
        self.special = {self.cfg["benchmark"], self.cfg.get("cash_management", {}).get("liquid_etf", ""),
                        self.cfg.get("core", {}).get("etf", ""), *g.values()}
        self.universe = universe or {s for s in prices["symbol"].unique()
                                     if s not in self.special and not s.startswith("^")}
        self.sectors = sectors or {}
        self.regime = None
        if strategy.PARAMS["market_model"] == "quadrant":
            self.regime = regime.compute(prices, self.ind, self.cfg["benchmark"], self.universe, g,
                                         strategy.PARAMS["use_global"])
        self.bars = prices.set_index(["date", "symbol"]).sort_index()
        bench = prices[prices["symbol"] == self.cfg["benchmark"]]
        self.days = [pd.Timestamp(x) for x in sorted(
            bench["date"].unique() if not bench.empty else prices["date"].unique())]
        self.meta = _read(META, {"last_processed": None})
        self.proposals: list[dict] = _read(PROPOSALS, [])
        self.approvals: dict = _read(APPROVALS, {})
        self.accounts = self._load_accounts()

    # ------------------------------------------------------------------ setup
    def _load_accounts(self) -> dict[str, Account]:
        out = {}
        for name, acfg in self.cfg["accounts"].items():
            if (STATE / f"{name}.json").exists():
                out[name] = Account.load(name)
            else:
                out[name] = Account.new(name, acfg, self.cfg["start_date"])
            out[name].liquid_symbol = self.cfg.get("cash_management", {}).get("liquid_etf", "")
            out[name].core_symbol = self.cfg.get("core", {}).get("etf", "")
        return out

    def save(self) -> None:
        for a in self.accounts.values():
            a.save()
        _write(PROPOSALS, self.proposals)
        _write(META, self.meta)

    # ---------------------------------------------------------------- helpers
    def bar(self, day, sym) -> pd.Series | None:
        try:
            return self.bars.loc[(day, sym)]
        except KeyError:
            return None

    def closes(self, day) -> dict[str, float]:
        try:
            return self.bars.loc[day]["close"].to_dict()
        except KeyError:
            return {}

    def tdays_between(self, a: str, b) -> int:
        a, b = pd.Timestamp(a), pd.Timestamp(b)
        return max(0, bisect.bisect_right(self.days, b) - bisect.bisect_right(self.days, a))

    def prev_day(self, d: pd.Timestamp) -> pd.Timestamp | None:
        i = bisect.bisect_left(self.days, d)
        return self.days[i - 1] if i > 0 else None

    # -------------------------------------------------------------- main loop
    def run(self, until: pd.Timestamp | None = None, auto_approve: bool = False) -> list:
        """Process every unprocessed trading day up to `until` (default: latest data)."""
        start = pd.Timestamp(self.cfg["start_date"])
        last = pd.Timestamp(self.meta["last_processed"]) if self.meta["last_processed"] else None
        until = until or self.days[-1]
        todo = [d for d in self.days if d >= start and (last is None or d > last) and d <= until]
        if last is None and todo:
            prev = [d for d in self.days if d < start]
            if prev:  # proposals for the very first session, from the evening before
                self.propose(prev[-1], auto_approve)
                bc = self.closes(prev[-1]).get(self.cfg["benchmark"])
                if bc:  # benchmark baseline = close before the start date
                    self.meta.setdefault("benchmark", {})[prev[-1].strftime("%Y-%m-%d")] = round(float(bc), 2)
        for d in todo:
            self.step(d, auto_approve)
            self.meta["last_processed"] = d.strftime("%Y-%m-%d")
        self.save()
        return todo

    def step(self, d: pd.Timestamp, auto_approve: bool) -> None:
        ds = d.strftime("%Y-%m-%d")
        self.corporate_actions(d)
        for a in self.accounts.values():
            a.contribute(ds)
        self.fill_proposals(d)
        self.rebalance_core(d)
        self.park_idle_cash(d)
        self.check_exits(d)
        cl = self.closes(d)
        for a in self.accounts.values():
            a.equity.append({"date": ds, "cash": round(a.cash, 2),
                             "holdings": round(a.market_value(cl), 2),
                             "liquid": round(a.liquid_value(cl), 2),
                             "core": round(a.core_value(cl), 2),
                             "equity": round(a.equity_value(cl), 2),
                             "contributed": a.contributed})
        bench = cl.get(self.cfg["benchmark"])
        if bench:
            self.meta.setdefault("benchmark", {})[ds] = round(float(bench), 2)
        self.propose(d, auto_approve)

    # ------------------------------------------------------ corporate actions
    def corporate_actions(self, d: pd.Timestamp) -> None:
        ev = self.actions[self.actions["date"] == d] if not self.actions.empty else self.actions
        ds = d.strftime("%Y-%m-%d")
        for _, e in ev.iterrows():
            for a in self.accounts.values():
                p = a.holding(e["symbol"])
                if not p or p.entry_date >= ds:
                    continue
                if e["kind"] == "split" and e["value"] > 0:
                    r = float(e["value"])
                    p.qty = int(round(p.qty * r))
                    p.entry_price, p.stop = round(p.entry_price / r, 2), round(p.stop / r, 2)
                    p.target, p.initial_stop = round(p.target / r, 2), round(p.initial_stop / r, 2)
                    a.log(ds, "split", f"{p.symbol} split/bonus ×{r:g}: now {p.qty} shares")
                elif e["kind"] == "dividend":
                    amt = round(p.qty * float(e["value"]), 2)
                    a.cash += amt
                    a.log(ds, "dividend", f"{p.symbol} dividend ₹{amt:,.2f}", amount=amt)

    # -------------------------------------------------------------- proposals
    def _approved(self, prop: dict, session_open: datetime) -> dict | None:
        ap = self.approvals.get(prop["id"])
        if not ap:
            return None
        at = datetime.fromisoformat(ap["at"])
        if at.tzinfo is None:
            at = at.replace(tzinfo=IST)
        return ap if at <= session_open else None

    def fill_proposals(self, d: pd.Timestamp) -> None:
        ds = d.strftime("%Y-%m-%d")
        session_open = datetime(d.year, d.month, d.day, 9, 15, tzinfo=IST)
        slip = self.cfg["fills"]["slippage_pct"] / 100
        cc = self.cfg["costs"]
        for prop in self.proposals:
            if prop["status"] != "pending":
                continue
            ap = self._approved(prop, session_open)
            if ap is None or ap["decision"] != "approve":
                prop["status"] = "rejected" if ap else "expired"
                prop["closed_on"] = ds
                continue
            prop["status"], prop["closed_on"], prop["approved_by"] = "approved", ds, ap.get("by", "you")
            b = self.bar(d, prop["symbol"])
            if b is None:
                prop["fills"] = {k: "no trade in this symbol today" for k in self.accounts}
                continue
            prop["fills"] = {}
            for name, a in self.accounts.items():
                if prop["type"] == "EXIT":
                    pos = a.holding(prop["symbol"])
                    if pos:
                        a.sell(ds, pos, b["open"] * (1 - slip), "exit approved", cc,
                               self.tdays_between(pos.entry_date, d))
                        prop["fills"][name] = f"sold @ {b['open'] * (1 - slip):.2f}"
                    continue
                plan = prop["sizing"].get(name, {})
                if not plan.get("qty"):
                    prop["fills"][name] = f"not taken: {plan.get('skip')}"
                    continue
                price = b["open"] * (1 + slip)
                if b["open"] <= prop["stop"]:
                    prop["fills"][name] = "skipped: opened at or below the stop"
                    continue
                if b["open"] >= prop["target"] or b["open"] > prop["ref_price"] * 1.03:
                    prop["fills"][name] = "skipped: gapped up more than 3%, so not chasing"
                    continue
                self.raise_cash(a, d, plan["qty"] * price * 1.003)
                qty = min(plan["qty"], int(a.cash // (price * 1.003)))
                if qty < 1:
                    prop["fills"][name] = "skipped: not enough cash"
                    continue
                a.buy(ds, prop["symbol"], qty, price, prop["stop"], prop["target"],
                      prop["setup"], prop["id"], cc)
                prop["fills"][name] = f"bought {qty} @ {price:.2f}"

    # ------------------------------------------------------------ market model
    def limits(self, d: pd.Timestamp) -> dict:
        """Trading limits from the market model, as of the close of `d`."""
        rk = self.cfg["risk"]
        if self.regime is not None:
            if d not in self.regime.index:
                return {"regime": "UNKNOWN", "risk_pct": 0, "max_pos": 0, "setups": (), "min_rs": None,
                        "core": 0.0, "entries_allowed": False, "text": "No market data for this day."}
            row = self.regime.loc[d]
            p = regime.params(row)
            p["entries_allowed"] = p["max_pos"] > 0
            p["text"] = regime.describe(row)
            return p
        ok = strategy.regime_ok(self.ind, d, self.cfg["benchmark"])
        return {"regime": "ON" if ok else "OFF", "risk_pct": rk["risk_per_trade_pct"],
                "max_pos": rk["max_open_positions"], "setups": ("breakout", "pullback"), "min_rs": None,
                "core": None, "entries_allowed": ok,
                "text": "NIFTY 500 above its 200-day average: entries allowed." if ok
                        else "NIFTY 500 below its 200-day average: no new entries."}

    def rebalance_core(self, d: pd.Timestamp) -> None:
        """At the open, move the core index holding toward its target.

        Target = core fraction x money not tied up in swing trades. The fraction is
        constant or set by the market regime (config core.mode). Trades only when
        off target by more than `band` of equity, to keep charges down."""
        cc = self.cfg.get("core", {})
        if not cc.get("enabled"):
            return
        prev = [x for x in [self.prev_day(d)] if x is not None]
        b = self.bar(d, cc["etf"])
        if not prev or b is None:
            return
        lim = self.limits(prev[-1])
        frac = lim["core"] if cc.get("mode") == "regime" and lim.get("core") is not None else cc["constant"]
        cl = self.closes(prev[-1])
        slip = self.cfg["fills"]["slippage_pct"] / 100
        ds = d.strftime("%Y-%m-%d")
        for a in self.accounts.values():
            eq = a.equity_value(cl)
            target = frac * (eq - a.market_value(cl))
            diff = target - a.core_value(cl)
            if abs(diff) < max(cc["band"] * eq, cc["min_trade"]):
                continue
            why = f"Target {frac:.0%} of uninvested money ({lim['regime']})."
            if diff > 0:
                self.raise_cash(a, d, diff, use_core=False)
                amt = min(diff, a.cash - self.cfg.get("cash_management", {}).get("cash_buffer", 0))
                if amt >= cc["min_trade"]:
                    a.core_buy(ds, b["open"] * (1 + slip), amt, self.cfg["costs"], why)
            else:
                price = b["open"] * (1 - slip)
                a.core_sell(ds, price, math.ceil(-diff / price), self.cfg["costs"], why)

    # --------------------------------------------------------- cash management
    def liquid_fill(self, d: pd.Timestamp, side: str) -> float | None:
        """Liquid-ETF fill price at the open. Thin ETFs often print stray opening
        trades, so buys fill at the higher of open and previous close and sells at the lower."""
        sym = self.cfg.get("cash_management", {}).get("liquid_etf", "")
        b = self.bar(d, sym) if sym else None
        if b is None:
            return None
        prev = [x for x in [self.prev_day(d)] if x is not None]
        pb = self.bar(prev[-1], sym) if prev else None
        pc = float(pb["close"]) if pb is not None else float(b["open"])
        return max(float(b["open"]), pc) if side == "BUY" else min(float(b["open"]), pc)

    def raise_cash(self, a: Account, d: pd.Timestamp, need: float, use_core: bool = True) -> None:
        """Sell liquid-ETF units at the open if cash is short for a buy, then core units."""
        short = need - a.cash
        price = self.liquid_fill(d, "SELL") if a.liquid_units else None
        if short > 0 and price:
            a.unpark(d.strftime("%Y-%m-%d"), price, short, self.cfg["costs"])
        short = need - a.cash
        b = self.bar(d, a.core_symbol) if use_core and a.core_units else None
        if short > 0 and b is not None:
            p = b["open"] * (1 - self.cfg["fills"]["slippage_pct"] / 100)
            a.core_sell(d.strftime("%Y-%m-%d"), p, math.ceil(short * 1.003 / p), self.cfg["costs"],
                        "To fund a swing trade.")

    def park_idle_cash(self, d: pd.Timestamp) -> None:
        """At the open, sweep idle cash into the liquid ETF.

        While the market filter allows entries, one full-size position's worth of
        cash stays free so a new trade doesn't immediately pay to unpark.
        """
        cm = self.cfg.get("cash_management")
        if not cm:
            return
        price = self.liquid_fill(d, "BUY")
        if price is None:
            return
        prev = [x for x in [self.prev_day(d)] if x is not None]
        entries_allowed = bool(prev) and self.limits(prev[-1])["entries_allowed"]
        cl = self.closes(prev[-1]) if prev else {}
        for a in self.accounts.values():
            keep = cm["cash_buffer"]
            if entries_allowed:
                keep += a.equity_value(cl) * self.cfg["risk"]["max_position_pct"] / 100
            spare = a.cash - keep
            if spare >= cm["min_park"]:
                a.park(d.strftime("%Y-%m-%d"), price, spare, self.cfg["costs"])

    def check_exits(self, d: pd.Timestamp) -> None:
        ds = d.strftime("%Y-%m-%d")
        slip = self.cfg["fills"]["slippage_pct"] / 100
        cc, rk = self.cfg["costs"], self.cfg["risk"]
        for a in self.accounts.values():
            for p in list(a.positions):
                b = self.bar(d, p.symbol)
                if b is None:
                    continue
                held = self.tdays_between(p.entry_date, d)
                o, h, l, c = b["open"], b["high"], b["low"], b["close"]
                if p.entry_date == ds:
                    o = p.entry_price  # filled at the open; only the rest of the day counts
                if o <= p.stop:
                    a.sell(ds, p, o * (1 - slip), "stop (gap)", cc, held)
                elif o >= p.target:
                    a.sell(ds, p, o * (1 - slip), "target (gap)", cc, held)
                elif l <= p.stop:  # if both hit intraday, assume the stop came first
                    a.sell(ds, p, p.stop * (1 - slip), "stop", cc, held)
                elif h >= p.target:
                    a.sell(ds, p, p.target * (1 - slip), "target", cc, held)
                elif held >= rk["max_holding_days"]:
                    a.sell(ds, p, c * (1 - slip), "time stop", cc, held)
                else:
                    r = p.entry_price - p.initial_stop
                    if not p.breakeven_moved and c >= p.entry_price + r:
                        p.stop, p.breakeven_moved = max(p.stop, p.entry_price), True
                        a.log(ds, "stop", f"{p.symbol}: up 1R, so the stop moves to breakeven ({p.entry_price:.2f})")

    def propose(self, d: pd.Timestamp, auto_approve: bool) -> list[dict]:
        ds = d.strftime("%Y-%m-%d")
        held = {p.symbol for a in self.accounts.values() for p in a.positions}
        lim = self.limits(d)
        quadrant = self.regime is not None
        cands = strategy.scan(self.ind, d, self.cfg["benchmark"], self.fund, exclude=self.special,
                              ignore_regime=quadrant, universe=self.universe, setups=lim["setups"],
                              min_rs=lim["min_rs"], sectors=self.sectors) if lim["entries_allowed"] else []
        self.meta["regime"] = {"date": ds, "regime": lim["regime"], "entries_allowed": lim["entries_allowed"],
                               "risk_pct": lim["risk_pct"], "max_pos": lim["max_pos"],
                               "setups": list(lim["setups"]), "text": lim["text"]}
        self.meta["watchlist"] = [] if lim["entries_allowed"] else strategy.scan(
            self.ind, d, self.cfg["benchmark"], self.fund, exclude=self.special, ignore_regime=True,
            universe=self.universe, sectors=self.sectors)
        rk = dict(self.cfg["risk"], risk_per_trade_pct=lim["risk_pct"], max_open_positions=lim["max_pos"])
        cl = self.closes(d)
        new = []
        reserved = {n: 0.0 for n in self.accounts}
        open_n = {n: len(a.positions) for n, a in self.accounts.items()}
        for c in cands:
            sizing = {}
            for name, a in self.accounts.items():
                free = a.cash + a.liquid_value(cl) + a.core_value(cl) - reserved[name]
                s = risk.size(c, a.equity_value(cl), free, open_n[name],
                              a.holding(c["symbol"]) is not None, rk, self.cfg["costs"])
                if s["qty"]:
                    reserved[name] += s["value"] * 1.003
                    open_n[name] += 1
                sizing[name] = s
            new.append({"id": f"{ds}-{c['symbol']}", "type": "ENTRY", "status": "pending",
                        "created": ds, **c, "sizing": sizing, "news": None})
        for sym, why in strategy.exit_signals(self.ind, d, sorted(held)).items():
            new.append({"id": f"{ds}-{sym}-EXIT", "type": "EXIT", "status": "pending",
                        "created": ds, "symbol": sym, "asof": ds, "technical": why,
                        "sizing": {}, "news": None})
        for prop in new:
            actionable = prop["type"] == "EXIT" or any(s["qty"] for s in prop["sizing"].values())
            if not actionable:
                prop["status"], prop["closed_on"] = "not_sized", ds
            elif auto_approve:
                self.approvals[prop["id"]] = {"decision": "approve", "by": "claude (backfill)",
                                              "at": f"{ds}T20:00:00+05:30"}
        self.proposals.extend(new)
        if auto_approve:
            _write(APPROVALS, self.approvals)
        return new
