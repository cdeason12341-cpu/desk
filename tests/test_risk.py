"""STEP 13.3 test_risk."""
import json
from agents.risk import Risk, evaluate
from agents.executor import Executor
from tests.helpers import make_ctx, pair, run

M = "Risk111111111111111111111111111111111pump"


def pos(entry=1.0, stop=6, opened=0, peak=None, trail=0):
    return {"entry_price": entry, "peak": peak or entry, "stop_pct": stop, "trail_active": trail, "opened_at": opened}


def test_rules():
    c, _ = make_ctx()
    s = c.s
    p = pos()
    r, u = evaluate(s, p, 1.25, 60); p.update(u)
    assert r is None and p["trail_active"] == 1 and p["peak"] == 1.25
    r, u = evaluate(s, p, 1.25 * 0.87, 120)
    assert r == "trail"
    r, _ = evaluate(s, pos(), 0.939, 60)
    assert r == "stop"
    r, _ = evaluate(s, pos(), 0.941, 60)
    assert r is None
    r, _ = evaluate(s, pos(opened=0), 1.04, 3 * 3600)
    assert r == "timeout"
    r, _ = evaluate(s, pos(opened=0), 1.04, 3 * 3600 - 1)
    assert r is None


def open_paper(c, clock, mint, name, price=1.0):
    ex = Executor(c)
    c.net.pairs[mint] = pair(mint, price=price, liq=100000, name=name, symbol=name)
    c.db.x("INSERT INTO coins(mint,name,symbol,status,migrated_at,first_seen,night) VALUES(?,?,?,?,?,?,?)",
           (mint, name, name, "passed", clock(), clock(), c.night))
    out = run(ex.run({"scan": {"mint": mint, "name": name, "symbol": name, "liquidityUsd": 100000},
                      "analyst": {"score": 80}, "ref_price": price}))
    assert out["status"] == "bought", out
    return c.db.one("SELECT * FROM positions WHERE mint=?", (mint,))


def test_stop_setting_never_loosens_open_and_applies_to_new():
    c, clock = make_ctx()
    risk = Risk(c)
    m_old, m_new = M, M[:-5] + "Npump"
    p_old = open_paper(c, clock, m_old, "OLDC")
    assert p_old["stop_pct"] == 6
    msg = c.s.set("stop", "-8%")
    assert msg == "stop -6% -> -8%"
    assert c.db.one("SELECT stop_pct FROM positions WHERE id=?", (p_old["id"],))["stop_pct"] == 6   # never moved down
    p_new = open_paper(c, clock, m_new, "NEWC")
    assert p_new["stop_pct"] == 8
    # drop the new one 8.1%, old one only 6% would fire at its own -6%; keep old at -5%
    c.net.pairs[m_new] = pair(m_new, price=0.919, liq=100000, name="NEWC", symbol="NEWC")
    c.net.pairs[m_old] = pair(m_old, price=0.95, liq=100000, name="OLDC", symbol="OLDC")
    clock.t += 15
    run(risk.poll_once())
    sell = [x for x in c.notifier.sent if x["kind"] == "sell"]
    assert len(sell) == 1 and sell[0]["text"].startswith("PAPER · NEWC hit the -8% stop · -$"), sell
    import links
    assert sell[0]["buttons"] == [[{"text": "Sell on FOMO", "url": links.coin_url(m_new, "solana")}],
                                  [{"text": "Sold", "callback_data": f"sold:{m_new}"}]]
    act = c.db.one("SELECT * FROM activity WHERE mint=? AND kind='stop'", (m_new,))
    assert act["title"] == "Stopped NEWC" and act["sub"] == "Risk · -8% stop" and act["right1"].startswith("-$")
    import main
    from publisher import Builder

    class D: pass
    d = D(); d.ctx = c; d.auditor = type("A", (), {"live": {}, "gpa_down_alerted": False})()
    d.analyst = type("A", (), {"live": {}})(); d.executor = type("E", (), {"pending": {}})()
    b = Builder(d)
    coin = b.coin(m_new)
    assert any(mk["label"] == "-8%" for mk in coin["marks"]), coin["marks"]
    assert b.agent("risk")["stopPill"] == "stop -8%"
    old_view = [p for p in b.agent("risk")["positions"] if p["mint"] == m_old][0]
    assert old_view["stopPct"] == 6
    # tightening applies to open positions
    c.s.set("stop", "-5%")
    assert c.db.one("SELECT stop_pct FROM positions WHERE mint=?", (m_old,))["stop_pct"] == 5


def test_paper_pnl_minus_fees():
    from agents.risk import pnl
    c, _ = make_ctx()
    p = {"entry_price": 1.0, "size_usd": 47.5}
    net, pct, f = pnl(c.s, p, 1.2)
    # entry fee 47.50*2%*0.9 = 0.855 ; exit 57.00 -> flat 0.95*0.9 = 0.855 ; gross 9.50
    assert abs(f - 1.71) < 1e-9 and net == round(9.5 - 1.71, 2)


def test_exit_texts_exact():
    from agents.risk import exit_text
    c, _ = make_ctx()
    p = {"stop_pct": 6}
    assert exit_text("", "trail", "USPR", p, 9.5, 20.0, c.s) == "Sold USPR · +$9.50 (+20.0%)"
    assert exit_text("PAPER · ", "stop", "USPR", p, -3.71, -7.8, c.s) == "PAPER · USPR hit the -6% stop · -$3.71"
    assert exit_text("", "timeout", "USPR", p, 1.2, 4.0, c.s) == "Sell USPR, dead money · 4.0% in 3h"
