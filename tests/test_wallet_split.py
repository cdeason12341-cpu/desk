"""DECISION 71 (owner 2026-11-17 5:59 PM CT: "Split the wallet 50/50 and run both live").
Strategy A LIVE and Strategy B LIVE share one GMGN wallet; each sizes against its own share, has its own slots and
night tickets, never buys a coin the other holds, and never sells the other's position."""
import asyncio
import pytest
import gmgn, strat_b_live
from agents.gmgn_trader import (budget_lamports, budget_view, strategy_free_lamports, check_caps, CapError, pick_size,
                                buy_cost_lamports, mint_held_live, open_count, night_usage, LAMPORTS)
from tests.chainsim import ChainSim
from tests.helpers import make_ctx, run
from tests.test_gmgn import go_live
from tests.test_strat_b import M, M2, _orders, _pair
from tests.test_strat_b_live_mode import _live, _signal, _swaps, SOL_USD

MA = "SplA111111111111111111111111111111111pump"
SPLIT = {"gmgn.live_wallet_share_pct": 50, "strat_b.live_wallet_share_pct": 50}
L = LAMPORTS


async def _nosleep(*a, **k):
    return None


def _a_row(c, mint, sol=0.3, status="open", entry=1.0, **kw):
    cols = dict(ts=c.clock(), night=c.night, mode="LIVE", mint=mint, name="Aco", status=status, size_usd=sol * SOL_USD,
                sol_usd=SOL_USD, lamports=int(sol * L), entry_price=entry, peak=entry, last_price=entry, tx="atx", **kw)
    return c.db.x(f"INSERT INTO gmgn_orders({','.join(cols)}) VALUES({','.join('?' * len(cols))})", tuple(cols.values())).lastrowid


def _b_row(c, mint, sol=0.3, status="open", entry=1.0):
    cols = dict(ts=c.clock(), night=c.night, mode="LIVE", mint=mint, name="Bco", status=status, size_usd=sol * SOL_USD,
                sol_usd=SOL_USD, lamports=int(sol * L), entry_price=entry, peak=entry, last_price=entry, opened_at=c.clock(),
                tx="btx", entry_src="chain", tokens=1000.0, trail_armed=0, last_seen=c.clock())
    return c.db.x(f"INSERT INTO strat_b_orders({','.join(cols)}) VALUES({','.join('?' * len(cols))})", tuple(cols.values())).lastrowid


# ---------- budget formula
def test_settings_defaults_protected_and_whole_wallet_by_default():
    c, _ = make_ctx()
    s = c.s
    assert s["gmgn.live_wallet_share_pct"] == 100 and s["strat_b.live_wallet_share_pct"] == 100
    assert s["strat_b.live_max_open"] == 4 and s["strat_b.live_max_tickets_night"] == 4
    import config
    for k in ("gmgn.live_wallet_share_pct", "strat_b.live_wallet_share_pct", "strat_b.live_max_open", "strat_b.live_max_tickets_night"):
        assert k in config.PROTECTED
        with pytest.raises(config.SettingError):
            s.set(k, 10)
    free = int(1 * L) - int(0.02 * L)
    assert strategy_free_lamports(s, c.db, "A", 1 * L) == free == strategy_free_lamports(s, c.db, "B", 1 * L)   # 100% = old behaviour


def test_budget_split_50_50_with_open_positions():
    c, _ = make_ctx(**SPLIT)
    s, w = c.s, 1 * L
    free = w - int(0.02 * L)
    a = budget_lamports(s, c.db, "A", w)
    assert a["avail"] == free // 2 and a["total"] == free
    _b_row(c, M2, sol=0.3)                                                 # B holds 0.3 SOL of coin (wallet now 0.7)
    w2 = w - int(0.3 * L)
    a, b = budget_lamports(s, c.db, "A", w2), budget_lamports(s, c.db, "B", w2)
    total = (w2 - int(0.02 * L)) + int(0.3 * L)
    assert a["total"] == b["total"] == total
    assert a["avail"] == total // 2                                        # A's half untouched by B's position
    assert b["avail"] == total // 2 - int(0.3 * L) and b["open"] == int(0.3 * L)
    _b_row(c, "Big1111111111111111111111111111111111pump", sol=0.4)        # B over its half -> 0, never negative
    w3 = w2 - int(0.4 * L)
    assert budget_lamports(s, c.db, "B", w3)["avail"] == 0
    assert budget_lamports(s, c.db, "A", w3)["avail"] == min((w3 - int(0.02 * L) + int(0.7 * L)) // 2, w3 - int(0.02 * L))
    assert budget_lamports(s, c.db, "A", None) is None


def test_budget_never_above_free_sol_and_view():
    c, _ = make_ctx(**{"gmgn.live_wallet_share_pct": 100, "strat_b.live_wallet_share_pct": 100})
    _b_row(c, M2, sol=0.5)
    b = budget_lamports(c.s, c.db, "A", int(0.3 * L))
    assert b["avail"] == int(0.3 * L) - int(0.02 * L)                      # 100% of 0.78 total, capped at 0.28 free
    v = budget_view(c.s, c.db, "B", int(0.3 * L), SOL_USD)
    assert v["sharePct"] == 100 and v["openCount"] == 1 and v["inPositionsUsd"] == pytest.approx(35.0)
    assert set(v) >= {"budgetSol", "budgetUsd", "shareUsd", "maxOpen", "ticketsMax"}
    assert budget_view(c.s, c.db, "A", None, None)["budgetUsd"] is None


# ---------- sizing against the share
def test_sizing_uses_own_share():
    c, _ = make_ctx(**SPLIT)
    s = c.s
    w = int(0.02 * L) + int(100 / SOL_USD * L)                             # reserve + $100 -> $50 each
    av = strategy_free_lamports(s, c.db, "A", w)
    assert pick_size(s, av, SOL_USD)[0] == 20.0                            # $50 + fees doesn't fit in $50, $20 does
    w = int(0.02 * L) + 2 * buy_cost_lamports(s, int(50 / SOL_USD * L)) + 10
    assert pick_size(s, strategy_free_lamports(s, c.db, "B", w), SOL_USD, top_usd=50)[0] == 50.0
    w = int(0.02 * L) + int(6 / SOL_USD * L)                               # $3 each -> whatever covers it, $1 floor
    size = pick_size(s, strategy_free_lamports(s, c.db, "A", w), SOL_USD)[0]
    assert size is not None and 1 <= size < 3


def test_check_caps_refuses_over_share_and_counts_own_slots():
    c, _ = make_ctx(**SPLIT, **{"gmgn.max_open": 1})
    s = c.s
    w = int(0.02 * L) + int(80 / SOL_USD * L)                             # $40 each
    lam = int(40 / SOL_USD * L)
    with pytest.raises(CapError, match="50% share"):
        check_caps(s, c.db, c.night, "LIVE", 40, lam, SOL_USD, w, strategy="A")
    check_caps(s, c.db, c.night, "LIVE", 40, lam, SOL_USD, w)              # old global call: no share check
    lam = int(20 / SOL_USD * L)
    check_caps(s, c.db, c.night, "LIVE", 20, lam, SOL_USD, w, strategy="A")
    _b_row(c, M2, sol=0.01)                                                # B's open doesn't take A's only slot
    check_caps(s, c.db, c.night, "LIVE", 20, lam, SOL_USD, w, strategy="A")
    assert open_count(c.db, "LIVE", "A") == 0 and open_count(c.db, "LIVE", "B") == 1 and open_count(c.db, "LIVE") == 1
    _a_row(c, MA, sol=0.01)
    with pytest.raises(CapError, match="max 1 at once"):
        check_caps(s, c.db, c.night, "LIVE", 20, lam, SOL_USD, w, strategy="A")
    assert night_usage(c.db, c.night, "LIVE", "B")[0] == 1 and night_usage(c.db, c.night, "LIVE", "A")[0] == 1


def test_b_night_tickets_are_its_own():
    c, _ = make_ctx(**SPLIT, **{"strat_b.live_max_tickets_night": 1, "gmgn.max_tickets_night": 1})
    w = 5 * L
    lam = int(20 / SOL_USD * L)
    _a_row(c, MA, sol=0.01, status="closed")                               # A used its 1 ticket
    check_caps(c.s, c.db, c.night, "LIVE", 20, lam, SOL_USD, w, strategy="B")
    _b_row(c, M2, sol=0.01, status="closed")
    with pytest.raises(CapError, match="night ticket cap"):
        check_caps(c.s, c.db, c.night, "LIVE", 20, lam, SOL_USD, w, strategy="B")


# ---------- end to end: A LIVE + B LIVE on one wallet
def test_a_live_sizes_from_its_half(monkeypatch):
    monkeypatch.setattr(asyncio, "sleep", _nosleep)
    c, clock, sb, cl, chain = _live(wallet_sol=0.02 + 100 / SOL_USD, **SPLIT)
    go_live(c)
    cl.strategy_id = "st1"
    r = run(c.gmgn.on_pass(MA, "Aco", "AC", 1.0, 500000, 80))
    o = c.db.one("SELECT * FROM gmgn_orders WHERE mint=?", (MA,))
    assert o["mode"] == "LIVE" and o["size_usd"] == 20.0 and r["status"] in ("open", "pending")


def test_b_live_sizes_from_its_half_and_reports_budget():
    c, clock, sb, cl, chain = _live(wallet_sol=0.02 + 100 / SOL_USD, **SPLIT)
    _signal(c, clock, sb, chain, sol_paid=0.3)
    o = _orders(c)[0]
    assert o["mode"] == "LIVE" and o["size_usd"] == 20.0                   # $50 doesn't fit B's ~$50 half with fees
    card = strat_b_live.budget(c)
    assert card["sharePct"] == 50 and card["openCount"] == 1


def test_same_coin_blocked_both_ways(monkeypatch):
    monkeypatch.setattr(asyncio, "sleep", _nosleep)
    # A holds M -> B refuses it
    c, clock, sb, cl, chain = _live(wallet_sol=5.0, **SPLIT)
    _a_row(c, M, sol=0.3)
    assert mint_held_live(c.db, M) == "A"
    _signal(c, clock, sb, chain)
    rows = _orders(c)
    assert rows[0]["status"] == "refused" and "already held" in rows[0]["note"] and not _swaps(cl)
    # B holds MA -> A skips it
    c2, clock2, sb2, cl2, chain2 = _live(wallet_sol=5.0, **SPLIT)
    go_live(c2)
    _b_row(c2, MA, sol=0.3)
    assert mint_held_live(c2.db, MA) == "B"
    assert run(c2.gmgn.on_pass(MA, "Aco", "AC", 1.0, 500000, 80)) is None and not _swaps(cl2)
    # last line of defence: B takes the coin after A's entry checks but before A's signed swap
    c3, clock3, sb3, cl3, chain3 = _live(wallet_sol=5.0, **SPLIT)
    go_live(c3)
    t = c3.gmgn
    _b_row(c3, MA, sol=0.3)
    lam = int(20 / SOL_USD * L)
    from agents.gmgn_trader import buy_body, exits_text
    r = run(t.live_buy(MA, "Aco", "AC", 1.0, 500000, 20.0, SOL_USD, lam, buy_body(c3.s, MA, lam), exits_text(c3.s)))
    assert r["status"] == "refused" and "held by B" in r["reason"] and not _swaps(cl3)


def test_sell_isolation_each_strategy_sells_only_its_own_rows(monkeypatch):
    monkeypatch.setattr(asyncio, "sleep", _nosleep)
    c, clock, sb, cl, chain = _live(wallet_sol=5.0, **SPLIT)
    go_live(c)
    chain.balance = 10 ** 9
    aid = _a_row(c, MA, sol=0.3, exits_phase="normal")
    # B's exit loop at +200% on both coins: it must sell nothing (A's coin is not in strat_b_orders)
    run(sb.live.once(pairs=[_pair(MA, price=3.0), _pair(M, price=3.0)]))
    assert not _swaps(cl)
    bid = _b_row(c, M, sol=0.3)
    run(sb.live.once(pairs=[_pair(MA, price=3.0), _pair(M, price=3.0)]))
    sells = [b for b in _swaps(cl) if b["input_token"] != gmgn.SOL]
    assert [b["input_token"] for b in sells] == [M]                        # only B's own coin
    assert c.db.one("SELECT status FROM gmgn_orders WHERE id=?", (aid,))["status"] == "open"
    # A's exit watchers never pick up B rows
    c2, clock2, sb2, cl2, chain2 = _live(wallet_sol=5.0, **SPLIT)
    go_live(c2)
    chain2.balance = 10 ** 9
    _b_row(c2, M, sol=0.3, entry=1.0)
    t = c2.gmgn
    assert run(t.trail_once(pairs=[_pair(M, price=0.1)])) == 0
    run(t.surge_once(pairs=[_pair(M, price=5.0)]))
    run(t.watch_once(pairs=[_pair(M, price=0.1)]))
    assert not _swaps(cl2)
    assert c2.db.one("SELECT status FROM strat_b_orders WHERE mint=?", (M,))["status"] == "open"
