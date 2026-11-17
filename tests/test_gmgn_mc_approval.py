"""DECISION 53: GMGN LIVE buys above gmgn.approve_above_mc_usd wait for the owner's Approve on Telegram
(owner 2026-11-16 6:08 PM CT). At or under the limit the automatic behaviour is unchanged."""
import asyncio
import config, links
from tests.helpers import pair, run
from tests.test_gmgn import M
from tests.test_gmgn_grace import GraceClient, _held, _live, _nosleep
from tests.test_overrides import DeskStub, OWNER, _tg, cb, msg


def _setup(monkeypatch, mc=500_000, limit=300_000, **kw):
    monkeypatch.setattr(asyncio, "sleep", _nosleep)
    fc = GraceClient()
    c, clock, t = _live(fc, **{"gmgn.approve_above_mc_usd": limit, "scanner.min_liquidity_usd": 10000, **kw})
    _held(t)
    p = pair(M, price=1.0, liq=50000, name="Big", symbol="BIG"); p["marketCap"] = mc
    c.net.pairs[M] = p
    return c, clock, t, fc


def _buys(fc):
    return [b for k, b in fc.calls if k == "swap" and b.get("output_token") == M]


def _asks(c):
    return [m for m in c.notifier.sent if m["kind"] == "gmgn_approve" and "APPROVE?" in m["text"]]


def test_setting_default_off_not_protected():
    assert config.BY_KEY["gmgn.approve_above_mc_usd"].default == 0
    assert "gmgn.approve_above_mc_usd" not in config.PROTECTED


def test_at_or_under_limit_buys_automatically(monkeypatch):
    c, clock, t, fc = _setup(monkeypatch, mc=300_000)
    r = run(t.on_pass(M, "Big", "BIG", 1.0, 50000, 80, mc=300_000))
    assert r["status"] == "open" and len(_buys(fc)) == 1 and not _asks(c)


def test_over_limit_asks_owner_and_does_not_buy(monkeypatch):
    c, clock, t, fc = _setup(monkeypatch)
    r = run(t.on_pass(M, "Big", "BIG", 1.0, 50000, 80, mc=450_000))
    assert r["status"] == "approval" and not _buys(fc)
    assert c.db.one("SELECT id FROM gmgn_orders") is None
    a = _asks(c)
    assert len(a) == 1 and "$450,000" in a[0]["text"] and "$300,000" in a[0]["text"]
    url = links.coin_url(M)
    assert "r=savipww" in url and url in a[0]["text"]
    flat = [b for row in a[0]["buttons"] for b in row]
    assert {"text": "Open on FOMO", "url": url} in flat
    aid = c.db.one("SELECT id FROM gmgn_approvals")["id"]
    assert {b.get("callback_data") for b in flat} >= {f"gmap:{aid}", f"gmsk:{aid}"}
    # a second pass of the same coin doesn't ask again
    assert run(t.on_pass(M, "Big", "BIG", 1.0, 50000, 80, mc=460_000))["status"] == "approval"
    assert len(_asks(c)) == 1


def test_unknown_mc_is_fetched_and_paper_never_asks(monkeypatch):
    c, clock, t, fc = _setup(monkeypatch, mc=900_000)
    assert run(t.on_pass(M, "Big", "BIG", 1.0, 50000, 80))["status"] == "approval"      # mc read from DexScreener
    c2, _, t2, fc2 = _setup(monkeypatch)
    c2.s.set("gmgn.mode", "PAPER", source="test", force=True)
    assert run(t2.on_pass(M, "Big", "BIG", 1.0, 50000, 80, mc=900_000))["status"] == "paper"
    assert not _asks(c2)


def test_approve_buys_once_after_fresh_check(monkeypatch):
    c, clock, t, fc = _setup(monkeypatch)
    run(t.on_pass(M, "Big", "BIG", 1.0, 50000, 80, mc=450_000))
    aid = c.db.one("SELECT id FROM gmgn_approvals")["id"]
    clock.t += 120
    out = run(t.approve(aid))
    assert "Approved Big" in out and "$500,000" in out and len(_buys(fc)) == 1
    o = c.db.one("SELECT * FROM gmgn_orders")
    assert o["status"] == "open" and o["exits_phase"] == "grace" and o["strategy_order_id"]
    assert c.db.one("SELECT status FROM gmgn_approvals")["status"] == "done"
    assert "already" in run(t.approve(aid)) and len(_buys(fc)) == 1                     # 2nd tap: no 2nd buy


def test_approve_refuses_on_dump_or_low_liquidity(monkeypatch):
    c, clock, t, fc = _setup(monkeypatch)
    run(t.on_pass(M, "Big", "BIG", 1.0, 50000, 80, mc=450_000))
    c.net.pairs[M]["priceUsd"] = "0.75"
    assert "down 25%" in run(t.approve(1)) and not _buys(fc)
    assert c.db.one("SELECT status FROM gmgn_approvals")["status"] == "refused"


def test_skip_and_expiry(monkeypatch):
    c, clock, t, fc = _setup(monkeypatch)
    run(t.on_pass(M, "Big", "BIG", 1.0, 50000, 80, mc=450_000))
    assert "Skipped Big" in run(t.skip_approval(1)) and not _buys(fc)
    assert run(t.on_pass(M, "Big", "BIG", 1.0, 50000, 80, mc=450_000)) is None          # skipped tonight: no re-ask
    c2, clock2, t2, fc2 = _setup(monkeypatch)
    run(t2.on_pass(M, "Big", "BIG", 1.0, 50000, 80, mc=450_000))
    clock2.t += 601
    run(t2.expire_approvals())
    assert c2.db.one("SELECT status FROM gmgn_approvals")["status"] == "expired"
    assert any("expired" in m["text"] and "Nothing was bought" in m["text"] for m in c2.notifier.sent)
    assert "already expired" in run(t2.approve(1)) and not _buys(fc2)


def test_telegram_buttons_and_text_owner_only(monkeypatch):
    c, clock, t, fc = _setup(monkeypatch)
    d = DeskStub.__new__(DeskStub); d.ctx, d.gmgn = c, t
    tg = _tg(d)
    run(t.on_pass(M, "Big", "BIG", 1.0, 50000, 80, mc=450_000))
    run(tg.handle(cb(12345, "gmap:1")))                                                 # not the owner
    run(tg.handle(msg(12345, "approve 1")))
    assert not _buys(fc) and c.db.one("SELECT status FROM gmgn_approvals")["status"] == "asked"
    run(tg.handle(cb(OWNER, "gmap:1")))
    assert len(_buys(fc)) == 1 and any("Approved Big" in m["text"] for m in tg.sent)
    c.db.x("UPDATE gmgn_orders SET status='closed'")
    c.db.x("INSERT INTO gmgn_approvals(ts,night,mint,name,status,expires,price) VALUES(?,?,?,?,?,?,?)",
           (clock.t, "other", "X" * 44, "Other", "asked", clock.t + 600, 1.0))
    run(tg.handle(msg(OWNER, "skip 2")))
    assert c.db.one("SELECT status FROM gmgn_approvals WHERE id=2")["status"] == "skipped"
