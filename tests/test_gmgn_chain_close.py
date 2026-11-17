"""DECISION 42: the 2026-11-15 'The Same Coin' LIVE order (gmgn_orders id 2). The desk closed it at 7:01:44 PM CT as a
+4.8% 'server' exit because GMGN wallet_holdings didn't show the mint (missing read as 0 held), 73s before GMGN's
server-side stop really sold it at 7:02:57 PM CT for a loss. LIVE closes now come only from the chain."""
import asyncio
import gmgn
from agents.gmgn_trader import GmgnTrader
from tests.helpers import make_ctx, run
from tests.test_gmgn import FakeClient
from tests.chainsim import ChainSim

M = "5qRxJ9W45zUrUtGGDneysiRBKZaceNriEqVUei1Rpump"
TOK = 1486846490944                         # 1,486,846.490944 SAME
BUY_SOL, FEE_SOL, SELL_SOL = -669872226, 901172, 576123734
SOL_USD = 71.91
_orig_sleep = asyncio.sleep


async def _yield(*a, **k):
    await _orig_sleep(0)


class SameClient(FakeClient):
    def __init__(self):
        super().__init__(sol=SOL_USD, wallet_sol=1.0, plan_blocked=False)
        self.history = []

    async def swap(self, body):
        gmgn.Gmgn._check_wallet(body)
        self.calls.append(("swap", body))
        return {"order_id": "od5141d", "status": "submitted", "hash": "CEd1TApS"}

    async def query_order(self, oid):
        return {"order_id": oid, "status": "confirmed", "hash": "CEd1TApS",
                "report": {"output_amount": str(TOK), "price_usd": "0.000031912267334055"}}

    async def strategy_orders(self, base_token=None, kind="open"):
        if kind == "history":
            return {"list": self.history}
        return {"list": [{"base_token": M, "order_id": "5b0a9c45", "status": "open"}]}

    async def wallet_holdings(self):
        return {"list": [], "next": "page2"}            # page 1 of 50 without the new mint (or indexer lag)


def _setup(buy_visible=True):
    c, clock = make_ctx(**{"risk.stop_pct": 15, "risk.trail_pct": 20, "gmgn.cap_trade_usd": 65.0, "gmgn.watch_s": 15})
    c.s.set("gmgn.mode", "LIVE", source="test", force=True)
    fc = SameClient()
    t = GmgnTrader(c, fc)
    c.gmgn = t
    ch = ChainSim(c.net, M)
    ch.add("CEd1TApS", int(clock.t), BUY_SOL, TOK, visible=buy_visible)
    ch.balance = TOK
    return c, clock, t, fc, ch


def _o(c):
    return c.db.one("SELECT * FROM gmgn_orders")


def _gm(c):
    return [m["text"] for m in c.notifier.sent if m["text"].startswith("GMGN · The Same Coin")]


def test_entry_is_real_fill_sol_paid_over_tokens(monkeypatch):
    monkeypatch.setattr(asyncio, "sleep", _yield)
    c, clock, t, fc, ch = _setup()
    assert run(t.on_pass(M, "The Same Coin", "SAME", 3.0e-05, 12600, 40))["status"] == "open"
    o = _o(c)
    real = 0.669872226 / 1486846.490944 * SOL_USD
    assert o["entry_src"] == "chain" and abs(o["entry_price"] - real) < 1e-12
    assert abs(o["entry_price"] - 3.1912e-05) > 1e-07              # not GMGN's fee-less report price


def test_same_coin_sequence_no_early_close_then_real_pnl(monkeypatch):
    monkeypatch.setattr(asyncio, "sleep", _yield)
    c, clock, t, fc, ch = _setup()
    t0 = clock.t
    run(t.on_pass(M, "The Same Coin", "SAME", 3.0e-05, 12600, 40))
    # 7:01:30 pump.fun DistributeFeeToHolders: +0.000901172 SOL, no token change
    ch.add("h8zb4mgv", int(t0 + 58), FEE_SOL, 0)
    # 7:01:44: GMGN holdings has no SAME. Old code closed here at +4.8%. Chain still holds every token.
    clock.t = t0 + 72
    run(t.watch_once(pairs=[], strategies={M: [{}]}))
    assert _o(c)["status"] == "open" and not any("sold" in x for x in _gm(c))
    # 7:02:57 GMGN's loss_stop sells. The balance reads 0 before the sell tx is visible to getSignatures/getTransaction.
    fc.history = [{"base_token": M, "order_id": "5b0a9c45", "status": "canceled", "place_action": "loss_stop",
                   "condition_orders": [{"order_type": "loss_stop", "status": "success"},
                                        {"order_type": "loss_stop_trace", "status": "cancel"},
                                        {"order_type": "profit_stop_trace", "status": "cancel"}]}]
    ch.add("3WZqAqbj", int(t0 + 145), SELL_SOL, -TOK, visible=False)
    ch.balance = 0
    clock.t = t0 + 150
    run(t.watch_once(pairs=[], strategies={}))
    o = _o(c)
    assert o["status"] == "exiting" and o["pnl_usd"] is None
    assert _gm(c)[-1] == "GMGN · The Same Coin sold, confirming result"
    clock.t = t0 + 166
    run(t.watch_once(pairs=[], strategies={}))                      # no second "confirming" push
    assert sum("confirming" in x for x in _gm(c)) == 1
    # the sell tx becomes visible: close with the real SOL deltas
    ch.txs["3WZqAqbj"]["visible"] = True
    clock.t = t0 + 185
    run(t.watch_once(pairs=[], strategies={}))
    o = _o(c)
    sol_in, sol_out = 0.669872226, 0.576123734
    assert o["status"] == "closed" and o["exit_reason"] == "server-side stop" and o["sell_tx"] == "3WZqAqbj"
    assert o["pnl_usd"] == round((sol_out - sol_in) * SOL_USD, 2) == -6.74
    assert o["pct"] == round(100 * (sol_out / sol_in - 1), 2) == -13.99
    assert abs(o["exit_price"] - sol_out / 1486846.490944 * SOL_USD) < 1e-12
    assert o["closed_at"] == int(t0 + 145)
    assert _gm(c)[-1] == ("GMGN · The Same Coin final result (server-side stop) · -$6.74 (-14.0%)\n"
                          "On-chain: 0.6699 SOL in → 0.5761 SOL out, fees included")
    assert not any("+4.8" in x or "about" in x for x in _gm(c))
    clock.t = t0 + 205
    run(t.watch_once(pairs=[], strategies={}))                      # closed: nothing more
    assert len(_gm(c)) == 2                                        # exactly: confirming, then final


def test_direct_close_when_sell_already_visible_and_rpc_unknown_never_closes(monkeypatch):
    monkeypatch.setattr(asyncio, "sleep", _yield)
    c, clock, t, fc, ch = _setup()
    t0 = clock.t
    run(t.on_pass(M, "The Same Coin", "SAME", 3.0e-05, 12600, 40))
    ch.balance = 0
    c.net.rpc_map["getTokenAccountsByOwner"] = lambda p: (_ for _ in ()).throw(RuntimeError("rpc down"))
    clock.t = t0 + 100
    run(t.watch_once(pairs=[], strategies={M: [{}]}))
    assert _o(c)["status"] == "open"                               # unknown balance is never "0 held"
    c.net.rpc_map["getTokenAccountsByOwner"] = ch.bal
    ch.add("3WZqAqbj", int(t0 + 90), SELL_SOL, -TOK)
    clock.t = t0 + 120
    run(t.watch_once(pairs=[], strategies={}))
    o = _o(c)
    assert o["status"] == "closed" and o["exit_reason"] == "server-side exit"      # history empty -> generic
    assert _gm(c)[-1].startswith("GMGN · The Same Coin sold (server-side exit) · -$6.74 (-14.0%)")


def test_buy_tx_not_visible_entry_fixed_later_and_no_close(monkeypatch):
    monkeypatch.setattr(asyncio, "sleep", _yield)
    c, clock, t, fc, ch = _setup(buy_visible=False)
    run(t.on_pass(M, "The Same Coin", "SAME", 3.0e-05, 12600, 40))
    o = _o(c)
    assert o["entry_src"] == "gmgn" and abs(o["entry_price"] - 3.1912267334055e-05) < 1e-15
    ch.balance = 0                                                 # even a 0 balance can't close without the buy on chain
    clock.t += 100
    run(t.watch_once(pairs=[], strategies={M: [{}]}))
    assert _o(c)["status"] == "open"
    ch.txs["CEd1TApS"]["visible"] = True
    ch.balance = TOK
    clock.t += 20
    run(t.watch_once(pairs=[], strategies={M: [{}]}))
    o = _o(c)
    assert o["entry_src"] == "chain" and abs(o["entry_price"] - 0.669872226 / 1486846.490944 * SOL_USD) < 1e-12


def test_partial_sell_is_not_a_close(monkeypatch):
    monkeypatch.setattr(asyncio, "sleep", _yield)
    c, clock, t, fc, ch = _setup()
    t0 = clock.t
    run(t.on_pass(M, "The Same Coin", "SAME", 3.0e-05, 12600, 40))
    ch.add("half", int(t0 + 60), SELL_SOL // 2, -TOK // 2)
    ch.balance = TOK - TOK // 2
    clock.t = t0 + 80
    run(t.watch_once(pairs=[], strategies={M: [{}]}))
    assert _o(c)["status"] == "open"
