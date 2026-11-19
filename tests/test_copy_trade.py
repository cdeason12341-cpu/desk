"""DECISION 92 (owner 2026-11-19 4:55 PM CT, t456u): Strategy A hybrid profile. Small $5 PAPER scanner trades
(max 2 open, Jupiter can-it-sell check) until the copied wallet (himOthy) buys, then A copies his buys and sells in
PAPER, sized by the fraction of his SOL he spent, with only a -50% backstop. Copy is read-only and PAPER-only."""
import json
import pytest
import config, copytrade, gmgn
from agents.gmgn_trader import small_open_count, paper_free_usd
from publisher import Builder, _a_label
from tests.helpers import pair, run
from tests.test_gmgn import FakeClient, trader
from tests.test_publisher_gmgn import DeskStub

W = "HMSwHxC3Qnq1PN5pFxBK4taemi8NQPvN3ZPxuCRTgFsE"
MT = "B34nXKzCgsJb9ChLuSHPRpXLE3WFSWyeT8Cqkzzmpump"
M2 = "mXcP1b111111111111111111111111111111111pump"
L = 1_000_000_000


def tx(sig, sol_pre, sol_post, tok_pre=0, tok_post=0, mint=MT, ts=1_800_000_000, err=None, wsol=None, owner=W):
    pre_t, post_t = [], []
    if tok_pre or tok_post:
        pre_t.append({"owner": owner, "mint": mint, "uiTokenAmount": {"amount": str(tok_pre), "decimals": 6}})
        post_t.append({"owner": owner, "mint": mint, "uiTokenAmount": {"amount": str(tok_post), "decimals": 6}})
    if wsol:
        pre_t.append({"owner": owner, "mint": copytrade.SOL, "uiTokenAmount": {"amount": str(wsol[0]), "decimals": 9}})
        post_t.append({"owner": owner, "mint": copytrade.SOL, "uiTokenAmount": {"amount": str(wsol[1]), "decimals": 9}})
    return {"blockTime": ts, "meta": {"err": err, "fee": 5000, "preBalances": [int(sol_pre)], "postBalances": [int(sol_post)],
                                      "preTokenBalances": pre_t, "postTokenBalances": post_t},
            "transaction": {"signatures": [sig], "message": {"accountKeys": [{"pubkey": W}]}}}


def mt_target(i, o):
    return o if i == copytrade.USDC else None


def ok_quote(monkeypatch, fail=None):
    calls = []

    async def q(i, o, amt, *a, **k):
        calls.append((i, o, amt))
        if fail == "sell" and o == copytrade.SOL:
            return {"ok": False, "no_route": True, "error": "COULD_NOT_FIND_ANY_ROUTE"}
        if fail == "nottradable" and o == mt_target(i, o):
            return {"ok": False, "no_route": True, "error": "TOKEN_NOT_TRADABLE"}
        if fail == "error":
            return {"ok": False, "no_route": False, "error": "jupiter 500"}
        return {"ok": True, "out": 1000, "impact_pct": 0.5, "route": "Pump.fun Amm"}
    monkeypatch.setattr(copytrade, "quote", q)
    return calls


class Chain:
    """Fake RPC: getSignaturesForAddress newest first, getTransaction by sig."""
    def __init__(self, c):
        self.sigs, self.txs = [], {}
        c.net.rpc_map["getSignaturesForAddress"] = lambda p: [dict(x) for x in self.sigs]
        c.net.rpc_map["getTransaction"] = lambda p: self.txs.get(p[0])

    def add(self, t, ts):
        sig = t["transaction"]["signatures"][0]
        t["blockTime"] = ts
        self.txs[sig] = t
        self.sigs.insert(0, {"signature": sig, "blockTime": ts, "err": None})


def hybrid(monkeypatch, fail=None, **kw):
    kw.setdefault("gmgn.profile", "hybrid")
    kw.setdefault("copy.wallet", W)
    kw.setdefault("copy.name", "himOthy")
    kw.setdefault("copy.sizing", "fraction")         # the himOthy rules; flat/first-buy tests opt in below
    kw.setdefault("copy.first_buy_only", False)
    c, clock, t = trader(FakeClient(plan_blocked=False, wallet_sol=0.0), **kw)
    calls = ok_quote(monkeypatch, fail)
    w = copytrade.CopyWatcher(c, t)
    ch = Chain(c)
    c.net.pairs[MT] = pair(MT, price=0.01, liq=200000, name="Fiona", symbol="FIONA")
    c.net.pairs[M2] = pair(M2, price=0.002, liq=100000, name="Mx", symbol="MX")
    return c, clock, t, w, ch, calls


# ---------- settings
def test_settings_defaults_protected_and_names():
    c, _, _ = trader()
    s = c.s
    assert s["gmgn.profile"] == "scanner" and s["gmgn.small_trade_usd"] == 5.0 and s["gmgn.small_max_open"] == 2
    assert s["copy.book_usd"] == 500.0 and s["copy.min_usd"] == 10.0 and s["copy.max_usd"] == 100.0
    assert s["copy.max_open"] == 10 and s["copy.backstop_pct"] == 50.0 and s["copy.wallet"] == ""
    assert s["copy.sizing"] == "flat" and s["copy.flat_usd"] == 20.0 and s["copy.first_buy_only"] is True
    assert "gmgn.profile" in config.PROTECTED
    with pytest.raises(config.SettingError):
        s.set("gmgn.profile", "hybrid", source="test")             # owner command only
    s.set("gmgn.profile", "hybrid", source="test", force=True)
    s.set("copy.wallet", W, source="test")
    with pytest.raises(config.SettingError):
        s.set("copy.wallet", "not-a-wallet", source="test")
    # copy.* never steals a bare name
    assert config.resolve("max open").key == "gmgn.max_open"
    assert config.resolve("copy max open").key == "copy.max_open"
    s.set("gmgn.profile", "scanner", source="test", force=True)     # scanner is restorable
    assert s["gmgn.profile"] == "scanner"


# ---------- tx classification
def test_classify_buy_sell_and_ignored():
    b = copytrade.classify(tx("b", 20 * L, 10 * L - 5000, 0, 500_000_000_000), W)
    assert b["side"] == "buy" and b["mint"] == MT and b["sol"] == 10 * L + 5000
    assert abs(b["frac"] - (10 * L + 5000) / (20 * L)) < 1e-12
    s = copytrade.classify(tx("s", 1 * L, 6 * L, 400, 100), W)
    assert s["side"] == "sell" and abs(s["frac"] - 0.75) < 1e-12
    # reward claim: token dust, no SOL move -> ignored; transfer out (SOL down, token down) -> ignored
    assert copytrade.classify(tx("r", 5 * L, 5 * L - 5000, 10, 20), W) is None
    assert copytrade.classify(tx("t", 5 * L, 5 * L - 5000, 100, 0), W) is None
    assert copytrade.classify(tx("f", 20 * L, 10 * L, 0, 100, err={"x": 1}), W) is None
    assert copytrade.classify(None, W) is None
    # WSOL counts as SOL
    w = copytrade.classify(tx("w", 1 * L, 1 * L, 0, 100, wsol=(4 * L, 2 * L)), W)
    assert w["side"] == "buy" and w["sol"] == 2 * L and abs(w["frac"] - 2 / 5) < 1e-9


def test_copy_size_clamped():
    c, _, _ = trader(**{"copy.sizing": "fraction"})
    assert copytrade.copy_size(c.s, 0.10, 500) == (50.0, 50.0)
    assert copytrade.copy_size(c.s, 0.001, 500)[1] == 10.0
    assert copytrade.copy_size(c.s, 0.9, 500)[1] == 100.0


# ---------- watcher: baseline, buy, partial sell, full sell
def test_baseline_copies_nothing_old_then_buy_and_sells(monkeypatch):
    c, clock, t, w, ch, _ = hybrid(monkeypatch)
    ch.add(tx("old", 60 * L, 10 * L, 0, 900), clock.t - 3600)        # his existing bag
    assert run(w.poll_once()) == "baseline"
    run(w.poll_once())
    assert c.db.one("SELECT COUNT(*) n FROM gmgn_orders")["n"] == 0
    # he spends 10 SOL of 50 (20%) -> $100 of the $500 book (clamped at $100)
    clock.t += 30
    ch.add(tx("buy1", 50 * L, 40 * L, 0, 1_000_000), clock.t - 4)
    note = run(w.poll_once())
    o = c.db.one("SELECT * FROM gmgn_orders WHERE a_kind='copy'")
    assert o["status"] == "open" and o["mode"] == "PAPER" and o["size_usd"] == 100.0 and o["no_surge"] == 1
    assert o["entry_price"] == 0.01 and o["reason"] == "copy himOthy"
    src = json.loads(o["copy_src"])[0]
    assert src["sig"] == "buy1" and src["delay_s"] == 4.0 and src["his_frac"] == 0.2 and src["book"] == 500.0
    assert "full copy" in note and copytrade.status(c)["state"] == "full copy"
    assert "swap" not in t.client.kinds()
    # he sells 40% -> we sell 40%: closed child $40, parent $60 left
    clock.t += 60
    c.net.pairs[MT] = pair(MT, price=0.02, liq=200000, name="Fiona", symbol="FIONA")
    ch.add(tx("sell1", 40 * L, 48 * L, 1_000_000, 600_000), clock.t - 3)
    run(w.poll_once())
    rows = c.db.rows("SELECT * FROM gmgn_orders WHERE a_kind='copy' ORDER BY id")
    assert len(rows) == 2
    parent, child = rows
    assert parent["status"] == "open" and parent["size_usd"] == 60.0
    assert child["status"] == "closed" and child["size_usd"] == 40.0 and child["exit_reason"] == "copy sell"
    assert child["pnl_usd"] == t._pnl({"size_usd": 40.0, "entry_price": 0.01}, 0.02)[0]
    # he sells the rest -> whole copy closed, back to normal
    clock.t += 60
    ch.add(tx("sell2", 48 * L, 60 * L, 600_000, 0), clock.t - 2)
    run(w.poll_once())
    parent = c.db.one("SELECT * FROM gmgn_orders WHERE id=?", (parent["id"],))
    assert parent["status"] == "closed" and parent["exit_reason"] == "copy sell"
    assert copytrade.status(c)["state"] == "normal"
    st = c.db.meta(copytrade.META)
    assert st["last_sig"] == "sell2" and st["trades"] == 3 and st["last_delay_s"] == 2.0


def test_add_to_copy_weighted_and_position_cap(monkeypatch):
    c, clock, t, w, ch, _ = hybrid(monkeypatch, **{"copy.max_position_usd": 150.0})
    run(w.poll_once())
    ch.add(tx("b1", 100 * L, 90 * L, 0, 10), clock.t)              # 10% -> $50
    run(w.poll_once())
    c.net.pairs[MT] = pair(MT, price=0.02, liq=200000)
    ch.add(tx("b2", 90 * L, 72 * L, 10, 20), clock.t)              # 20% -> $100, room only $100
    run(w.poll_once())
    o = c.db.one("SELECT * FROM gmgn_orders WHERE a_kind='copy'")
    assert o["size_usd"] == 150.0
    assert abs(o["entry_price"] - 150 / (50 / 0.01 + 100 / 0.02)) < 1e-12
    ch.add(tx("b3", 72 * L, 60 * L, 20, 30), clock.t)              # at the cap: skipped
    run(w.poll_once())
    assert c.db.one("SELECT size_usd FROM gmgn_orders WHERE a_kind='copy'")["size_usd"] == 150.0
    assert len(json.loads(o["copy_src"])) == 2


def test_sell_check_no_route_skips_copy(monkeypatch):
    c, clock, t, w, ch, _ = hybrid(monkeypatch, fail="sell")
    run(w.poll_once())
    ch.add(tx("b1", 100 * L, 90 * L, 0, 10), clock.t)
    run(w.poll_once())
    o = c.db.one("SELECT * FROM gmgn_orders")
    assert o["status"] == "refused" and "unsellable" in o["reason"] and o["a_kind"] == "copy"


def test_copy_max_open_and_no_mc_filter(monkeypatch):
    c, clock, t, w, ch, _ = hybrid(monkeypatch, **{"copy.max_open": 1, "scanner.min_market_cap_usd": 10_000_000})
    run(w.poll_once())
    ch.add(tx("b1", 100 * L, 90 * L, 0, 10), clock.t)
    ch.add(tx("b2", 90 * L, 80 * L, 0, 10, mint=M2), clock.t)
    run(w.poll_once())
    rows = c.db.rows("SELECT * FROM gmgn_orders ORDER BY id")
    assert rows[0]["status"] == "open"                              # bought despite the $10M market-cap floor
    assert rows[1]["status"] == "refused" and "1/1 copy coins" in rows[1]["reason"]


def test_unindexed_tx_retried_in_order(monkeypatch):
    c, clock, t, w, ch, _ = hybrid(monkeypatch)
    run(w.poll_once())
    ch.add(tx("b1", 100 * L, 90 * L, 0, 10), clock.t)
    real = ch.txs.pop("b1")
    run(w.poll_once())
    assert c.db.one("SELECT COUNT(*) n FROM gmgn_orders")["n"] == 0
    ch.txs["b1"] = real
    run(w.poll_once())
    assert c.db.one("SELECT status FROM gmgn_orders")["status"] == "open"


def test_sell_without_copy_is_ignored(monkeypatch):
    c, clock, t, w, ch, _ = hybrid(monkeypatch)
    run(w.poll_once())
    ch.add(tx("s1", 1 * L, 5 * L, 100, 0), clock.t)                # his old DCTY-style bag
    run(w.poll_once())
    assert c.db.one("SELECT COUNT(*) n FROM gmgn_orders")["n"] == 0


def test_copy_idle_when_scanner(monkeypatch):
    c, clock, t, w, ch, _ = hybrid(monkeypatch)
    c.s.set("gmgn.profile", "scanner", source="test", force=True)
    assert run(w.poll_once()).startswith("idle")


# ---------- DECISION 93: LIVE copies of 9s4ji, every other buy, $20 each, $60 per coin, 5 coins, borrow from B
def live(monkeypatch, wallet_sol=4.0, **kw):
    kw.setdefault("copy.sizing", "flat")
    kw.setdefault("copy.every_n", 2)
    kw.setdefault("copy.max_position_usd", 60.0)
    kw.setdefault("copy.max_open", 5)
    kw.setdefault("copy.name", "9s4ji")
    kw.setdefault("gmgn.live_wallet_share_pct", 50.0)
    kw.setdefault("strat_b.live_wallet_share_pct", 50.0)
    c, clock, t, w, ch, calls = hybrid(monkeypatch, **kw)
    c.s.set("gmgn.mode", "LIVE", source="test", force=True)
    t.client.confirm = True
    c.net.pairs[copytrade.SOL] = pair(copytrade.SOL, price=70.0, liq=5_000_000, name="Wrapped SOL", symbol="SOL")
    bal = {"sol": int(wallet_sol * L), "tok": {}}
    his_sigs = c.net.rpc_map["getSignaturesForAddress"]
    c.net.rpc_map["getSignaturesForAddress"] = lambda p: his_sigs(p) if p[0] == W else []
    c.net.rpc_map["getBalance"] = lambda p: {"value": bal["sol"]}
    c.net.rpc_map["getTokenAccountsByOwner"] = lambda p: {"value": [{"account": {"data": {"parsed": {"info": {
        "tokenAmount": {"amount": str(bal["tok"].get(p[1]["mint"], 0))}}}}}}]}
    monkeypatch.setattr(copytrade, "CONFIRM_GAP_S", 0.0)
    run(w.poll_once())                                               # baseline
    return c, clock, t, w, ch, bal


def swaps(t, side="buy"):
    return [b for k, b in t.client.calls if k == "swap" and ((b["input_token"] == gmgn.SOL) == (side == "buy"))]


def test_live_every_other_buy_20_each_60_cap(monkeypatch):
    c, clock, t, w, ch, bal = live(monkeypatch)
    for i in range(7):                                               # 7 buys of the same coin: #1, #3, #5 copied; #7 hits $60
        clock.t += 5
        ch.add(tx(f"b{i + 1}", 100 * L, 99 * L, i * 10, i * 10 + 10), clock.t)
        run(w.poll_once())
    o = c.db.one("SELECT * FROM gmgn_orders WHERE a_kind='copy' AND mode='LIVE'")
    b = swaps(t)
    assert len(b) == 3 and o["size_usd"] == 60.0 and o["status"] == "open" and o["no_surge"] == 1
    assert all(abs(int(x["input_amount"]) - int(20 / 70 * L)) <= 1 for x in b)
    assert b[0]["condition_orders"] == [{"order_type": "loss_stop", "side": "sell", "price_scale": "50", "sell_ratio": "100"}]
    assert "condition_orders" not in b[1] and "condition_orders" not in b[2]   # adds ride the first buy's backstop
    assert json.loads(o["audit"])["buys"] and json.loads(o["audit"]).get("pending") == []
    assert w.state()["buys"] == 7 and w.state()["mint_buys"][MT] == 7
    assert c.db.one("SELECT COUNT(*) n FROM gmgn_orders WHERE status='refused'")["n"] == 0
    # he sells half -> 50% of our whole position; then the rest -> 100%, row exits, closes when the wallet is empty
    clock.t += 5
    ch.add(tx("s1", 10 * L, 12 * L, 1000, 500), clock.t)
    run(w.poll_once())
    s = swaps(t, "sell")
    assert len(s) == 1 and s[0]["input_amount_bps"] == "5000" and s[0]["input_token"] == MT
    clock.t += 5
    ch.add(tx("s2", 12 * L, 14 * L, 500, 0), clock.t)
    run(w.poll_once())
    s = swaps(t, "sell")
    assert s[-1]["input_amount_bps"] == "10000"
    o = c.db.one("SELECT * FROM gmgn_orders WHERE id=?", (o["id"],))
    assert o["status"] == "exiting" and o["exit_reason"] == "copy sell"
    clock.t += 60
    bal["tok"][MT] = 0
    run(w.live_tick(o, 0.01, 1000, clock.t))
    assert c.db.one("SELECT status FROM gmgn_orders WHERE id=?", (o["id"],))["status"] == "closed"


def test_live_every_other_counts_per_coin(monkeypatch):
    # DECISION 94 (owner 5:40 PM CT): every other buy IN THE SAME COIN; a new coin's first buy is always copied
    c, clock, t, w, ch, bal = live(monkeypatch, wallet_sol=40.0)
    seq = [MT, M2, MT, M2, MT]                       # per coin: MT #1 #2 #3, M2 #1 #2
    for i, m in enumerate(seq):
        clock.t += 5
        ch.add(tx(f"p{i}", 100 * L, 99 * L, i * 10, i * 10 + 10, mint=m), clock.t)
        run(w.poll_once())
    rows = {o["mint"]: o for o in c.db.rows("SELECT * FROM gmgn_orders WHERE a_kind='copy' AND mode='LIVE'")}
    assert rows[MT]["size_usd"] == 40.0 and rows[M2]["size_usd"] == 20.0   # MT #1 + #3; M2 #1 only
    assert len(swaps(t)) == 3
    assert w.state()["mint_buys"] == {MT: 3, M2: 2} and w.state()["buys"] == 5


def test_live_max_5_coins_and_insufficient_balance_no_gmgn_call(monkeypatch):
    c, clock, t, w, ch, bal = live(monkeypatch, wallet_sol=40.0, **{"copy.every_n": 1})
    mints = [f"Cn{i:02d}" + "1" * 36 + "pump" for i in range(6)]
    for i, m in enumerate(mints):
        c.net.pairs[m] = pair(m, price=0.01, liq=200000, name=f"C{i}")
        clock.t += 5
        ch.add(tx(f"c{i}", 100 * L, 99 * L, 0, 10, mint=m), clock.t)
        run(w.poll_once())
    assert len(swaps(t)) == 5
    r = c.db.one("SELECT * FROM gmgn_orders WHERE status='refused'")
    assert r["mint"] == mints[5] and "5/5 copy coins" in r["reason"]
    # broke wallet: refused before any GMGN request
    c2, clock2, t2, w2, ch2, bal2 = live(monkeypatch, wallet_sol=0.1, **{"copy.every_n": 1})
    n0 = len(t2.client.calls)
    clock2.t += 5
    ch2.add(tx("z1", 100 * L, 99 * L, 0, 10), clock2.t)
    run(w2.poll_once())
    r = c2.db.one("SELECT * FROM gmgn_orders WHERE status='refused'")
    assert r and "insufficient balance" in r["reason"] and r["mode"] == "LIVE"
    assert len(t2.client.calls) == n0                                # no gas_price / swap / quote to GMGN


def test_live_borrows_b_unused_share_keeps_one_b_buy(monkeypatch):
    # 1.0 SOL wallet, A share 20%: 0.196 SOL can't cover a $20 buy (~0.292 SOL with fees) -> borrow B's unused share
    c, clock, t, w, ch, bal = live(monkeypatch, wallet_sol=1.0, **{"copy.every_n": 1, "gmgn.live_wallet_share_pct": 20.0})
    clock.t += 5
    ch.add(tx("b1", 100 * L, 99 * L, 0, 10), clock.t)
    run(w.poll_once())
    assert len(swaps(t)) == 1
    src = json.loads(c.db.one("SELECT copy_src FROM gmgn_orders WHERE mint=?", (MT,))["copy_src"])
    assert src[0]["borrow_lamports"] > 0
    # wallet after that buy: free 0.57 SOL, B has nothing open -> one B $20 buy + fees stays: refused
    bal["sol"] = int(0.59 * L)
    clock.t += 5
    ch.add(tx("b2", 100 * L, 99 * L, 0, 10, mint=M2), clock.t)
    run(w.poll_once())
    assert len(swaps(t)) == 1
    r = c.db.one("SELECT reason FROM gmgn_orders WHERE mint=?", (M2,))
    assert "insufficient balance" in r["reason"] and "for one B buy" in r["reason"]


def test_live_small_scanner_stays_paper_and_watch_skips_copy_rows(monkeypatch):
    c, clock, t, w, ch, bal = live(monkeypatch)
    r = run(t.on_pass("Sm01" + "1" * 36 + "pump", "S", "S", 1.0, 50000, 80))
    o = c.db.one("SELECT * FROM gmgn_orders WHERE id=?", (r["id"],))
    assert r["status"] == "paper" and o["mode"] == "PAPER" and o["a_kind"] == "small" and o["size_usd"] == 5.0
    assert not swaps(t)
    clock.t += 5
    ch.add(tx("b1", 100 * L, 99 * L, 0, 10), clock.t)
    run(w.poll_once())
    o = c.db.one("SELECT * FROM gmgn_orders WHERE a_kind='copy'")
    assert o["mode"] == "LIVE" and t._trail_rows() == []
    seen = []

    async def hook(o, *a):
        seen.append(o["id"])
    t.copy_hook = hook
    c.db.x("UPDATE gmgn_orders SET last_check=0")
    run(t.watch_once(pairs=[c.net.pairs[MT]]))
    assert seen == [o["id"]]                                         # LIVE copy rows never go through _live_tick
    assert _a_label(c.s, o) == "LIVE · copying 9s4ji"


# ---------- exits: only his sells + -50% backstop
def test_copy_rows_skip_scanner_exits_keep_backstop(monkeypatch):
    c, clock, t, w, ch, _ = hybrid(monkeypatch, **{"risk.stop_pct": 6})
    run(w.poll_once())
    ch.add(tx("b1", 100 * L, 90 * L, 0, 10), clock.t)
    run(w.poll_once())
    o = c.db.one("SELECT * FROM gmgn_orders WHERE a_kind='copy'")
    for px in (0.03, 0.012, 0.0055):                               # +200%, trail-sized drop, -45%: all held
        clock.t += 4 * 3600                                         # past A's 3h timeout too
        run(t.watch_once(pairs=[pair(MT, price=px, liq=200000)]))
        assert c.db.one("SELECT status FROM gmgn_orders WHERE id=?", (o["id"],))["status"] == "open"
    assert run(t.surge_once(pairs=[pair(MT, price=0.05, liq=200000)])) == 0   # no surge on copies
    run(t.watch_once(pairs=[pair(MT, price=0.0049, liq=200000)]))
    o = c.db.one("SELECT * FROM gmgn_orders WHERE id=?", (o["id"],))
    assert o["status"] == "closed" and o["exit_reason"] == "copy stop"


# ---------- normal mode: small $5 scanner trades next to the copies
def test_hybrid_small_scanner_trades(monkeypatch):
    c, clock, t, w, ch, calls = hybrid(monkeypatch)
    run(w.poll_once())
    ch.add(tx("b1", 100 * L, 90 * L, 0, 10), clock.t)
    run(w.poll_once())                                              # one copy open: does not take a small slot
    mints = [f"Sm{i:02d}" + "1" * 36 + "pump" for i in range(3)]
    res = [run(t.on_pass(m, f"S{i}", "S", 1.0, 50000, 80)) for i, m in enumerate(mints)]
    assert [r["status"] for r in res] == ["paper", "paper", "refused"]
    assert "2/2" in res[2]["reason"]
    small = c.db.rows("SELECT * FROM gmgn_orders WHERE a_kind='small' AND status='open'")
    assert len(small) == 2 and all(o["size_usd"] == 5.0 for o in small)
    assert small_open_count(c.db) == 2
    assert sum(1 for x in calls if x[0] == copytrade.USDC) >= 3     # Jupiter check on the copy and each small buy


def test_hybrid_small_unsellable_skipped(monkeypatch):
    c, clock, t, w, ch, _ = hybrid(monkeypatch, fail="sell")
    r = run(t.on_pass("Sm99" + "1" * 36 + "pump", "S", "S", 1.0, 50000, 80))
    assert r["status"] == "refused" and "unsellable" in r["reason"]


def test_copy_profile_no_scanner_and_scanner_profile_unchanged(monkeypatch):
    c, clock, t, w, ch, _ = hybrid(monkeypatch, **{"gmgn.profile": "copy"})
    assert run(t.on_pass("Sm98" + "1" * 36 + "pump", "S", "S", 1.0, 50000, 80)) is None
    c.s.set("gmgn.profile", "scanner", source="test", force=True)
    r = run(t.on_pass("Sm97" + "1" * 36 + "pump", "S", "S", 1.0, 50000, 80))
    o = c.db.one("SELECT * FROM gmgn_orders WHERE id=?", (r["id"],))
    assert o["size_usd"] == 500.0 and o["a_kind"] is None


# ---------- app
def test_publisher_labels_and_status(monkeypatch):
    c, clock, t, w, ch, _ = hybrid(monkeypatch)
    run(w.poll_once())
    ch.add(tx("b1", 100 * L, 90 * L, 0, 10), clock.t)
    run(w.poll_once())
    run(t.on_pass("Sm01" + "1" * 36 + "pump", "S", "S", 1.0, 50000, 80))
    b = Builder(DeskStub(c, t))
    g = b.gmgn()
    assert g["copy"]["state"] == "full copy" and g["copy"]["name"] == "himOthy" and g["copy"]["paperOnly"]
    labels = sorted(p["label"] for p in g["open"])
    assert labels == ["PAPER · copying himOthy", "PAPER · small"]
    feed = b.strat_feed()["A_paper"]
    assert {e.get("label") for e in feed} >= {"PAPER · copying himOthy", "PAPER · small"}
    tr = b.trades()
    assert {r.get("label") for r in tr["paper"]["open"]} >= {"PAPER · copying himOthy", "PAPER · small"}
    assert _a_label(c.s, {"mode": "LIVE"}) == "LIVE"
    blob = json.dumps(g)
    assert "copy_src" not in blob and "sell_check" not in blob


def test_app_shows_copy_labels_and_mode_line():
    js = open("web/app.js").read()
    assert "function aCopyStatus" in js and "full copy" in js and 'r.label || "PAPER"' in js


def test_jupiter_not_listed_falls_back_to_dex_sells(monkeypatch):
    """Live 2026-11-19 5:11 PM CT: his FIONA buy (pump AMM, 0.9% tax) got TOKEN_NOT_TRADABLE from Jupiter."""
    c, clock, t, w, ch, _ = hybrid(monkeypatch, fail="nottradable")
    run(w.poll_once())
    ch.add(tx("b1", 100 * L, 90 * L, 0, 10), clock.t)
    run(w.poll_once())
    o = c.db.one("SELECT * FROM gmgn_orders")
    assert o["status"] == "open" and "DexScreener shows real sells" in o["quote"]
    # few real sells -> still skipped
    c.net.pairs[M2] = pair(M2, price=0.002, liq=100000, name="Mx", symbol="MX")
    c.net.pairs[M2]["txns"]["h1"]["sells"] = 3
    ch.add(tx("b2", 90 * L, 80 * L, 0, 10, mint=M2), clock.t)
    run(w.poll_once())
    o2 = c.db.one("SELECT * FROM gmgn_orders WHERE mint=?", (M2,))
    assert o2["status"] == "refused" and "too few real sells" in o2["reason"]


# ---------- 9s4ji rules (owner 5:17 PM CT): flat $20, first buy of each coin only, sells by fraction
def test_flat_20_first_buy_only(monkeypatch):
    c, clock, t, w, ch, _ = hybrid(monkeypatch, **{"copy.sizing": "flat", "copy.first_buy_only": True, "copy.name": "9s4ji"})
    assert c.s["copy.flat_usd"] == 20.0
    run(w.poll_once())
    ch.add(tx("b1", 1251 * L, 1250 * L, 0, 10), clock.t)           # tiny fraction of 1,251 SOL -> still $20
    run(w.poll_once())
    o = c.db.one("SELECT * FROM gmgn_orders WHERE a_kind='copy'")
    assert o["size_usd"] == 20.0 and o["status"] == "open"
    ch.add(tx("b2", 1250 * L, 1249 * L, 10, 20), clock.t)          # add-on buy: ignored
    run(w.poll_once())
    assert c.db.one("SELECT COUNT(*) n, SUM(size_usd) u FROM gmgn_orders WHERE a_kind='copy'")["u"] == 20.0
    ch.add(tx("s1", 1249 * L, 1250 * L, 20, 5), clock.t)           # sells 75% of his 20 -> we sell 75%
    run(w.poll_once())
    assert c.db.one("SELECT size_usd FROM gmgn_orders WHERE id=?", (o["id"],))["size_usd"] == 5.0
    ch.add(tx("s2", 1250 * L, 1251 * L, 5, 0), clock.t)            # full exit -> 100%
    run(w.poll_once())
    assert c.db.one("SELECT status FROM gmgn_orders WHERE id=?", (o["id"],))["status"] == "closed"
    ch.add(tx("b3", 1251 * L, 1250 * L, 0, 10), clock.t)           # re-buy of a coin already copied: ignored
    ch.add(tx("b4", 1250 * L, 1249 * L, 3, 10, mint=M2), clock.t)  # coin he already held: ignored
    run(w.poll_once())
    assert c.db.one("SELECT COUNT(*) n FROM gmgn_orders WHERE a_kind='copy' AND status='open'")["n"] == 0
    assert copytrade.label(c.s) == "PAPER · copying 9s4ji"


def test_wallet_swap_rebaselines(monkeypatch):
    c, clock, t, w, ch, _ = hybrid(monkeypatch)
    run(w.poll_once())
    ch.add(tx("b1", 100 * L, 90 * L, 0, 10), clock.t)
    c.s.set("copy.wallet", "9s4ji7MRh6xx9PVx7D4by8YjpHMfPVtHfYuMUy8VaTTp", source="test")
    assert run(w.poll_once()) == "baseline"
    assert c.db.one("SELECT COUNT(*) n FROM gmgn_orders")["n"] == 0
