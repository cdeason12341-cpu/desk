"""DECISION 44/45 (owner 2026-11-15 8:33 + 8:40 PM CT): LIVE GMGN buys are $50 when free SOL covers it, else $20, else the
largest amount free SOL covers down to a $1 floor, else skipped;
at most 4 GMGN positions at once (pending + open + exiting). FOMO paper sizing is unchanged."""
import asyncio
import pytest
import config, gmgn
from agents.executor import size_ticket
from agents.gmgn_trader import pick_size, free_lamports, buy_cost_lamports, committed_lamports, open_count, max_affordable_lamports
from tests.helpers import make_ctx, run
from tests.test_gmgn import FakeClient, trader, go_live

L = gmgn.LAMPORTS
M = "Adpt111111111111111111111111111111111pump"


async def _nosleep(*a, **k):
    return None


def _swaps(fc):
    return [b for k, b in fc.calls if k == "swap"]


def test_settings_defaults_and_protected():
    c, _ = make_ctx()
    assert c.s["gmgn.cap_trade_usd"] == 50.0 and c.s["gmgn.step_trade_usd"] == 0.0 and c.s["gmgn.fallback_trade_usd"] == 20.0 and c.s["gmgn.min_trade_usd"] == 1.0 and c.s["gmgn.max_open"] == 4
    for k in ("min trade", "max open", "cap trade", "fallback trade", "step trade"):
        with pytest.raises(config.SettingError):
            c.s.set(k, "30")
    c.s.set("gmgn.max_open", 5, source="test", force=True)
    assert c.s["gmgn.max_open"] == 5 and "gmgn.min_trade_usd" in config.PROTECTED


def test_pick_size_tiers_fees_reserve_and_pool():
    c, _ = make_ctx()
    s = c.s
    need50 = buy_cost_lamports(s, int(50 / 70 * L))
    need20 = buy_cost_lamports(s, int(20 / 70 * L))
    assert need50 > int(50 / 70 * L) * 1.01                         # 1% GMGN fee + priority/tip + rent on top
    assert pick_size(s, need50, 70)[:2] == (50.0, int(50 / 70 * L))
    assert pick_size(s, need50 - 1, 70)[0] == 20.0
    assert pick_size(s, need20, 70)[0] == 20.0
    # under $20: the largest amount free SOL covers (cents, rounded down), full cost still fits
    size, lam, why = pick_size(s, need20 - 1, 70)
    assert why is None and 19.0 < size < 20.0 and buy_cost_lamports(s, lam) <= need20 - 1
    free = int(0.05 * L)                                            # 0.05 SOL free -> about $2.4 at $70 after fees + rent
    size, lam, _ = pick_size(s, free, 70)
    assert size == round(size, 2) and buy_cost_lamports(s, lam) <= free
    assert buy_cost_lamports(s, int((size + 0.02) / 70 * L)) > free  # a few cents more would not fit
    assert max_affordable_lamports(s, free) >= lam
    need1 = buy_cost_lamports(s, int(1 / 70 * L))
    assert pick_size(s, need1, 70)[0] == 1.0                         # exactly the $1 floor
    size, lam, why = pick_size(s, need1 - 1, 70)
    assert size is None and "can't cover even $1.00" in why and "reserve" in why
    assert pick_size(s, 10 * L, 70, pool_cap_usd=30)[0] == 20.0     # thin pool: top size excluded
    assert pick_size(s, 10 * L, 70, pool_cap_usd=10)[0] == 10.0     # pool cap 10: buy 10
    assert pick_size(s, 10 * L, 70, pool_cap_usd=0.5)[0] is None
    assert free_lamports(s, 1 * L, int(0.3 * L)) == int(0.68 * L)   # 1 SOL - 0.02 reserve - 0.3 in flight


def test_live_buys_50_when_wallet_covers_it_and_push_shows_open_count(monkeypatch):
    monkeypatch.setattr(asyncio, "sleep", _nosleep)
    fc = FakeClient(plan_blocked=False, strategy_id="st1", wallet_sol=1.43)
    c, _, t = trader(fc)
    go_live(c)
    r = run(t.on_pass(M, "Gm", "GM", 1.0, 50000, 80))
    assert r["status"] == "open"
    assert int(_swaps(fc)[0]["input_amount"]) == int(50 / 70 * L)
    o = c.db.one("SELECT * FROM gmgn_orders")
    assert o["size_usd"] == 50.0                                    # not the FOMO 47.50 min ticket
    texts = [m["text"] for m in c.notifier.sent]
    assert any(x.startswith("GMGN · Buying Gm · $50.00 (0.7143 SOL) · 1/4 open") for x in texts)
    assert any(x.startswith("GMGN · Bought Gm · $50.00 · 1/4 open") for x in texts)


def test_live_falls_back_to_20_then_skips(monkeypatch):
    monkeypatch.setattr(asyncio, "sleep", _nosleep)
    fc = FakeClient(plan_blocked=False, strategy_id="st1", wallet_sol=0.5)       # $35: $50 doesn't fit, $20 does
    c, _, t = trader(fc)
    go_live(c)
    assert run(t.on_pass(M, "Gm", "GM", 1.0, 50000, 80))["status"] == "open"
    assert int(_swaps(fc)[0]["input_amount"]) == int(20 / 70 * L)
    assert c.db.one("SELECT size_usd FROM gmgn_orders")["size_usd"] == 20.0
    fc2 = FakeClient(plan_blocked=False, strategy_id="st1", wallet_sol=0.039)  # tonight's 8:01 PM wallet: buys what it covers
    c2, _, t2 = trader(fc2)
    go_live(c2)
    assert run(t2.on_pass(M, "Gm", "GM", 1.0, 50000, 80))["status"] == "open"
    o = c2.db.one("SELECT size_usd, lamports FROM gmgn_orders")
    assert 0.5 < o["size_usd"] < 1.2 and int(_swaps(fc2)[0]["input_amount"]) == o["lamports"]
    assert buy_cost_lamports(c2.s, o["lamports"]) <= int(0.039 * L) - int(0.02 * L)
    fc3 = FakeClient(plan_blocked=False, wallet_sol=0.022)                     # under the $1 floor: skipped
    c3, _, t3 = trader(fc3)
    go_live(c3)
    r = run(t3.on_pass(M, "Gm", "GM", 1.0, 50000, 80))
    assert r["status"] == "refused" and "can't cover even $1.00" in r["reason"] and not _swaps(fc3)


def test_in_flight_buys_are_committed(monkeypatch):
    monkeypatch.setattr(asyncio, "sleep", _nosleep)
    fc = FakeClient(plan_blocked=False, strategy_id="st1", wallet_sol=1.0)
    c, _, t = trader(fc)
    go_live(c)
    t._row(ts=1, night=c.night, mode="LIVE", mint="Pend", status="pending", size_usd=50, lamports=int(0.6 * L))
    assert committed_lamports(c.db, "LIVE") == int(0.6 * L)
    run(t.on_pass(M, "Gm", "GM", 1.0, 50000, 80))
    assert int(_swaps(fc)[0]["input_amount"]) == int(20 / 70 * L)  # 1.0 - 0.02 - 0.6 = 0.38 free: $20 tier


def test_fifth_open_position_refused(monkeypatch):
    monkeypatch.setattr(asyncio, "sleep", _nosleep)
    fc = FakeClient(plan_blocked=False, strategy_id="st1", wallet_sol=10.0)
    c, _, t = trader(fc)
    go_live(c)
    c.s.set("gmgn.max_tickets_night", 50, source="test", force=True)              # tonight's setting
    for i, st in enumerate(("pending", "open", "open", "exiting")):
        t._row(ts=1, night=c.night, mode="LIVE", mint=f"m{i}", status=st, size_usd=50, lamports=int(0.7 * L))
    t._row(ts=1, night=c.night, mode="LIVE", mint="done", status="closed", size_usd=50)        # closed doesn't count
    t._row(ts=1, night=c.night, mode="PAPER", mint="pp", status="open", size_usd=50)          # other mode doesn't count
    assert open_count(c.db, "LIVE") == 4
    r = run(t.on_pass(M, "Gm", "GM", 1.0, 50000, 80))
    assert r == {"status": "refused", "reason": "4/4 GMGN positions already open (max 4 at once)"}
    assert not _swaps(fc)
    assert c.notifier.sent[-1]["text"] == "GMGN · Refused Gm: 4/4 GMGN positions already open (max 4 at once)"
    c.db.x("UPDATE gmgn_orders SET status='closed' WHERE mint='m1'")                          # one closes: room again
    assert run(t.on_pass(M[:-5] + "Bpump", "Gb", "GB", 1.0, 50000, 80))["status"] == "open"


def test_status_line_and_fomo_sizing_unchanged():
    c, _, t = trader(FakeClient(wallet_sol=1.43))
    go_live(c)
    run(t.wallet_lamports(fresh=True)); run(t.sol_usd())
    line = t.status_line()
    assert "size $50, then $20, then whatever free SOL covers down to $1.00" in line and "next $50.00" in line and "0/4 open" in line
    sz = size_ticket(c.s, 100.0, 100.0, 50000)                     # FOMO paper: still the $47.50 min-ticket bump
    assert sz["status"] == "ok" and sz["size"] == 47.5
