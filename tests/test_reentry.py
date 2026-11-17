"""DECISION 46: re-entry after a LIVE GMGN exit (owner 2026-11-15 8:52 PM CT; bounce +7% at 8:53 PM CT)."""
import asyncio
import reentry
from reentry import ReEntry, evaluate
from agents.gmgn_trader import GmgnTrader
from agents.analyst import Analyst
from tests.helpers import make_ctx, run, pair
from tests.test_gmgn_chain_close import SameClient, M, TOK, BUY_SOL, SELL_SOL, SOL_USD
from tests.chainsim import ChainSim

_orig_sleep = asyncio.sleep


async def _yield(*a, **k):
    await _orig_sleep(0)


class Stub:
    def __init__(self, result):
        self.result, self.calls = result, []

    async def run(self, *a, **k):
        self.calls.append((a, k))
        return self.result


class Desk:
    def __init__(self, c, gmgn, audit=True, score=55):
        self.ctx, self.gmgn = c, gmgn
        self.auditor = Stub({"passed": audit, "reason": None if audit else "red: mint authority"})
        self.analyst = Stub({"passed": score >= 40, "score": score, "failed": [] if score >= 40 else ["score under 40"],
                             "_priceUsd": None})


def _setup(audit=True, score=55):
    c, clock = make_ctx(**{"risk.stop_pct": 15, "risk.trail_pct": 20, "gmgn.watch_s": 15, "scanner.min_liquidity_usd": 5000})
    c.s.set("gmgn.mode", "LIVE", source="test", force=True)
    fc = SameClient()
    t = GmgnTrader(c, fc)
    c.gmgn = t
    d = Desk(c, t, audit, score)
    re = ReEntry(d)
    c.reentry = re
    ch = ChainSim(c.net, M)
    ch.add("CEd1TApS", int(clock.t), BUY_SOL, TOK)
    ch.balance = TOK
    return c, clock, t, fc, ch, d, re


def _buy_and_sell(c, clock, t, ch):
    t0 = clock.t
    assert run(t.on_pass(M, "ni hao cat", "CAT", 3.0e-05, 12600, 55))["status"] == "open"
    c.db.x("UPDATE gmgn_orders SET last_price=?, last_liq=? WHERE mint=?", (3.0e-05, 12600, M))
    ch.add("SELLtx", int(t0 + 60), SELL_SOL, -TOK)
    ch.balance = 0
    clock.t = t0 + 70
    run(t.watch_once(pairs=[], strategies={}))
    assert c.db.one("SELECT status FROM gmgn_orders WHERE id=1")["status"] == "closed"
    return t0


def _w(c):
    return c.db.one("SELECT * FROM reentry_watch ORDER BY id DESC LIMIT 1")


def _poll(c, re, price, liq=12600, buys=20, sells=8):
    c.net.pairs[M] = pair(M, price=price, liq=liq, buys_m5=buys, sells_m5=sells, name="ni hao cat", symbol="CAT")

    async def go():
        n = await re.poll_once()
        await asyncio.gather(*list(re._tasks))
        return n
    return run(go())


def test_defaults_and_bounce_is_7():
    c, _ = make_ctx()
    assert c.s["gmgn.reentry_enabled"] is True and c.s["gmgn.reentry_bounce_pct"] == 7
    assert c.s["gmgn.reentry_watch_min"] == 60 and c.s["gmgn.reentry_max_per_coin"] == 1 and c.s["gmgn.reentry_poll_s"] == 15


def test_evaluate_rules():
    c, _ = make_ctx(**{"scanner.min_liquidity_usd": 5000})
    w = {"low": 1.0, "sell_liq": 20000, "expires": 1000}
    assert evaluate(c.s, w, 1.069, 20000, 30, 10, 0)[0] == "watch"           # +6.9% < 7
    v, note = evaluate(c.s, w, 1.07, 20000, 30, 10, 0)
    assert v == "trigger" and "+7.0%" in note
    assert evaluate(c.s, w, 1.2, 20000, 10, 10, 0)[0] == "watch"             # buys must beat sells
    assert evaluate(c.s, w, 1.2, 9999, 30, 10, 0)[0] == "drop"               # liq down > 50%
    assert evaluate(c.s, w, 1.2, 4000, 30, 10, 0)[0] == "drop"               # under scanner floor
    assert evaluate(c.s, w, 1.2, 20000, 30, 10, 1000)[0] == "expire"


def test_live_close_arms_watch_for_60_min(monkeypatch):
    monkeypatch.setattr(asyncio, "sleep", _yield)
    c, clock, t, fc, ch, d, re = _setup()
    t0 = _buy_and_sell(c, clock, t, ch)
    w = _w(c)
    assert w["status"] == "watching" and w["order_id"] == 1 and w["low"] == 3.0e-05 and w["sell_liq"] == 12600
    assert w["sold_at"] == int(t0 + 60) and w["expires"] == w["sold_at"] + 3600
    assert re.arm(c.db.one("SELECT * FROM gmgn_orders WHERE id=1")) is None      # idempotent


def test_low_tracks_then_bounce_rebuys_with_reentry_label(monkeypatch):
    monkeypatch.setattr(asyncio, "sleep", _yield)
    c, clock, t, fc, ch, d, re = _setup()
    _buy_and_sell(c, clock, t, ch)
    clock.t += 15
    _poll(c, re, 2.5e-05, buys=3, sells=9)
    assert _w(c)["low"] == 2.5e-05 and _w(c)["status"] == "watching"
    clock.t += 15
    _poll(c, re, 2.6e-05)                                   # +4%: not yet
    assert _w(c)["status"] == "watching" and not d.auditor.calls
    ch.balance = TOK
    clock.t += 15
    _poll(c, re, 2.7e-05)                                   # +8% and m5 buys 20 > sells 8
    assert d.auditor.calls[0][1] == {"reentry": True} and d.analyst.calls[0][1] == {"reentry": True}
    o = c.db.one("SELECT * FROM gmgn_orders WHERE id=2")
    assert o["status"] == "open" and o["reason"] == "re-entry" and o["size_usd"] == 50.0
    w = _w(c)
    assert w["status"] == "bought" and "+8.0%" in w["note"]
    gm = [m["text"] for m in c.notifier.sent if m["text"].startswith("GMGN · RE-ENTRY")]
    assert gm[0].startswith("GMGN · RE-ENTRY · Buying ni hao cat · $50.00 (") and "bounced +8% off post-sell low · 1/4 open" in gm[0]
    assert any(x.startswith("GMGN · RE-ENTRY · Bought ni hao cat") for x in gm)


def test_max_one_reentry_per_coin_per_night(monkeypatch):
    monkeypatch.setattr(asyncio, "sleep", _yield)
    c, clock, t, fc, ch, d, re = _setup()
    _buy_and_sell(c, clock, t, ch)
    c.db.x("INSERT INTO gmgn_orders(ts,night,mode,mint,name,status,reason,exit_reason) VALUES(?,?,?,?,?,?,?,?)",
           (clock.t, c.night, "LIVE", M, "ni hao cat", "closed", "re-entry", "server-side stop"))
    o = c.db.one("SELECT * FROM gmgn_orders WHERE id=2")
    assert re.arm(o) is None                                 # 1/1 used: no new watch
    r = run(t.on_pass(M, "ni hao cat", "CAT", 3e-05, 12600, 55, reentry={"bouncePct": 9}))
    assert r["status"] == "refused" and "re-entry limit" in r["reason"]


def test_rejected_or_liquidity_drop_ends_watch(monkeypatch):
    monkeypatch.setattr(asyncio, "sleep", _yield)
    c, clock, t, fc, ch, d, re = _setup(score=30)
    _buy_and_sell(c, clock, t, ch)
    clock.t += 15
    _poll(c, re, 3.3e-05)
    assert _w(c)["status"] == "rejected" and "analyst" in _w(c)["note"]
    assert c.db.one("SELECT COUNT(*) n FROM gmgn_orders")["n"] == 1      # no buy
    clock.t += 15
    _poll(c, re, 4e-05)                                     # one trigger per watch
    assert len(d.auditor.calls) == 1
    c2, clock2, t2, fc2, ch2, d2, re2 = _setup()
    _buy_and_sell(c2, clock2, t2, ch2)
    clock2.t += 15
    _poll(c2, re2, 3.3e-05, liq=6000)                       # -52% liquidity since the sell
    assert _w(c2)["status"] == "dropped" and "liquidity down" in _w(c2)["note"] and not d2.auditor.calls


def test_expires_after_watch_window(monkeypatch):
    monkeypatch.setattr(asyncio, "sleep", _yield)
    c, clock, t, fc, ch, d, re = _setup()
    _buy_and_sell(c, clock, t, ch)
    clock.t = _w(c)["expires"] + 1
    _poll(c, re, 9e-05)
    assert _w(c)["status"] == "expired" and not d.auditor.calls


def test_disabled_does_not_arm(monkeypatch):
    monkeypatch.setattr(asyncio, "sleep", _yield)
    c, clock, t, fc, ch, d, re = _setup()
    c.s.set("gmgn.reentry_enabled", False, source="test")
    _buy_and_sell(c, clock, t, ch)
    assert _w(c) is None


def test_analyst_reentry_skips_per_night_guard():
    c, clock = make_ctx(**{"analyst.mode": "rules"})
    a = Analyst(c)
    p = pair(M, price=3e-05, liq=12600)
    c.net.pairs[M] = p
    scan = {"mint": M, "name": "ni hao cat", "symbol": "CAT", "liquidityUsd": 12600, "top10Pct": 20, "ageMin": 30}
    audit = {"passed": True, "bundled": False, "holders": 300}
    assert run(a.run(scan, audit, pair=p)) is not None
    assert run(a.run(scan, audit, pair=p)) is None                       # normal path: once per night
    assert run(a.run(scan, audit, pair=p, reentry=True)) is not None     # re-entry: fresh decision


def test_feed_shape(monkeypatch):
    monkeypatch.setattr(asyncio, "sleep", _yield)
    c, clock, t, fc, ch, d, re = _setup()
    _buy_and_sell(c, clock, t, ch)
    clock.t += 15
    _poll(c, re, 2.9e-05, buys=2, sells=5)
    f = re.feed()[0]
    assert f["status"] == "watching" and f["needPct"] == 7 and f["low"] == 2.9e-05 and f["minLeft"] > 50
