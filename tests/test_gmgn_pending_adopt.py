"""DECISION 78: watch a manual wallet buy and adopt it into Strategy A LIVE at break-even.

Until price is back at the recorded entry, the desk does not touch the position. If the owner
sells it themselves, the watch cancels itself. The pending entry lives in meta so it survives
a desk restart. Checks run from the gmgn loop at least every ~10s while a watch is armed.
"""
import json
import pytest
from agents.gmgn_trader import (PENDING_ADOPT_POLL_S, PENDING_ADOPTS_META, limit_stop_only,
                                mint_held_live, open_count)
from gmgn import LAMPORTS
import notif_label as nl
from tests.chainsim import ChainSim
from tests.helpers import pair, run
from tests.test_gmgn import FakeClient, M, go_live, trader

SOL_USD = 70.0
NAME, SYM = "Official Grand Theft Auto VI", "GTAVI"


def _chain(c, clock, sigs=None, sol_paid=0.5, tokens=4_000_000_000, supply=1_000_000_000.0, mint=M):
    sigs = sigs or ["buy1"]
    chain = ChainSim(c.net, mint)
    lam = -int(sol_paid * LAMPORTS)
    chain.add(sigs[0], clock.t, lam, tokens)
    for i, sig in enumerate(sigs[1:], 1):
        chain.add(sig, clock.t + i, lam, tokens)
    chain.balance = tokens * len(sigs)
    c.net.rpc_map["getTokenSupply"] = lambda p: {"value": {"uiAmount": supply}}
    return chain


def _live(**settings):
    base = {"risk.stop_pct": 50, "risk.trail_pct": 20, "risk.trail_start_pct": 20,
            "risk.poll_s": 15, "risk.surge_sell_enabled": True}
    base.update(settings)
    c, clock, t = trader(FakeClient(sol=SOL_USD), **base)
    go_live(c)
    return c, clock, t


def test_loop_wait_shortens_to_10s_only_while_a_watch_is_armed():
    c, clock, t = _live()
    assert t._loop_wait_s() == 15
    t.queue_pending_adopt(M, NAME, SYM, ["buy1"])
    assert t._loop_wait_s() == PENDING_ADOPT_POLL_S == 10
    t.set_pending_adopts([])
    assert t._loop_wait_s() == 15


def test_queue_survives_meta_reload_and_does_not_open_a_row():
    c, clock, t = _live()
    t.queue_pending_adopt(M, NAME, SYM, ["buy1"], note="owner asked to protect at break even")
    raw = c.db.meta(PENDING_ADOPTS_META)
    assert len(raw) == 1 and raw[0]["mint"] == M and raw[0]["sigs"] == ["buy1"]
    assert raw[0]["notified"] is False
    # a new trader on the same db (desk restart) still sees it
    from agents.gmgn_trader import GmgnTrader
    t2 = GmgnTrader(c, FakeClient(sol=SOL_USD))
    got = t2.pending_adopts()
    assert got[0]["symbol"] == SYM and c.db.one("SELECT COUNT(*) n FROM gmgn_orders")["n"] == 0


def test_below_entry_only_notifies_setup_and_leaves_the_bag_alone():
    c, clock, t = _live()
    chain = _chain(c, clock)
    t.queue_pending_adopt(M, NAME, SYM, ["buy1"])
    entry = 0.5 * SOL_USD / (4_000_000_000 / 1e6)
    run(t.check_pending_adopts(prices={M: entry * 0.5}))
    rec = t.pending_adopts()[0]
    assert rec["notified"] is True
    assert rec["entry_price"] == pytest.approx(entry, rel=1e-9)
    assert rec["size_usd"] == round(0.5 * SOL_USD, 2)
    texts = [m["text"] for m in c.notifier.sent]
    assert texts == [f"Desk · watching {SYM}, Strat A protection at break even (${rec['size_usd']:,.2f})"]
    assert nl.telegram("info", texts[0]) == texts[0]
    assert c.db.one("SELECT COUNT(*) n FROM gmgn_orders")["n"] == 0
    assert not [b for k, b in t.client.calls if k == "strategy_create"]
    assert not [b for k, b in t.client.calls if k == "swap"]
    # a second tick does not re-notify
    run(t.check_pending_adopts(prices={M: entry * 0.5}))
    assert [m["text"] for m in c.notifier.sent] == texts
    assert t.pending_adopts()


def test_at_or_above_entry_adopts_like_dior_and_removes_the_watch():
    c, clock, t = _live()
    chain = _chain(c, clock, sigs=["buy1", "buy2"], sol_paid=0.25, tokens=2_000_000_000)
    t.queue_pending_adopt(M, NAME, SYM, ["buy1", "buy2"], note="adopted manual buy")
    entry = 0.5 * SOL_USD / (4_000_000_000 / 1e6)
    run(t.check_pending_adopts(prices={M: entry}))          # exactly break even
    assert t.pending_adopts() == []
    o = c.db.one("SELECT * FROM gmgn_orders WHERE mint=?", (M,))
    assert o["mode"] == "LIVE" and o["status"] == "open" and o["reason"] == "adopted manual buy"
    audit = json.loads(o["audit"])
    assert audit["adopted"] is True and [b["sig"] for b in audit["buys"]] == ["buy1", "buy2"]
    assert o["entry_src"] == "chain" and o["tx"] == "buy2"
    assert o["lamports"] == int(0.5 * LAMPORTS)
    assert o["size_usd"] == round(0.5 * SOL_USD, 2)
    assert o["entry_price"] == pytest.approx(entry, rel=1e-9)
    assert o["exits_phase"] == "normal" and o["grace_until"] is None and o["no_surge"] == 0
    assert o["repair"] == "adopted manual buy; stop_loss strategy created"
    assert limit_stop_only(o)
    st = [b for k, b in t.client.calls if k == "strategy_create"]
    assert len(st) == 1 and st[0]["sub_order_type"] == "stop_loss" and st[0]["amount_in_percent"] == "100"
    assert st[0]["amount_in"] == str(4_000_000_000)          # 100% of tokens held at adopt time
    assert float(st[0]["check_price"]) == pytest.approx(entry * 0.5, rel=1e-9)
    assert o["strategy_order_id"] == "s1"
    assert json.loads(o["conditions"]) == [{"order_type": "loss_stop", "price_scale": "50", "sell_ratio": "100"}]
    assert not [b for k, b in t.client.calls if k == "swap"]
    assert mint_held_live(c.db, M) == "A" and open_count(c.db, "LIVE", "A") == 1
    texts = [m["text"] for m in c.notifier.sent]
    assert f"Desk · watching {SYM}, Strat A protection at break even (${o['size_usd']:,.2f})" in texts
    assert f"Strat A · {SYM} back to break even, now protected" in texts
    assert nl.telegram("gmgn", texts[-1]) == texts[-1]
    # already adopted: a leftover watch would refuse, and a second adopt does not create another stop
    r2 = run(t.adopt(M, NAME, SYM, ["buy1", "buy2"]))
    assert r2["status"] == "refused" and "already held by A" in r2["reason"]
    assert len([b for k, b in t.client.calls if k == "strategy_create"]) == 1


def test_already_above_entry_on_first_tick_adopts_immediately():
    c, clock, t = _live()
    _chain(c, clock)
    t.queue_pending_adopt(M, NAME, SYM, ["buy1"])
    entry = 0.5 * SOL_USD / (4_000_000_000 / 1e6)
    run(t.check_pending_adopts(prices={M: entry * 1.01}))
    assert t.pending_adopts() == []
    o = c.db.one("SELECT * FROM gmgn_orders WHERE mint=?", (M,))
    assert o["status"] == "open" and o["reason"] == "adopted manual buy"
    texts = [m["text"] for m in c.notifier.sent]
    assert any(t.startswith("Desk · watching") for t in texts)
    assert f"Strat A · {SYM} back to break even, now protected" in texts


def test_owner_sold_cancels_the_watch_and_does_not_adopt():
    c, clock, t = _live()
    chain = _chain(c, clock)
    t.queue_pending_adopt(M, NAME, SYM, ["buy1"])
    entry = 0.5 * SOL_USD / (4_000_000_000 / 1e6)
    run(t.check_pending_adopts(prices={M: entry * 0.4}))     # setup notify, still held
    chain.balance = 0
    run(t.check_pending_adopts(prices={M: entry * 2}))       # price is up, but tokens are gone
    assert t.pending_adopts() == []
    assert c.db.one("SELECT COUNT(*) n FROM gmgn_orders")["n"] == 0
    assert not [b for k, b in t.client.calls if k == "strategy_create"]
    texts = [m["text"] for m in c.notifier.sent]
    assert f"Desk · {SYM} watch cancelled (sold)" in texts
    assert not any("now protected" in x for x in texts)


def test_adopt_uses_tokens_held_now_and_reuses_an_existing_stop():
    c, clock, t = _live()
    async def _open(**kw):
        return {"list": [{"order_id": "exist1", "base_token": M, "status": "open", "sub_order_type": "stop_loss"}]}
    t.client.strategy_orders = _open
    chain = _chain(c, clock)
    chain.balance = 3_000_000_000                            # owner sold 25%; remaining is what the stop covers
    r = run(t.adopt(M, NAME, SYM, ["buy1"], note="adopted manual buy"))
    assert r["status"] == "open" and r["strategy_order_id"] == "exist1" and r["held_raw"] == 3_000_000_000
    assert not [b for k, b in t.client.calls if k == "strategy_create"]
    o = c.db.one("SELECT * FROM gmgn_orders WHERE id=?", (r["id"],))
    assert "existing GMGN strategy" in o["repair"]
    assert o["lamports"] == int(0.5 * LAMPORTS)


def test_unknown_balance_or_missing_price_waits_and_does_not_cancel():
    c, clock, t = _live()
    chain = _chain(c, clock)
    t.queue_pending_adopt(M, NAME, SYM, ["buy1"])
    # no price this tick: after setup notify, the watch stays
    run(t.check_pending_adopts(prices={}))
    assert t.pending_adopts() and c.db.one("SELECT COUNT(*) n FROM gmgn_orders")["n"] == 0
    # RPC failed (no token-accounts value): must not treat that as a sell
    c.net.rpc_map["getTokenAccountsByOwner"] = lambda p: None
    entry = 0.5 * SOL_USD / (4_000_000_000 / 1e6)
    run(t.check_pending_adopts(prices={M: entry * 2}))
    assert t.pending_adopts() and c.db.one("SELECT COUNT(*) n FROM gmgn_orders")["n"] == 0
    assert not any("cancelled (sold)" in m["text"] for m in c.notifier.sent)
