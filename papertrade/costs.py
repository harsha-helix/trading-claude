"""Groww equity-delivery charges and Indian capital-gains tax.

All rates come from config.json["costs"] / ["tax"] so they can be updated
when the broker or the government changes them.
"""
from dataclasses import dataclass


@dataclass
class Charges:
    brokerage: float
    stt: float
    exchange: float
    sebi: float
    stamp: float
    gst: float
    dp: float

    @property
    def total(self) -> float:
        return round(self.brokerage + self.stt + self.exchange + self.sebi
                     + self.stamp + self.gst + self.dp, 2)

    def as_dict(self) -> dict:
        return {**self.__dict__, "total": self.total}


def brokerage(value: float, c: dict) -> float:
    b = min(c["brokerage_max"], value * c["brokerage_pct"] / 100)
    return max(c["brokerage_min"], b)


def charges(side: str, value: float, c: dict, stt: bool = True) -> Charges:
    """Charges for one delivery order. side is 'BUY' or 'SELL'.
    stt=False for debt/liquid ETFs, which don't attract STT."""
    brk = brokerage(value, c)
    stt = value * c["stt_pct"] / 100 if stt else 0.0
    exch = value * c["exchange_txn_pct"] / 100
    sebi = value * c["sebi_per_crore"] / 1e7
    stamp = value * c["stamp_duty_buy_pct"] / 100 if side == "BUY" else 0.0
    dp = 0.0
    if side == "SELL" and value >= c["dp_waiver_below"]:
        dp = c["dp_charge_per_sell"]
    gst = (brk + exch + sebi + dp) * c["gst_pct"] / 100
    r = lambda x: round(x, 2)
    return Charges(r(brk), r(stt), r(exch), r(sebi), r(stamp), r(gst), r(dp))


def round_trip_cost(entry_value: float, exit_value: float, c: dict) -> float:
    return charges("BUY", entry_value, c).total + charges("SELL", exit_value, c).total


def estimate_tax(realised: list[dict], t: dict) -> dict:
    """Estimated capital-gains tax for a list of closed trades.

    Each item needs 'net_pnl' and 'holding_days'. Losses offset gains of the
    same type first (simplified: STCL also offsets LTCG, as the law allows).
    """
    st = sum(x["net_pnl"] for x in realised if x["holding_days"] < t["ltcg_min_days"])
    lt = sum(x["net_pnl"] for x in realised if x["holding_days"] >= t["ltcg_min_days"])
    if st < 0:
        lt, st = lt + st, 0.0
    lt_taxable = max(0.0, lt - t["ltcg_exemption"])
    stcg_tax = max(0.0, st) * t["stcg_pct"] / 100
    ltcg_tax = lt_taxable * t["ltcg_pct"] / 100
    return {"stcg": round(st, 2), "ltcg": round(lt, 2),
            "stcg_tax": round(stcg_tax, 2), "ltcg_tax": round(ltcg_tax, 2),
            "total_tax": round(stcg_tax + ltcg_tax, 2)}
