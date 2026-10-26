"""Position sizing and per-account risk checks."""
from __future__ import annotations

import math

from . import costs as C


def size(cand: dict, equity: float, free_cash: float, open_positions: int,
         holding: bool, risk_cfg: dict, cost_cfg: dict) -> dict:
    """Size one candidate for one account. Returns qty=0 with a reason if skipped."""
    entry, stop, target = cand["ref_price"], cand["stop"], cand["target"]
    per_share_risk = entry - stop
    res = {"qty": 0, "value": 0.0, "risk": 0.0, "reward": 0.0, "est_costs": 0.0, "skip": None}

    if holding:
        res["skip"] = "already holding"
        return res
    if open_positions >= risk_cfg["max_open_positions"]:
        res["skip"] = f"already at the maximum of {risk_cfg['max_open_positions']} open positions"
        return res
    if (target - entry) / per_share_risk < risk_cfg["min_reward_risk"] - 1e-9:
        res["skip"] = "reward:risk below minimum"
        return res

    risk_budget = equity * risk_cfg["risk_per_trade_pct"] / 100
    qty = math.floor(risk_budget / per_share_risk)
    qty = min(qty, math.floor(equity * risk_cfg["max_position_pct"] / 100 / entry))
    # leave room for buy-side charges
    qty = min(qty, math.floor(free_cash / (entry * 1.003)))
    if qty < 1:
        cap = equity * risk_cfg["max_position_pct"] / 100
        res["skip"] = (f"can't buy even 1 share within the limits: share price ₹{entry:,.0f}, "
                       f"position cap ₹{cap:,.0f}, free cash ₹{free_cash:,.0f}")
        return res

    value = qty * entry
    cost = C.round_trip_cost(value, qty * target, cost_cfg)
    reward = qty * (target - entry)
    if cost > reward * risk_cfg["max_cost_to_reward_pct"] / 100:
        res["skip"] = (f"costs (₹{cost:,.0f}) would take {cost / reward:.0%} of the "
                       f"expected gain (₹{reward:,.0f})")
        return res
    res.update(qty=qty, value=round(value, 2), risk=round(qty * per_share_risk, 2),
               reward=round(reward, 2), est_costs=round(cost, 2))
    return res
