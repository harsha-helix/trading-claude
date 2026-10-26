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

from . import risk, strategy
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
                 cfg: dict | None = None):
        self.cfg = cfg or load_cfg()
        self.prices = prices
        self.actions = actions
        self.fund = fundamentals
        self.bars = prices.set_index(["date", "symbol"]).sort_index()
        bench = prices[prices["symbol"] == self.cfg["benchmark"]]
        self.days = sorted(bench["date"].unique()) if not bench.empty else sorted(prices["date"].unique())
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
        return sum(1 for d in self.days if a < d <= b)

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
        self.check_exits(d)
        cl = self.closes(d)
        for a in self.accounts.values():
            a.equity.append({"date": ds, "cash": round(a.cash, 2),
                             "holdings": round(a.market_value(cl), 2),
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
                qty = min(plan["qty"], int(a.cash // (price * 1.003)))
                if qty < 1:
                    prop["fills"][name] = "skipped: not enough cash"
                    continue
                a.buy(ds, prop["symbol"], qty, price, prop["stop"], prop["target"],
                      prop["setup"], prop["id"], cc)
                prop["fills"][name] = f"bought {qty} @ {price:.2f}"

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
        cands = strategy.scan(self.prices, d, self.cfg["benchmark"], self.fund)
        cl = self.closes(d)
        new = []
        reserved = {n: 0.0 for n in self.accounts}
        open_n = {n: len(a.positions) for n, a in self.accounts.items()}
        for c in cands:
            sizing = {}
            for name, a in self.accounts.items():
                s = risk.size(c, a.equity_value(cl), a.cash - reserved[name], open_n[name],
                              a.holding(c["symbol"]) is not None, self.cfg["risk"], self.cfg["costs"])
                if s["qty"]:
                    reserved[name] += s["value"] * 1.003
                    open_n[name] += 1
                sizing[name] = s
            new.append({"id": f"{ds}-{c['symbol']}", "type": "ENTRY", "status": "pending",
                        "created": ds, **c, "sizing": sizing, "news": None})
        for sym, why in strategy.exit_signals(self.prices, d, sorted(held)).items():
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
