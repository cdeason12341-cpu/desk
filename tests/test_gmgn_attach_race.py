"""DECISION 39: the 2026-11-15 nema LIVE buy. GMGN attached the swap's condition_orders as its own strategy, but the
swap/query_order responses carry no strategy_order_id; the desk 'repaired' 3s after the swap (tokens not landed:
'amount_in must be greater than zero'), market-sold, repaired again from a racing settle, tried a 2nd sell
(InsufficientBalanceErr) and wrongly told the owner to sell it himself."""
import asyncio
import gmgn
from agents.gmgn_trader import GmgnTrader
from tests.helpers import make_ctx, run
from tests.test_gmgn import FakeClient

M = "Fvv62MhgjrXpvXmKcLci2HoiCAxFmSkUm1Niamvipump"
FILLED = 1334328338343
_orig_sleep = asyncio.sleep


async def _yield(*a, **k):
    await _orig_sleep(0)


class NemaClient(FakeClient):
    def __init__(self, strategy_after=None, create_fail_n=0, sell_fail=None, **kw):
        super().__init__(plan_blocked=False, strategy_id=None, **kw)
        self.strategy_after, self.create_fail_n, self.sell_fail = strategy_after, create_fail_n, sell_fail
        self.n_strat = 0
        self.sells = 0

    async def swap(self, body):
        gmgn.Gmgn._check_wallet(body)
        self.calls.append(("swap", body))
        if body["input_token"] == M:
            self.sells += 1
            if self.sell_fail and self.sells >= self.sell_fail:
                raise gmgn.GmgnError(500, 50003200, None, "POST /v1/trade/swap InsufficientBalanceErr")
            return {"order_id": f"sell{self.sells}", "status": "pending"}
        return {"order_id": "od4352", "status": "submitted", "hash": "S4Suky"}      # no strategy_order_id, like GMGN

    async def query_order(self, oid):
        self.calls.append(("query_order", oid))
        if oid.startswith("sell"):
            return {"order_id": oid, "status": "confirmed",
                    "report": {"output_amount": "665785698", "price_usd": "0.000036135184172592"}}
        return {"order_id": oid, "status": "confirmed",
                "report": {"output_amount": str(FILLED), "price_usd": "0.000035565089028696"}}

    async def strategy_orders(self, base_token=None, kind="open"):
        self.calls.append(("strategy_orders", kind))
        self.n_strat += 1
        if self.strategy_after is not None and self.n_strat > self.strategy_after:
            return {"list": [{"base_token": M, "order_id": "6e4a1520", "status": "open", "order_type": "smart_trade"}]}
        return {"list": []}

    async def strategy_create(self, body):
        self.calls.append(("strategy_create", body))
        if self.create_fail_n:
            self.create_fail_n -= 1
            raise gmgn.GmgnError(400, 40000300, None, "POST /v1/trade/strategy/create amount_in must be greater than zero")
        return {"order_id": "s1"}


def _setup(client, balances):
    """balances: list of raw on-chain token amounts returned by successive getTokenAccountsByOwner calls (last repeats)."""
    c, clock = make_ctx(**{"risk.stop_pct": 15, "risk.trail_pct": 20, "gmgn.cap_trade_usd": 65.0})
    c.s.set("gmgn.mode", "LIVE", source="test", force=True)
    t = GmgnTrader(c, client)
    c.gmgn = t
    seq = list(balances)

    def bal(params):
        v = seq.pop(0) if len(seq) > 1 else seq[0]
        return {"value": [{"account": {"data": {"parsed": {"info": {"tokenAmount": {"amount": str(v)}}}}}}]}
    c.net.rpc_map["getTokenAccountsByOwner"] = bal
    return c, clock, t


def _texts(c):
    return [m["text"] for m in c.notifier.sent]


def _swaps(fc, side):
    return [b for k, b in fc.calls if k == "swap" and (b["input_token"] == M) == (side == "sell")]


def test_gmgn_own_strategy_found_no_repair_no_sell(monkeypatch):
    monkeypatch.setattr(asyncio, "sleep", _yield)
    fc = NemaClient(strategy_after=2)                       # GMGN's smart_trade strategy shows up on the 3rd look
    c, _, t = _setup(fc, [0, 0, FILLED])
    r = run(t.on_pass(M, "nema", "nma", 3.5565e-05, 12800, 42))
    assert r == {"status": "open", "strategy_order_id": "6e4a1520"}
    assert "strategy_create" not in fc.kinds() and _swaps(fc, "sell") == []
    o = c.db.one("SELECT * FROM gmgn_orders")
    assert o["status"] == "open" and o["strategy_order_id"] == "6e4a1520"
    assert not any("sell it yourself" in x or "market-sold" in x for x in _texts(c))
    assert any("exits live on GMGN" in x for x in _texts(c))


def test_waits_for_fill_then_creates_stop_with_real_amount(monkeypatch):
    monkeypatch.setattr(asyncio, "sleep", _yield)
    fc = NemaClient(create_fail_n=1)                         # exact error from the incident, once
    c, _, t = _setup(fc, [0, 0, 0, FILLED])                 # tokens land after a few polls
    r = run(t.on_pass(M, "nema", "nma", 3.5565e-05, 12800, 42))
    assert r == {"status": "open", "repair": "strategy"}
    creates = [b for k, b in fc.calls if k == "strategy_create"]
    assert len(creates) == 2 and creates[-1]["amount_in"] == str(FILLED)
    assert _swaps(fc, "sell") == []                          # no market sell: the stop attached after the fill settled


def test_exact_incident_sequence_single_sell_and_truthful_alerts(monkeypatch):
    monkeypatch.setattr(asyncio, "sleep", _yield)
    fc = NemaClient(create_fail_n=99, sell_fail=2)           # stop never attaches; a 2nd sell would hit InsufficientBalanceErr
    c, clock, t = _setup(fc, [FILLED])
    held = lambda params: {"value": [{"account": {"data": {"parsed": {"info": {"tokenAmount": {
        "amount": "0" if fc.sells else str(FILLED)}}}}}}]}             # tokens leave the wallet once the 1st sell is sent
    c.net.rpc_map["getTokenAccountsByOwner"] = held

    async def go():
        buy = asyncio.ensure_future(t.on_pass(M, "nema", "nma", 3.5565e-05, 12800, 42))
        for _ in range(5):                                   # the watcher ticks while the buy is settling (the old race)
            await _orig_sleep(0)
            await t.watch_once(pairs=[], holdings={M: 1334328.3}, strategies={})
        r = await buy
        await t.repair(1)                                     # even a late second repair can't send a 2nd sell
        return r
    r = run(go())
    assert r["repair"] == "market_sell"
    assert len(_swaps(fc, "sell")) == 1                       # exactly ONE market sell
    texts = _texts(c)
    assert not any("sell it yourself" in x or "FAILED" in x for x in texts), texts
    # watcher closes it from the chain (DECISION 42): wallet empty + sell tx found; real SOL deltas incl. fees
    from tests.chainsim import ChainSim
    ch = ChainSim(c.net, M)
    ch.add("S4Suky", int(clock.t), -664625572, FILLED)
    ch.add("2WxoBmvB", int(clock.t) + 3, 657714895, -FILLED)
    ch.add("2DECjgns", int(clock.t) + 14, 1, 0)                      # 1-lamport dust tx: not a sell
    ch.balance = 0
    clock.t += 120
    run(t.watch_once(pairs=[], holdings={M: 0.0}, strategies={}))
    o = c.db.one("SELECT * FROM gmgn_orders")
    sol_in, sol_out = 0.664625572, 0.657714895
    assert o["status"] == "closed" and o["sell_tx"] == "2WxoBmvB" and o["exit_reason"] == "no server-side exit"
    assert abs(o["exit_price"] - sol_out / 1334328.338343 * o["sol_usd"]) < 1e-12
    assert o["pct"] == round(100 * (sol_out / sol_in - 1), 2) == -1.04
    assert c.db.one("SELECT COUNT(*) n FROM gmgn_orders WHERE status IN ('open','exiting','pending','submitting')")["n"] == 0


def test_sell_failed_alert_only_when_tokens_really_held(monkeypatch):
    monkeypatch.setattr(asyncio, "sleep", _yield)
    # sell fails and on-chain shows 0 tokens -> no 'sell it yourself'
    fc = NemaClient(create_fail_n=99, sell_fail=1)
    c, _, t = _setup(fc, [FILLED] * 8 + [0])
    run(t.on_pass(M, "nema", "nma", 3.5565e-05, 12800, 42))
    assert not any("sell it yourself" in x for x in _texts(c))
    assert any("holds none" in x or "no tokens left" in x for x in _texts(c))
    # sell fails and tokens still held -> the urgent alert
    fc = NemaClient(create_fail_n=99, sell_fail=1)
    c, _, t = _setup(fc, [FILLED])
    r = run(t.on_pass(M, "nema", "nma", 3.5565e-05, 12800, 42))
    assert r["repair"] == "failed"
    assert any("sell it yourself" in x for x in _texts(c))


def test_no_sell_when_wallet_already_empty(monkeypatch):
    monkeypatch.setattr(asyncio, "sleep", _yield)
    fc = NemaClient()
    c, _, t = _setup(fc, [0])
    t._row(ts=c.clock(), night=c.night, mode="LIVE", mint=M, name="nema", status="open", size_usd=47.5, lamports=655353200,
           sol_usd=72.48, entry_price=3.5565e-05, order_id="od4352")
    assert run(t.safe_sell(1, "timeout")) == "none_held" and _swaps(fc, "sell") == []
