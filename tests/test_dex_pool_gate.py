"""DECISION 86: skip a new A or B LIVE buy when DexScreener and the on-chain pool differ by more than 30%."""
import pytest
import config
import fastprice as fp
import notif_label as nl
from tests.helpers import make_ctx, run, pair
from tests.test_gmgn import trader, go_live, FakeClient, M
from tests.test_strat_b_live_mode import _live, _signal, _orders, _swaps


def test_setting_defaults_and_hot_load():
    import json, time
    c, _ = make_ctx()
    assert c.s["gmgn.max_dex_pool_diff_pct"] == 30.0
    assert "gmgn.max_dex_pool_diff_pct" not in config.PROTECTED
    assert config.resolve("dex pool diff").key == "gmgn.max_dex_pool_diff_pct"
    assert config.resolve("max dex pool diff").key == "gmgn.max_dex_pool_diff_pct"
    c.db.x("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
           ("gmgn.max_dex_pool_diff_pct", json.dumps(40.0)))
    c.db.x("INSERT INTO change_log(ts,key,old,new,source) VALUES(?,?,?,?,?)",
           (time.time(), "gmgn.max_dex_pool_diff_pct", json.dumps(30.0), json.dumps(40.0), "chat"))
    assert "gmgn.max_dex_pool_diff_pct" in c.s.sync() and c.s["gmgn.max_dex_pool_diff_pct"] == 40.0


def test_diff_math_and_amazon_numbers():
    # AMAZON #84: pool 1.03e-05 vs dex 5.99e-05 (~6x, about -83%)
    d = fp.dex_pool_diff_pct(5.99e-05, 1.03e-05)
    assert d == pytest.approx(100 * (1.03e-05 / 5.99e-05 - 1), rel=1e-9)
    assert abs(d) > 30
    assert fp.dex_pool_diff_pct(1.0, 1.20) == pytest.approx(20.0)
    assert fp.dex_pool_diff_pct(1.0, 0.70) == pytest.approx(-30.0)
    assert fp.dex_pool_diff_pct(0, 1.0) is None
    assert fp.dex_pool_diff_pct(1.0, None) is None


def test_check_blocks_only_when_both_prices_known_and_over_limit(monkeypatch):
    c, _ = make_ctx()

    async def pool(net, mint, **kw):
        return 1.03e-05, None
    monkeypatch.setattr(fp, "read_pool_usd", pool)

    res = run(fp.check_buy_dex_pool(c, "mint", 5.99e-05, sol_usd=70, max_pct=30))
    assert res["blocked"] is True and "83%" in res["reason"]
    assert res["pool_usd"] == 1.03e-05
    # 20% off: hold
    async def near(net, mint, **kw):
        return 1.20, None
    monkeypatch.setattr(fp, "read_pool_usd", near)
    res = run(fp.check_buy_dex_pool(c, "mint", 1.0, max_pct=30))
    assert res["blocked"] is False and res["diff_pct"] == pytest.approx(20.0)
    # exactly 30%: not more than 30, so allow (100/130 avoids float noise on 1.30)
    async def edge(net, mint, **kw):
        return 130.0, None
    monkeypatch.setattr(fp, "read_pool_usd", edge)
    res = run(fp.check_buy_dex_pool(c, "mint", 100.0, max_pct=30))
    assert res["blocked"] is False and res["diff_pct"] == pytest.approx(30.0)
    # just over
    async def over(net, mint, **kw):
        return 130.1, None
    monkeypatch.setattr(fp, "read_pool_usd", over)
    res = run(fp.check_buy_dex_pool(c, "mint", 100.0, max_pct=30))
    assert res["blocked"] is True


def test_unreadable_pool_or_zero_setting_does_not_block(monkeypatch):
    c, _ = make_ctx()

    async def miss(net, mint, **kw):
        return None, "rpc RuntimeError"
    monkeypatch.setattr(fp, "read_pool_usd", miss)
    res = run(fp.check_buy_dex_pool(c, "mint", 1.0, max_pct=30))
    assert res["blocked"] is False and res["error"] == "rpc RuntimeError"

    async def far(net, mint, **kw):
        return 0.1, None
    monkeypatch.setattr(fp, "read_pool_usd", far)
    res = run(fp.check_buy_dex_pool(c, "mint", 1.0, max_pct=0))
    assert res["blocked"] is False and res["error"] == "off"
    res = run(fp.check_buy_dex_pool(c, "mint", 0, max_pct=30))
    assert res["blocked"] is False and res["error"] == "no dex price"


def test_a_skips_amazon_like_divergence_and_does_not_swap(monkeypatch):
    async def pool(net, mint, **kw):
        return 1.03e-05, None
    monkeypatch.setattr(fp, "read_pool_usd", pool)
    fc = FakeClient(plan_blocked=False)
    c, _, t = trader(fc)
    go_live(c)
    r = run(t.on_pass(M, "AMAZON", "AMAZON", 5.99e-05, 16000, 80))
    assert r["status"] == "refused" and "83%" in r["reason"] and "on-chain pool" in r["reason"]
    assert "swap" not in fc.kinds()
    o = c.db.one("SELECT * FROM gmgn_orders WHERE mint=?", (M,))
    assert o["status"] == "refused" and "83%" in o["reason"]
    text = c.notifier.sent[-1]["text"]
    assert text.startswith("GMGN · Skipped AMAZON:") and "on-chain pool" in text
    who, title = nl.headline("gmgn_refused", text)
    assert who == "Strat A" and title.startswith("Strat A skipped AMAZON") and "83%" in title


def test_a_still_buys_when_pool_unreadable(monkeypatch):
    async def miss(net, mint, **kw):
        return None, "rpc RuntimeError"
    monkeypatch.setattr(fp, "read_pool_usd", miss)
    fc = FakeClient()
    c, _, t = trader(fc)
    r = run(t.on_pass(M, "Gm", "GM", 1.0, 50000, 80))
    assert r["status"] == "paper" and "swap" not in fc.kinds()
    assert c.db.one("SELECT status FROM gmgn_orders WHERE mint=?", (M,))["status"] == "open"


def test_b_live_skips_amazon_like_divergence(monkeypatch):
    # Buy tick price is 0.85; scale the AMAZON #84 ratio so the skip reason still shows ~83%.
    async def pool(net, mint, **kw):
        return 0.85 * (1.03e-05 / 5.99e-05), None
    monkeypatch.setattr(fp, "read_pool_usd", pool)
    c, clock, sb, cl, chain = _live(wallet_sol=5.0)
    _signal(c, clock, sb, chain)
    rows = _orders(c)
    assert len(rows) == 1 and rows[0]["status"] == "refused"
    assert "83%" in (rows[0]["note"] or "") and "on-chain pool" in rows[0]["note"]
    assert not _swaps(cl)
    pushes = [m["text"] for m in c.notifier.sent if "Skipped" in m["text"]]
    assert len(pushes) == 1 and pushes[0].startswith("B LIVE · Skipped Cat:")
    who, title = nl.headline("strat_b", pushes[0])
    assert who == "Strat B" and title.startswith("Strat B skipped Cat") and "83%" in title


def test_open_position_exit_feed_still_accepts_jupiter(monkeypatch):
    """The exit-price sanity check is unchanged: Jupiter can still vouch for a pool far from DexScreener."""
    from tests.test_fastprice import test_sanity_check_accepts_pool_when_jupiter_agrees
    test_sanity_check_accepts_pool_when_jupiter_agrees(monkeypatch)
