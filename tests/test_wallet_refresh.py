"""DECISION 73 (owner 2026-11-17 7:06 PM CT): the wallet read, and so A's / B's 50/50 budgets, follow the real wallet.
Re-read at least every 30 s, right after any LIVE fill / exit, and fresh before every LIVE buy."""
import asyncio, time
import gmgn
from agents.gmgn_trader import GmgnTrader, budget_view
from publisher import Publisher, Builder
from tests.helpers import make_ctx, run
from tests.test_gmgn import FakeClient
from tests.test_publisher_gmgn import DeskStub


def _setup(wallet_sol=2.0):
    c, clock = make_ctx(**{"gmgn.mode": "LIVE", "gmgn.live_wallet_share_pct": 50.0, "strat_b.live_wallet_share_pct": 50.0})
    cl = FakeClient(wallet_sol=wallet_sol)
    t = GmgnTrader(c, cl)
    c.gmgn = t
    d = DeskStub(c, t)
    return d, c, cl, t


def test_fill_or_exit_marks_wallet_stale_and_refresh_rereads():
    d, c, cl, t = _setup()
    run(t.refresh_wallet(min_age=45))
    assert t._bal[1] == 2 * gmgn.LAMPORTS and not t._bal_stale
    cl.wallet_sol = 1.0                                       # a buy spent SOL
    run(t.refresh_wallet(min_age=45))
    assert t._bal[1] == 2 * gmgn.LAMPORTS                     # still fresh: no re-read
    c.event("fill", "X")
    assert t._bal_stale
    run(t.refresh_wallet(min_age=45))
    assert t._bal[1] == 1 * gmgn.LAMPORTS and not t._bal_stale
    cl.wallet_sol = 1.5
    c.event("exit", "X")
    run(t.refresh_wallet(min_age=45))
    assert t._bal[1] == int(1.5 * gmgn.LAMPORTS)


def test_budgets_follow_the_new_wallet_value():
    d, c, cl, t = _setup(2.0)
    run(t.refresh_wallet(min_age=45))
    a1 = budget_view(c.s, c.db, "A", t._bal[1], 100.0)["budgetSol"]
    cl.wallet_sol = 4.0
    c.event("exit", "X")
    run(t.refresh_wallet(min_age=45))
    a2 = budget_view(c.s, c.db, "A", t._bal[1], 100.0)["budgetSol"]
    assert a2 > a1 and abs(a2 - (4.0 - c.s["gmgn.sol_reserve"]) / 2) < 1e-6
    st = Builder(d).build()["state.json"]["gmgn"]
    assert st["sol"] == 4.0 and abs(st["budgets"]["B"]["budgetSol"] - a2) < 1e-6


def test_publisher_idle_refresh_publishes_on_change():
    d, c, cl, t = _setup(2.0)
    p = Publisher(d)

    async def go():
        assert await p.idle_wallet() is True                 # first read
        p.dirty.clear()
        assert await p.idle_wallet() is False                # fresh: nothing to do
        cl.wallet_sol = 3.0
        t._bal = (time.time() - 46, t._bal[1])               # older than WALLET_REFRESH_S
        assert await p.idle_wallet() is True and p.dirty.is_set()
        assert t._bal[1] == 3 * gmgn.LAMPORTS
    run(go())


def test_live_buy_sizing_reads_the_wallet_fresh():
    """Before every LIVE buy A and B call wallet_lamports(fresh=True) (source check, behaviour unchanged)."""
    import inspect, strat_b_live
    import agents.gmgn_trader as gt
    assert inspect.getsource(gt).count("wallet_lamports(fresh=True)") >= 3
    assert "wallet_lamports(fresh=True)" in inspect.getsource(strat_b_live)
