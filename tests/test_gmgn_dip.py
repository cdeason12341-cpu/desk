"""DECISION 64: Strategy A waits for a small dip before buying (owner 2026-11-16 8:40–8:42 PM CT).
Buy instantly at >=5% below the high since approval; if already down more than 10%, still buy and log it.
If no dip in 30s, buy at market (do not skip). No bounce. Safety problems still skip.
FOMO paper fills wait for the same trigger. Strategy B is unchanged."""
import config, links, webpush
from agents.executor import Executor
from dip import DIP_GAP_LOG_PCT, dip_ready, drop_pct
from publisher import Builder
from tests.helpers import make_ctx, pair, run
from tests.test_gmgn import M, trader
from tests.test_gmgn_grace import GraceClient, _held, _live, _nosleep
from tests.test_publisher_gmgn import DeskStub
import asyncio


def _dip(monkeypatch, mode="LIVE", **kw):
    settings = {"gmgn.dip_enabled": True, "gmgn.dip_pct": 5, "gmgn.dip_timeout_s": 30,
                "scanner.min_liquidity_usd": 10000, **kw}
    monkeypatch.setattr(asyncio, "sleep", _nosleep)
    if mode == "LIVE":
        fc = GraceClient()
        c, clock, t = _live(fc, **settings)
        _held(t)
        c.net.pairs[M] = pair(M, price=1.0, liq=50000, name="Dip", symbol="DIP")
        return c, clock, t, fc
    c, clock, t = trader(**settings)
    c.net.pairs[M] = pair(M, price=1.0, liq=50000, name="Dip", symbol="DIP")
    return c, clock, t, None


def _swap(fc):
    return [b for k, b in fc.calls if k == "swap"] if fc else []


def test_settings_defaults_not_protected_and_hot_load():
    assert config.BY_KEY["gmgn.dip_enabled"].default is True
    c, _ = make_ctx(**{"gmgn.dip_enabled": True})
    assert c.s["gmgn.dip_enabled"] is True
    assert c.s["gmgn.dip_pct"] == 5.0 and c.s["gmgn.dip_timeout_s"] == 30
    assert "gmgn.dip_bounce_pct" not in config.BY_KEY
    assert "gmgn.dip_timeout_min" not in config.BY_KEY
    for k in ("gmgn.dip_enabled", "gmgn.dip_pct", "gmgn.dip_timeout_s"):
        assert k not in config.PROTECTED
    assert c.s.set("dip pct", "7", source="test") == "dip pct 5% -> 7%"
    assert c.s.set("dip timeout", "15", source="test") == "dip timeout 30s -> 15s"
    import pytest
    with pytest.raises(config.SettingError):
        c.s.set("gmgn.dip_timeout_s", 0, source="test")
    with pytest.raises(config.SettingError):
        c.s.set("gmgn.dip_pct", 51, source="test")
    c.db.x("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
           ("gmgn.dip_enabled", "false"))
    c.db.x("INSERT INTO change_log(ts,key,old,new,source) VALUES(?,?,?,?,?)",
           (c.clock(), "gmgn.dip_enabled", "true", "false", "chat"))
    assert "gmgn.dip_enabled" in c.s.sync() and c.s["gmgn.dip_enabled"] is False


def test_dip_ready_unit_no_bounce_gap_and_timeout():
    s = {"gmgn.dip_pct": 5.0}
    assert dip_ready(s, 1.0, 0.951) is None                         # -4.9%
    assert dip_ready(s, 1.0, 0.95) == "dip"                         # -5% exactly, no bounce needed
    assert dip_ready(s, 1.0, 0.94) == "dip"                         # still falling, still a buy
    assert dip_ready(s, 1.0, 0.899) == "dip_gap"                    # already >10% down: still buy
    assert DIP_GAP_LOG_PCT == 10.0 and drop_pct(1.0, 0.88) == 12.0
    # timeout buys at market even with only a tiny dip; a real dip on the deadline wins
    assert dip_ready(s, 1.0, 0.99, now=30, expires=30) == "timeout"
    assert dip_ready(s, 1.0, 0.95, now=30, expires=30) == "dip"
    assert dip_ready(s, 1.0, None, now=29, expires=30) is None
    assert dip_ready(s, 1.0, None, now=30, expires=30) == "timeout"


def test_on_pass_parks_then_dip_buys(monkeypatch):
    c, clock, t, fc = _dip(monkeypatch)
    r = run(t.on_pass(M, "Dip", "DIP", 1.0, 50000, 80))
    assert r["status"] == "dip" and not _swap(fc)
    assert c.db.one("SELECT status FROM gmgn_dips")["status"] == "watching"
    assert c.db.one("SELECT id FROM gmgn_orders") is None
    notes = [m["text"] for m in c.notifier.sent if m["kind"] == "stage_dip"]
    assert any("⏳ Dip watch · Dip" in n and "Need -5% from $1" in n and "buy anyway in 30s" in n for n in notes)
    assert all("r=savipww" in (m.get("buttons") or [[{}]])[0][0].get("url", "") or "r=savipww" in m["text"]
               for m in c.notifier.sent if m["kind"] == "stage_dip")
    assert links.coin_url(M) in notes[0] and "r=savipww" in links.coin_url(M)
    # second pass doesn't open a second watch
    assert run(t.on_pass(M, "Dip", "DIP", 1.01, 50000, 80))["status"] == "dip"
    assert c.db.one("SELECT COUNT(*) n FROM gmgn_dips")["n"] == 1
    # price ticks up, then dips 5% from the new high
    run(c.dip.tick(prices={M: 1.10}, liqs={M: 50000}))
    assert c.db.one("SELECT high FROM gmgn_dips")["high"] == 1.10
    assert c.db.one("SELECT id FROM gmgn_orders") is None
    run(c.dip.tick(prices={M: 1.10 * 0.95}, liqs={M: 50000}))
    assert len(_swap(fc)) == 1
    o = c.db.one("SELECT * FROM gmgn_orders")
    assert o and o["status"] == "open"          # LIVE fill price comes from the chain when a tx is present
    d = c.db.one("SELECT * FROM gmgn_dips")
    assert d["status"] == "bought" and d["reason"] == "dip" and abs(d["last_price"] - 1.10 * 0.95) < 1e-12
    assert any("🎯 Dip buy · Dip" in m["text"] and "Down 5.0% from the high" in m["text"]
               for m in c.notifier.sent)


def test_timeout_buys_at_market(monkeypatch):
    c, clock, t, fc = _dip(monkeypatch)
    run(t.on_pass(M, "Dip", "DIP", 1.0, 50000, 80))
    run(c.dip.tick(prices={M: 0.99}, liqs={M: 50000}))              # only -1%, keep watching
    assert not _swap(fc)
    clock.t += 30
    run(c.dip.tick(prices={M: 0.99}, liqs={M: 50000}))
    assert len(_swap(fc)) == 1
    o = c.db.one("SELECT * FROM gmgn_orders")
    assert o and o["status"] == "open"
    d = c.db.one("SELECT * FROM gmgn_dips")
    assert d["reason"] == "timeout" and d["last_price"] == 0.99
    assert any("🎯 Dip timeout · Dip" in m["text"] and "buying at market" in m["text"] for m in c.notifier.sent)


def test_gap_over_10_still_buys(monkeypatch):
    c, clock, t, fc = _dip(monkeypatch)
    run(t.on_pass(M, "Dip", "DIP", 1.0, 50000, 80))
    run(c.dip.tick(prices={M: 0.88}, liqs={M: 50000}))
    assert len(_swap(fc)) == 1
    d = c.db.one("SELECT * FROM gmgn_dips")
    assert d["reason"] == "dip_gap" and d["last_price"] == 0.88
    assert c.db.one("SELECT status FROM gmgn_orders")["status"] == "open"
    assert any("Gapped 12.0% from the high · still buying" in m["text"] for m in c.notifier.sent)


def test_safety_skip_low_liquidity(monkeypatch):
    c, clock, t, fc = _dip(monkeypatch)
    run(t.on_pass(M, "Dip", "DIP", 1.0, 50000, 80))
    run(c.dip.tick(prices={M: 0.95}, liqs={M: 1000}))               # under $10k floor, even though it's a dip
    assert not _swap(fc)
    d = c.db.one("SELECT * FROM gmgn_dips")
    assert d["status"] == "skipped" and "liquidity" in d["reason"]
    assert c.db.one("SELECT id FROM gmgn_orders") is None
    assert any("Dip skipped · Dip" in m["text"] and "liquidity" in m["text"] for m in c.notifier.sent)


def test_disabled_buys_immediately(monkeypatch):
    c, clock, t, fc = _dip(monkeypatch, **{"gmgn.dip_enabled": False})
    r = run(t.on_pass(M, "Dip", "DIP", 1.0, 50000, 80))
    assert r["status"] == "open" and len(_swap(fc)) == 1
    assert c.db.one("SELECT id FROM gmgn_dips") is None


def test_approval_then_dip(monkeypatch):
    c, clock, t, fc = _dip(monkeypatch, **{"gmgn.approve_above_mc_usd": 300000})
    r = run(t.on_pass(M, "Dip", "DIP", 1.0, 50000, 80, mc=450000))
    assert r["status"] == "approval" and not _swap(fc)
    assert c.db.one("SELECT id FROM gmgn_dips") is None
    c.net.pairs[M]["priceUsd"] = "1.0"
    c.net.pairs[M]["marketCap"] = 450000
    out = run(t.approve(1))
    assert "Watching for a 5% dip" in out and "30s" in out
    assert not _swap(fc) and c.db.one("SELECT status FROM gmgn_dips")["status"] == "watching"
    assert c.db.one("SELECT status FROM gmgn_approvals")["status"] == "done"
    run(c.dip.tick(prices={M: 0.95}, liqs={M: 50000}))
    assert len(_swap(fc)) == 1


def test_fomo_paper_waits_and_fills_at_dip_price(monkeypatch):
    monkeypatch.setattr(asyncio, "sleep", _nosleep)
    c, clock, t = trader(**{"gmgn.dip_enabled": True, "gmgn.dip_pct": 5, "gmgn.dip_timeout_s": 30})
    # desk is PAPER by default; GMGN trader exists so both sides wait on one watch
    c.net.pairs[M] = pair(M, price=2.0, liq=60000, name="EXC", symbol="EXC")
    ex = Executor(c)
    item = {"scan": {"mint": M, "name": "EXC", "symbol": "EXC", "liquidityUsd": 60000},
            "analyst": {"score": 77}, "ref_price": 2.0}
    out = run(ex.run(item, pair=c.net.pairs[M]))
    assert out["status"] == "watching" and c.db.one("SELECT id FROM positions") is None
    assert c.db.one("SELECT id FROM gmgn_orders") is None
    d = c.db.one("SELECT * FROM gmgn_dips")
    assert d["status"] == "watching" and d["want_gmgn"] == 1 and d["fomo_item"]
    run(c.dip.tick(prices={M: 1.90}, liqs={M: 60000}))              # -5%
    pos = c.db.one("SELECT * FROM positions")
    assert pos and pos["status"] == "open" and pos["entry_price"] == 1.90
    o = c.db.one("SELECT * FROM gmgn_orders")
    assert o and o["mode"] == "PAPER" and o["entry_price"] == 1.90
    assert c.db.one("SELECT reason FROM gmgn_dips")["reason"] == "dip"
    # one fill per night
    assert run(ex.run(item, pair=c.net.pairs[M])) is None


def test_fomo_timeout_fills_even_if_price_went_up(monkeypatch):
    monkeypatch.setattr(asyncio, "sleep", _nosleep)
    c, clock, t = trader(**{"gmgn.dip_enabled": True})
    c.net.pairs[M] = pair(M, price=2.0, liq=60000, name="EXC", symbol="EXC")
    ex = Executor(c)
    run(ex.run({"scan": {"mint": M, "name": "EXC", "liquidityUsd": 60000}, "analyst": {"score": 77}, "ref_price": 2.0},
               pair=c.net.pairs[M]))
    clock.t += 30
    run(c.dip.tick(prices={M: 2.40}, liqs={M: 60000}))              # pumped, no dip: still buy at market
    pos = c.db.one("SELECT * FROM positions")
    assert pos["entry_price"] == 2.40
    assert c.db.one("SELECT reason FROM gmgn_dips")["reason"] == "timeout"


def test_pipeline_push_titles_and_live_feed(monkeypatch):
    c, clock, t, fc = _dip(monkeypatch)
    run(t.on_pass(M, "Dip", "DIP", 1.0, 50000, 80))
    watch = next(m for m in c.notifier.sent if m["kind"] == "stage_dip" and "Dip watch" in m["text"])
    n = webpush.render("stage_dip", watch["text"], M)
    assert n["cat"] == "pipeline" and n["tag"] == f"pipe-{M}"
    assert n["title"] == "Dip · waiting for a dip" and "need -5%" in n["body"] and n["url"] == "#/live"
    fd = Builder(DeskStub(c)).build()["feed.json"]
    assert fd["dips"] and fd["dips"][0]["mint"] == M and fd["dips"][0]["needPct"] == 5
    assert "r=savipww" in fd["dips"][0]["link"]
    assert any(i["stage"] == "dip" and i["outcome"] == "watching" for i in fd["items"])
    run(c.dip.tick(prices={M: 0.95}, liqs={M: 50000}))
    buy = next(m for m in c.notifier.sent if "🎯 Dip buy" in m["text"])
    n2 = webpush.render("stage_dip", buy["text"], M)
    assert n2["title"] == "Dip · buying the dip" and "down 5.0%" in n2["body"] and n2["renotify"] is True
    to_txt = "🎯 Dip timeout · Dip\nNo 5% dip in 30s · buying at market\n" + links.coin_url(M)
    n3 = webpush.render("stage_dip", to_txt, M)
    assert n3["title"] == "Dip · buying (no dip)" and "buying at market" in n3["body"]
    sk = webpush.render("stage_dip", "Dip skipped · Dip\nliquidity $1,000 is under the $10,000 floor\n" + links.coin_url(M), M)
    assert sk["title"] == "Dip · dip skipped" and "liquidity $1,000" in sk["body"]
    fd2 = Builder(DeskStub(c)).build()["feed.json"]
    assert fd2["dips"] == []
    assert any(i["stage"] == "dip" and i["outcome"] == "dip_buy" for i in fd2["items"])


def test_paper_gmgn_timeout_too(monkeypatch):
    c, clock, t, fc = _dip(monkeypatch, mode="PAPER")
    r = run(t.on_pass(M, "Dip", "DIP", 1.0, 50000, 80))
    assert r["status"] == "dip" and c.db.one("SELECT id FROM gmgn_orders") is None
    clock.t += 30
    run(c.dip.tick(prices={M: 1.0}, liqs={M: 50000}))
    o = c.db.one("SELECT * FROM gmgn_orders")
    assert o["mode"] == "PAPER" and o["status"] == "open" and o["entry_price"] == 1.0
