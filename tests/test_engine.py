import numpy as np
import pandas as pd
import pytest

from papertrade import costs, engine, portfolio, risk

CFG = engine.load_cfg()
# the swing-strategy tests run in swing mode with plain cash accounts
SWING = dict(CFG, strategy={"mode": "swing"},
             accounts={k: {kk: vv for kk, vv in v.items() if kk not in ("core", "vehicle", "direct_from")}
                       for k, v in CFG["accounts"].items()})


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
    cfg = dict(SWING, start_date=days[245].strftime("%Y-%m-%d"))
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
    cfg = dict(SWING, start_date=days[245].strftime("%Y-%m-%d"))
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


def test_idle_cash_parked_in_liquid_etf_when_market_filter_off():
    days = pd.bdate_range("2025-01-01", periods=260)
    rows = []
    for i, d in enumerate(days):
        # benchmark in a steady downtrend: market filter blocks entries
        bc = 1000 - i
        rows.append({"date": d, "symbol": CFG["benchmark"], "open": bc, "high": bc,
                     "low": bc, "close": bc, "volume": 0})
        lp = 100 * (1 + 0.06 / 252) ** i
        rows.append({"date": d, "symbol": "GROWWLIQID", "open": lp, "high": lp,
                     "low": lp, "close": lp, "volume": 1e6})
    prices = pd.DataFrame(rows)
    cfg = dict(SWING, start_date=days[220].strftime("%Y-%m-%d"))
    e = engine.Engine(prices, pd.DataFrame(columns=["date", "symbol", "kind", "value"]), {}, cfg)
    e.run(auto_approve=True)
    a = e.accounts["A_lumpsum"]
    assert a.liquid_units > 0 and a.cash < CFG["cash_management"]["min_park"]
    assert a.equity[-1]["equity"] > a.equity[0]["equity"]  # earning the liquid yield
    b = e.accounts["B_monthly"]
    assert b.liquid_units > 0  # ₹10k is above the ₹5k minimum
    # raising cash sells just enough units
    before = a.liquid_units
    e.raise_cash(a, days[-1], a.cash + 20_000)
    assert a.cash >= 20_000 and 0 < before - a.liquid_units < before


def _trend_prices(slope: float, n: int = 400, extra=("NIFTYBEES",)):
    days = pd.bdate_range("2024-01-01", periods=n)
    rows = []
    for i, d in enumerate(days):
        bc = 1000 + slope * i
        rows.append({"date": d, "symbol": CFG["benchmark"], "open": bc, "high": bc, "low": bc, "close": bc, "volume": 0})
        for k in range(5):  # a few stocks moving with the market, for breadth
            c = 100 + slope * 0.1 * i + k
            rows.append({"date": d, "symbol": f"S{k}", "open": c, "high": c, "low": c, "close": c, "volume": 1e5})
        for sym in extra:
            c = 100 + slope * 0.1 * i
            rows.append({"date": d, "symbol": sym, "open": c, "high": c, "low": c, "close": c, "volume": 1e6})
        lp = 100 * (1 + 0.06 / 252) ** i
        rows.append({"date": d, "symbol": "GROWWLIQID", "open": lp, "high": lp, "low": lp, "close": lp, "volume": 1e6})
    return pd.DataFrame(rows), days


def test_regime_quadrants():
    from papertrade import regime, strategy
    for slope, want in ((1.0, "BULL"), (-1.0, "BEAR")):
        prices, _ = _trend_prices(slope)
        ind = strategy.compute_all(prices)
        r = regime.compute(prices, ind, CFG["benchmark"], {f"S{k}" for k in range(5)}, {}, use_global=False)
        assert r["regime"].iloc[-1] == want


def test_core_holding_constant_fraction(monkeypatch):
    from papertrade import strategy
    monkeypatch.setitem(strategy.PARAMS, "market_model", "quadrant")
    prices, days = _trend_prices(1.0)
    cfg = dict(SWING, start_date=days[300].strftime("%Y-%m-%d"), global_symbols={},
               core=dict(CFG["core"], enabled=True, mode="constant", constant=0.6))
    e = engine.Engine(prices, pd.DataFrame(columns=["date", "symbol", "kind", "value"]), {}, cfg)
    e.run(auto_approve=True)
    a = e.accounts["A_lumpsum"]
    last = a.equity[-1]
    assert 0.5 < last["core"] / last["equity"] < 0.75     # within the rebalance band of 60%
    assert last["liquid"] > 0                              # the rest earns the liquid yield


def test_core_fifo_tax_split():
    a = portfolio.Account.new("A_lumpsum", CFG["accounts"]["A_lumpsum"], "2025-01-01")
    a.core_symbol = "NIFTYBEES"
    a.core_buy("2025-01-01", 100.0, 10_000, CFG["costs"], "t")
    a.core_buy("2025-12-01", 120.0, 10_000, CFG["costs"], "t")
    first = a.core_lots[0]["units"]
    a.core_sell("2026-02-01", 130.0, first + 5, CFG["costs"], "t")
    days = [r["holding_days"] for r in a.core_realised]
    assert days[0] > 365 and days[1] < 365                 # oldest lot first: long-term, then short-term
    assert a.core_units == a.core_lots[0]["units"]


def test_momentum_monthly_rebalance(monkeypatch):
    from papertrade import strategy
    monkeypatch.setitem(strategy.MOMENTUM, "top_n", 3)
    monkeypatch.setitem(strategy.MOMENTUM, "buffer", 1)   # 10 stocks: a 2x buffer would keep everything
    monkeypatch.setitem(strategy.MOMENTUM, "low_vol_weight", 0.0)  # test the rebalance mechanics on pure momentum
    days = pd.bdate_range("2024-01-01", periods=330)
    rows = []
    for i, d in enumerate(days):
        bc = 1000 + i
        rows.append({"date": d, "symbol": CFG["benchmark"], "open": bc, "high": bc, "low": bc, "close": bc, "volume": 0})
        lp = 100 * (1 + 0.06 / 252) ** i
        rows.append({"date": d, "symbol": "GROWWLIQID", "open": lp, "high": lp, "low": lp, "close": lp, "volume": 1e6})
        for k in range(10):
            # S0..S9: steady drift k%/yr with noise; S9 strongest until day 300, then collapses
            drift = (k - 3) * 0.0006
            c = 100 * np.exp(drift * i + 0.01 * np.sin(i / 7 + k))
            if k == 9 and i > 300:          # S9 slides 2% a day after day 300
                c *= np.exp(-0.02 * (i - 300))
            rows.append({"date": d, "symbol": f"S{k}", "open": c, "high": c, "low": c, "close": c, "volume": 1e6})
    prices = pd.DataFrame(rows)
    cfg = dict(CFG, start_date=days[280].strftime("%Y-%m-%d"), global_symbols={},
               strategy={"mode": "momentum"}, core=dict(CFG["core"], enabled=False),
               accounts={"A_lumpsum": dict(CFG["accounts"]["A_lumpsum"], vehicle="direct")})
    e = engine.Engine(prices, pd.DataFrame(columns=["date", "symbol", "kind", "value"]), {}, cfg,
                      universe={f"S{k}" for k in range(10)})
    e.run(auto_approve=True)
    a = e.accounts["A_lumpsum"]
    buys = [x for x in e.proposals if x["type"] == "ENTRY"]
    assert {x["symbol"] for x in buys[:3]} == {"S9", "S8", "S7"}       # first month: top 3
    assert all(p.setup == "momentum" for p in a.positions) and len(a.positions) <= 3
    assert any(x["type"] == "EXIT" and x["symbol"] == "S9" for x in e.proposals)  # collapse -> sold
    assert "S9" not in {p.symbol for p in a.positions}
    entries = [x["date"] for x in a.events if x["kind"] == "buy"]
    start = cfg["start_date"]
    assert all(d == start or pd.Timestamp(d).day <= 3 for d in entries)  # start, then only at month turns
    assert len(entries) > 3                                                # it did rebalance later
