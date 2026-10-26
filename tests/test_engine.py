import numpy as np
import pandas as pd
import pytest

from papertrade import costs, engine, portfolio, risk

CFG = engine.load_cfg()


@pytest.fixture(autouse=True)
def tmp_state(tmp_path, monkeypatch):
    monkeypatch.setattr(portfolio, "STATE", tmp_path)
    monkeypatch.setattr(engine, "STATE", tmp_path)
    monkeypatch.setattr(engine, "PROPOSALS", tmp_path / "proposals.json")
    monkeypatch.setattr(engine, "APPROVALS", tmp_path / "approvals.json")
    monkeypatch.setattr(engine, "META", tmp_path / "meta.json")


def test_buy_charges_groww():
    ch = costs.charges("BUY", 10_000, CFG["costs"])
    assert ch.brokerage == 10.0          # 0.1% of 10k, under the ₹20 cap
    assert ch.stt == 10.0
    assert ch.stamp == 1.5
    assert ch.dp == 0.0
    assert ch.total == pytest.approx(23.66, abs=0.02)


def test_sell_charges_include_dp():
    ch = costs.charges("SELL", 100_000, CFG["costs"])
    assert ch.brokerage == 20.0          # capped
    assert ch.dp == 20.0
    assert ch.stamp == 0.0
    assert ch.gst == pytest.approx((20 + 2.97 + 0.1 + 20) * 0.18, abs=0.01)


def test_min_brokerage():
    assert costs.charges("BUY", 1_000, CFG["costs"]).brokerage == 5.0


def test_tax_short_term_loss_offsets():
    t = costs.estimate_tax([{"net_pnl": 10_000, "holding_days": 30},
                            {"net_pnl": -4_000, "holding_days": 10}], CFG["tax"])
    assert t["stcg_tax"] == pytest.approx(6_000 * 0.20)


def test_sizing_respects_risk_and_cap():
    cand = {"ref_price": 1000.0, "stop": 950.0, "target": 1125.0}
    s = risk.size(cand, 100_000, 100_000, 0, False, CFG["risk"], CFG["costs"])
    # 2% risk = 2000 / 50 = 40 shares, but 25% cap = 25 shares
    assert s["qty"] == 25


def test_sizing_skips_when_costs_eat_reward():
    cand = {"ref_price": 1000.0, "stop": 990.0, "target": 1025.0}
    s = risk.size(cand, 10_000, 10_000, 0, False, CFG["risk"], CFG["costs"])
    assert s["qty"] == 0 and "costs" in s["skip"]


def _synthetic(breakout_day: int, n: int = 300):
    days = pd.bdate_range("2025-08-01", periods=n)
    rng = np.random.default_rng(0)
    # steady uptrend with noise, then a high-volume breakout and a run-up
    base = 500 + np.arange(n) * 1.0 + rng.normal(0, 2, n)
    base[breakout_day - 30:breakout_day] = base[breakout_day - 30]  # flat base
    base[breakout_day] = base[breakout_day - 1] + 25
    base[breakout_day + 1:] = base[breakout_day] + np.arange(1, n - breakout_day) * 6
    vol = np.full(n, 1_000_000.0)
    vol[breakout_day] = 3_000_000
    rows = []
    for i, d in enumerate(days):
        c = base[i]
        rows.append({"date": d, "symbol": "TEST", "open": c - 1, "high": c + 3,
                     "low": c - 3, "close": c, "volume": vol[i]})
        rows.append({"date": d, "symbol": CFG["benchmark"], "open": 100, "high": 101,
                     "low": 99, "close": 100 + i * 0.01, "volume": 0})
    return pd.DataFrame(rows), days


def test_breakout_entry_then_target():
    prices, days = _synthetic(breakout_day=250)
    cfg = dict(CFG, start_date=days[245].strftime("%Y-%m-%d"))
    acts = pd.DataFrame(columns=["date", "symbol", "kind", "value"])
    e = engine.Engine(prices, acts, {}, cfg)
    e.run(auto_approve=True)
    props = [p for p in e.proposals if p["symbol"] == "TEST" and p["type"] == "ENTRY"]
    assert props and props[0]["setup"] == "breakout"
    a = e.accounts["A_lumpsum"]
    assert a.closed, "position should have been closed at target"
    t = a.closed[0]
    assert t["reason"].startswith("target") and t["net_pnl"] > 0
    assert t["entry_date"] == days[251].strftime("%Y-%m-%d")  # filled next open, no lookahead
    # cash conservation: deposits + net P&L of closed trades == final cash when flat
    if not a.positions:
        assert a.cash == pytest.approx(a.contributed + sum(x["net_pnl"] for x in a.closed), abs=0.05)


def test_unapproved_proposal_expires():
    prices, days = _synthetic(breakout_day=250)
    cfg = dict(CFG, start_date=days[245].strftime("%Y-%m-%d"))
    e = engine.Engine(prices, pd.DataFrame(columns=["date", "symbol", "kind", "value"]), {}, cfg)
    e.run(auto_approve=False)
    assert not e.accounts["A_lumpsum"].closed and not e.accounts["A_lumpsum"].positions
    assert any(p["status"] == "expired" for p in e.proposals)


def test_monthly_contribution():
    a = portfolio.Account.new("B_monthly", CFG["accounts"]["B_monthly"], "2026-10-01")
    a.contribute("2026-10-15")
    assert a.contributed == 10_000
    a.contribute("2026-11-02")
    a.contribute("2026-11-03")
    assert a.contributed == 20_000 and a.cash == 20_000
