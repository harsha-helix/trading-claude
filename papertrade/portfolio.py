"""Account ledger: cash, open positions, closed trades, equity history.

Each account is one JSON file under state/ so every change shows up as a
readable git diff.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import costs as C

STATE = Path(__file__).resolve().parent.parent / "state"
TICK = 0.05


def to_tick(price: float) -> float:
    """NSE prices move in ₹0.05 steps."""
    return round(round(price / TICK) * TICK, 2)


@dataclass
class Position:
    symbol: str
    qty: int
    entry_date: str
    entry_price: float
    stop: float
    target: float
    initial_stop: float
    setup: str
    proposal_id: str
    entry_charges: float
    breakeven_moved: bool = False


@dataclass
class Account:
    name: str
    label: str
    cash: float
    monthly_contribution: float
    contributed: float
    positions: list[Position] = field(default_factory=list)
    closed: list[dict] = field(default_factory=list)
    events: list[dict] = field(default_factory=list)
    equity: list[dict] = field(default_factory=list)
    last_contribution_month: str = ""
    # idle cash parked in a liquid ETF (not a trading position)
    liquid_symbol: str = ""
    liquid_units: int = 0
    liquid_cost: float = 0.0          # cost basis incl. buy charges, for tax
    liquid_last_price: float = 0.0
    liquid_realised: list[dict] = field(default_factory=list)

    # ------------------------------------------------------------ persistence
    @classmethod
    def load(cls, name: str) -> "Account":
        d = json.loads((STATE / f"{name}.json").read_text())
        d["positions"] = [Position(**p) for p in d["positions"]]
        return cls(**d)

    @classmethod
    def new(cls, name: str, cfg: dict, start: str) -> "Account":
        a = cls(name=name, label=cfg["label"], cash=float(cfg["initial_cash"]),
                monthly_contribution=float(cfg["monthly_contribution"]),
                contributed=float(cfg["initial_cash"]), last_contribution_month=start[:7])
        a.log(start, "deposit", f"Opening balance ₹{cfg['initial_cash']:,.0f}", amount=cfg["initial_cash"])
        return a

    def save(self) -> None:
        STATE.mkdir(exist_ok=True)
        d = asdict(self)
        (STATE / f"{self.name}.json").write_text(json.dumps(d, indent=1, ensure_ascii=False))

    # ---------------------------------------------------------------- helpers
    def log(self, day: str, kind: str, text: str, **extra) -> None:
        self.events.append({"date": day, "kind": kind, "text": text, **extra})

    def holding(self, symbol: str) -> Position | None:
        return next((p for p in self.positions if p.symbol == symbol), None)

    def market_value(self, closes: dict[str, float]) -> float:
        return sum(p.qty * closes.get(p.symbol, p.entry_price) for p in self.positions)

    def liquid_value(self, closes: dict[str, float]) -> float:
        if not self.liquid_units:
            return 0.0
        self.liquid_last_price = closes.get(self.liquid_symbol, self.liquid_last_price)
        return self.liquid_units * self.liquid_last_price

    def equity_value(self, closes: dict[str, float]) -> float:
        return self.cash + self.market_value(closes) + self.liquid_value(closes)

    # ---------------------------------------------------------------- actions
    def contribute(self, day: str) -> None:
        month = day[:7]
        if self.monthly_contribution and month > self.last_contribution_month:
            self.cash += self.monthly_contribution
            self.contributed += self.monthly_contribution
            self.last_contribution_month = month
            self.log(day, "deposit", f"Monthly top-up ₹{self.monthly_contribution:,.0f}",
                     amount=self.monthly_contribution)

    def buy(self, day: str, symbol: str, qty: int, price: float, stop: float, target: float,
            setup: str, proposal_id: str, cc: dict) -> Position:
        price = to_tick(price)
        value = qty * price
        ch = C.charges("BUY", value, cc)
        self.cash -= value + ch.total
        pos = Position(symbol, qty, day, price, stop, target, stop, setup,
                       proposal_id, ch.total)
        self.positions.append(pos)
        self.log(day, "buy", f"BUY {qty} {symbol} @ ₹{price:,.2f}", symbol=symbol, qty=qty,
                 price=round(price, 2), charges=ch.as_dict(), proposal_id=proposal_id)
        return pos

    def sell(self, day: str, pos: Position, price: float, reason: str, cc: dict,
             trading_days_held: int) -> dict:
        price = to_tick(price)
        value = pos.qty * price
        ch = C.charges("SELL", value, cc)
        self.cash += value - ch.total
        gross = (price - pos.entry_price) * pos.qty
        net = gross - pos.entry_charges - ch.total
        risk = (pos.entry_price - pos.initial_stop) * pos.qty
        from datetime import date
        cal_days = (date.fromisoformat(day) - date.fromisoformat(pos.entry_date)).days
        trade = {
            "symbol": pos.symbol, "setup": pos.setup, "qty": pos.qty,
            "entry_date": pos.entry_date, "entry_price": pos.entry_price,
            "exit_date": day, "exit_price": round(price, 2), "reason": reason,
            "gross_pnl": round(gross, 2), "charges": round(pos.entry_charges + ch.total, 2),
            "net_pnl": round(net, 2), "r_multiple": round(net / risk, 2) if risk > 0 else None,
            "holding_days": cal_days, "trading_days": trading_days_held,
            "proposal_id": pos.proposal_id,
        }
        self.closed.append(trade)
        self.positions.remove(pos)
        self.log(day, "sell", f"SELL {pos.qty} {pos.symbol} @ ₹{price:,.2f} ({reason}), "
                 f"net P&L ₹{net:,.0f}", symbol=pos.symbol, qty=pos.qty, price=round(price, 2),
                 charges=ch.as_dict(), reason=reason, net_pnl=round(net, 2))
        return trade

    # ------------------------------------------------------------- liquid ETF
    def park(self, day: str, price: float, amount: float, cc: dict) -> None:
        """Buy liquid-ETF units with up to `amount` of cash (charges included)."""
        price = to_tick(price) if price >= 1 else price
        units = int(amount / (price * 1.002))
        if units < 1:
            return
        value = units * price
        ch = C.charges("BUY", value, cc, stt=False)
        self.cash -= value + ch.total
        self.liquid_units += units
        self.liquid_cost += value + ch.total
        self.liquid_last_price = price
        self.log(day, "park", f"Parked ₹{value:,.0f} in {self.liquid_symbol} ({units} units @ ₹{price:,.2f})",
                 units=units, price=price, charges=ch.as_dict())

    def unpark(self, day: str, price: float, need: float, cc: dict) -> None:
        """Sell enough liquid-ETF units to raise `need` in cash, after charges."""
        if not self.liquid_units or need <= 0:
            return
        units = min(self.liquid_units, int(need / (price * 0.997)) + 1)
        value = units * price
        ch = C.charges("SELL", value, cc, stt=False)
        basis = self.liquid_cost * units / self.liquid_units
        self.cash += value - ch.total
        self.liquid_units -= units
        self.liquid_cost -= basis
        gain = value - ch.total - basis
        self.liquid_realised.append({"date": day, "units": units, "gain": round(gain, 2)})
        self.log(day, "unpark", f"Withdrew ₹{value:,.0f} from {self.liquid_symbol} ({units} units @ ₹{price:,.2f})",
                 units=units, price=price, charges=ch.as_dict(), gain=round(gain, 2))
