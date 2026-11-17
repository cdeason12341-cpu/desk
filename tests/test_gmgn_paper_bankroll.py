"""DECISION 68 (owner 2026-11-17 10:20 AM CT): Strategy A PAPER uses a $2500 simulated bankroll with $500 buys.
Never limited by the real wallet or gmgn.cap_trade_usd. Strategy B LIVE sizing and slots are unchanged."""
import config, gmgn
from agents.gmgn_trader import (
    pick_size, pick_paper_size, paper_free_usd, paper_snapshot, paper_since_ts,
    check_caps, check_paper_caps, CapError, open_count, night_usage, committed_lamports,
    PAPER_BANKROLL_META, LAMPORTS,
)
from publisher import Builder
from tests.helpers import make_ctx, run
from tests.test_gmgn import FakeClient, trader, go_live
from tests.test_publisher_gmgn import DeskStub

L = LAMPORTS
M = "Adpt111111111111111111111111111111111pump"


def _mint(i):
    return f"Pb{i:02d}" + "1" * 36 + "pump"


def test_settings_defaults_not_protected():
    c, _ = make_ctx()
    assert c.s["gmgn.paper_bankroll_usd"] == 2500.0 and c.s["gmgn.paper_trade_usd"] == 500.0
    assert c.s["gmgn.cap_trade_usd"] == 50.0
    assert "gmgn.paper_bankroll_usd" not in config.PROTECTED
    assert "gmgn.paper_trade_usd" not in config.PROTECTED
    c.s.set("gmgn.paper_bankroll_usd", 3000, source="test")
    c.s.set("paper trade size", 400, source="test")
    assert c.s["gmgn.paper_bankroll_usd"] == 3000.0 and c.s["gmgn.paper_trade_usd"] == 400.0


def test_pick_paper_size_full_partial_refuse():
    c, _ = make_ctx()
    s = c.s
    assert pick_paper_size(s, 2500, 70)[:2] == (500.0, int(500 / 70 * L))
    assert pick_paper_size(s, 500, 70)[0] == 500.0
    size, lam, why = pick_paper_size(s, 200, 70)
    assert why is None and size == 200.0 and lam == int(200 / 70 * L)
    size, lam, why = pick_paper_size(s, 0.5, 70)
    assert size is None and "paper free $0.50 can't cover even $1.00" in why and "bankroll $2,500.00" in why
    assert pick_paper_size(s, 2500, 70, pool_cap_usd=100)[0] == 100.0
    assert pick_paper_size(s, 2500, 70, pool_cap_usd=0.5)[0] is None
    # live pick_size is unchanged and still caps at $50
    assert pick_size(s, 10 * L, 70)[0] == 50.0
    assert pick_size(s, 10 * L, 70, None, top_usd=500)[0] == 50.0


def test_paper_buys_500_with_empty_wallet(monkeypatch):
    fc = FakeClient(plan_blocked=False, wallet_sol=0.0)
    c, _, t = trader(fc)
    r = run(t.on_pass(M, "Gm", "GM", 1.0, 50000, 80))
    assert r["status"] == "paper"
    o = c.db.one("SELECT * FROM gmgn_orders")
    assert o["size_usd"] == 500.0 and o["mode"] == "PAPER" and o["status"] == "open"
    assert o["lamports"] == int(500 / 70 * L)
    assert "swap" not in fc.kinds()
    snap = paper_snapshot(c.s, c.db)
    assert snap["bankrollUsd"] == 2500.0 and snap["freeUsd"] == 2000.0 and snap["inPositionsUsd"] == 500.0
    assert snap["pnlUsd"] == 0.0
    assert "PAPER · GMGN · Buy Gm · $500.00" in c.notifier.sent[-1]["text"]


def test_paper_partial_then_refuse_when_bankroll_spent():
    fc = FakeClient(plan_blocked=False, wallet_sol=0.0)
    c, clock, t = trader(fc, **{"gmgn.paper_bankroll_usd": 700.0})
    assert run(t.on_pass(M, "Gm", "GM", 1.0, 50000, 80))["status"] == "paper"
    assert c.db.one("SELECT size_usd FROM gmgn_orders")["size_usd"] == 500.0
    assert paper_free_usd(c.s, c.db) == 200.0
    r = run(t.on_pass(_mint(2), "G2", "G2", 1.0, 50000, 80))
    assert r["status"] == "paper"
    o2 = c.db.one("SELECT size_usd FROM gmgn_orders WHERE mint=?", (_mint(2),))
    assert o2["size_usd"] == 200.0
    assert paper_free_usd(c.s, c.db) == 0.0
    r3 = run(t.on_pass(_mint(3), "G3", "G3", 1.0, 50000, 80))
    assert r3["status"] == "refused" and "paper free $0.00 can't cover even $1.00" in r3["reason"]
    assert c.db.one("SELECT COUNT(*) n FROM gmgn_orders WHERE status='open'")["n"] == 2


def test_old_paper_rows_before_baseline_do_not_count():
    fc = FakeClient(plan_blocked=False, wallet_sol=0.0)
    c, clock, t = trader(fc)
    # "last night" paper history: a big loss and an open ticket, both before the reset
    t._row(ts=clock.t - 10_000, night="old", mode="PAPER", mint=_mint(9), name="Old",
           status="closed", size_usd=500, pnl_usd=-2000, closed_at=clock.t - 9000)
    t._row(ts=clock.t - 8000, night="old", mode="PAPER", mint=_mint(8), name="Still",
           status="open", size_usd=500, lamports=int(500 / 70 * L), entry_price=1.0)
    c.db.set_meta(PAPER_BANKROLL_META, clock.t)
    assert paper_since_ts(c.db) == clock.t
    assert paper_free_usd(c.s, c.db) == 2500.0
    r = run(t.on_pass(M, "Gm", "GM", 1.0, 50000, 80))
    assert r["status"] == "paper"
    assert c.db.one("SELECT size_usd FROM gmgn_orders WHERE mint=?", (M,))["size_usd"] == 500.0
    assert paper_free_usd(c.s, c.db) == 2000.0          # only the new ticket counts


def test_paper_open_and_tickets_do_not_consume_b_live_slots():
    fc = FakeClient(plan_blocked=False, wallet_sol=0.0)
    c, clock, t = trader(fc)
    # four B LIVE positions would fill the live cap; A paper still has its own 4 slots
    for i in range(4):
        c.db.x("INSERT INTO strat_b_orders(ts,night,mode,mint,name,status,size_usd,lamports) VALUES(?,?,?,?,?,?,?,?)",
               (clock.t, c.night, "LIVE", _mint(20 + i), f"B{i}", "open", 50, int(50 / 70 * L)))
    assert open_count(c.db, "LIVE") == 4
    assert open_count(c.db, "PAPER") == 0
    r = run(t.on_pass(M, "Gm", "GM", 1.0, 50000, 80))
    assert r["status"] == "paper"
    assert open_count(c.db, "LIVE") == 4 and open_count(c.db, "PAPER") == 1
    assert night_usage(c.db, c.night, "LIVE")[0] == 4
    assert night_usage(c.db, c.night, "PAPER") == (1, 500.0)
    assert committed_lamports(c.db, "LIVE") == 0
    # A paper has its own 4 open slots; a 5th is refused. B's live count stays 4.
    for i in range(3):
        assert run(t.on_pass(_mint(i), f"G{i}", f"G{i}", 1.0, 50000, 80))["status"] == "paper"
    r5 = run(t.on_pass(_mint(4), "G4", "G4", 1.0, 50000, 80))
    assert r5["status"] == "refused" and "4/4 GMGN positions already open" in r5["reason"]
    # close one paper ticket: max_open has room, but the night ticket cap (4) still blocks
    c.db.x("UPDATE gmgn_orders SET status='closed', pnl_usd=0, closed_at=? WHERE mint=?", (clock.t, M))
    r6 = run(t.on_pass(_mint(5), "G5", "G5", 1.0, 50000, 80))
    assert r6["status"] == "refused" and "night ticket cap reached (4/4)" in r6["reason"]
    assert open_count(c.db, "LIVE") == 4 and night_usage(c.db, c.night, "LIVE")[0] == 4


def test_live_still_uses_wallet_and_50_cap():
    import pytest
    fc = FakeClient(plan_blocked=False, wallet_sol=0.0)
    c, _, t = trader(fc)
    go_live(c)
    r = run(t.on_pass(M, "Gm", "GM", 1.0, 50000, 80))
    assert r["status"] == "refused" and "can't cover even $1.00" in r["reason"]
    assert "paper free" not in r["reason"]
    with pytest.raises(CapError, match="per-trade cap"):
        check_caps(c.s, c.db, c.night, "LIVE", 500, int(500 / 70 * L), 70, 100 * L)
    check_paper_caps(c.s, c.db, c.night, 500)            # paper $500 is allowed


def test_status_line_and_publisher_show_paper_bankroll():
    fc = FakeClient(plan_blocked=False, wallet_sol=0.0)
    c, clock, t = trader(fc)
    c.db.set_meta(PAPER_BANKROLL_META, clock.t)
    line = t.status_line()
    assert "GMGN PAPER" in line
    assert "paper bankroll $2,500.00, free $2,500.00, P&L $+0.00" in line
    assert "size $500.00, then whatever paper free covers down to $1.00" in line
    assert "tonight 0/4 tickets" in line and "0/4 open" in line
    assert "cap_trade" not in line and "$50.00" not in line
    run(t.on_pass(M, "Gm", "GM", 1.0, 50000, 80))
    d = DeskStub(c, t)
    g = Builder(d).gmgn()
    assert g["paper"]["bankrollUsd"] == 2500.0 and g["paper"]["tradeUsd"] == 500.0
    assert g["paper"]["freeUsd"] == 2000.0 and g["paper"]["inPositionsUsd"] == 500.0
    assert g["paper"]["pnlUsd"] == 0.0 and g["paper"]["since"]
    assert g["capTradeUsd"] == 50.0                      # live cap unchanged in the payload
    st = Builder(d).state()
    assert st["gmgn"]["paper"]["freeUsd"] == 2000.0
    line2 = t.status_line()
    assert "free $2,000.00" in line2 and "1/4 open" in line2 and "tonight 1/4 tickets" in line2
