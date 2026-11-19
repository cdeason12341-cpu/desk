"""DECISION 89: a sell GMGN can't route (GetSwapRouteErr / little pool hit) backs off hard; B writes the order off
after NO_ROUTE_WRITE_OFF tries at its Jupiter value (tokens stay in the wallet); A backs off 5 min and up."""
import pytest
import gmgn, jupiter, strat_b, strat_b_live
from tests.helpers import run
from tests.test_gmgn import M

ROUTE_ERR = gmgn.GmgnError(400, 40000404, None, "POST /v1/trade/swap GetSwapRouteErr:little pool hit")


def test_b_no_route_backs_off_then_writes_off(monkeypatch):
    from tests.test_strat_b_live_mode import _live, _signal, _swaps, _orders, _pair
    c, clock, sb, cl, chain = _live(wallet_sol=5.0)
    _signal(c, clock, sb, chain)
    o = [x for x in _orders(c) if x["status"] == "open"][0]
    e = o["entry_price"]
    sent = []

    async def swap(body):
        sent.append(body)
        if body["input_token"] != gmgn.SOL:
            raise ROUTE_ERR
        return {"order_id": "x", "status": "pending", "hash": "h"}
    cl.swap = swap

    async def q(a, b, amount, slippage_bps=2500):
        return {"ok": True, "out": 274729, "impact_pct": 99.99, "route": "FluxBeam"}
    monkeypatch.setattr(jupiter, "quote", q)
    tick = lambda: run(sb.live.once(pairs=[_pair(o["mint"], price=e * 2.05)]))
    clock.t += 5; tick()
    assert len(sent) == 1
    wait1 = sb.live._retry_sell[o["id"]] - clock.t
    assert wait1 >= strat_b_live.NO_ROUTE_RETRY_S - 1
    clock.t += 60; tick()                                     # inside the 5 min backoff: nothing sent
    assert len(sent) == 1
    clock.t = sb.live._retry_sell[o["id"]] + 1; tick()
    assert len(sent) == 2 and sb.live._retry_sell[o["id"]] - clock.t >= 2 * strat_b_live.NO_ROUTE_RETRY_S - 1
    clock.t = sb.live._retry_sell[o["id"]] + 1; tick()        # 3rd no-route: written off
    assert len(sent) == 3
    w = [x for x in _orders(c) if x["id"] == o["id"]][0]
    assert w["status"] == "closed" and w["exit_reason"] == "honeypot - unsellable (no route)"
    value = 274729 / 1e9 * w["sol_usd"]
    assert w["pnl_usd"] == pytest.approx(value - w["size_usd"], abs=0.01) and "tokens left in wallet" in w["note"]
    assert any("wrote it off" in m["text"] for m in c.notifier.sent)
    clock.t += 3600; tick()                                    # never tried again
    assert len(sent) == 3
    import strat_b_gta as G
    assert w["night"] == c.night


def test_b_write_off_counts_zero_when_no_jupiter_route(monkeypatch):
    from tests.test_strat_b_live_mode import _live, _signal, _orders
    c, clock, sb, cl, chain = _live(wallet_sol=5.0)
    _signal(c, clock, sb, chain)
    o = [x for x in _orders(c) if x["status"] == "open"][0]

    async def q(*a, **k):
        return {"ok": False, "no_route": True, "error": "COULD_NOT_FIND_ANY_ROUTE"}
    monkeypatch.setattr(jupiter, "quote", q)
    assert run(sb.live.write_off(o["id"], "test")) == "written_off"
    w = [x for x in _orders(c) if x["id"] == o["id"]][0]
    assert w["pnl_usd"] == pytest.approx(-w["size_usd"]) and w["status"] == "closed"
    assert run(sb.live.write_off(o["id"], "test")) == "already"


def test_other_sell_errors_keep_the_short_backoff():
    from tests.test_strat_b_live_mode import _live, _signal, _orders, _pair
    c, clock, sb, cl, chain = _live(wallet_sol=5.0)
    _signal(c, clock, sb, chain)
    o = [x for x in _orders(c) if x["status"] == "open"][0]

    async def swap(body):
        raise gmgn.GmgnError(500, 500, "X", "down")
    cl.swap = swap
    clock.t += 5
    run(sb.live.once(pairs=[_pair(o["mint"], price=o["entry_price"] * 2.05)]))
    assert sb.live._retry_sell[o["id"]] - clock.t == pytest.approx(strat_b_live.SELL_RETRY_S)
    assert o["id"] not in sb.live._no_route


def test_a_no_route_sell_backs_off_five_minutes_and_up(monkeypatch):
    from tests.test_gmgn_desk_trail import _promoted_limit, _tick, _sells, _row
    from agents.gmgn_trader import trail_net_floor_price, NO_ROUTE_RETRY_S
    c, clock, t, fc = _promoted_limit(monkeypatch)
    _tick(t, clock, 1.30)
    floor_px = trail_net_floor_price(c.s, _row(c))

    async def boom(body):
        fc.calls.append(("swap", body)); raise ROUTE_ERR
    fc.swap = boom
    _tick(t, clock, floor_px)
    oid = _row(c)["id"]
    assert len(_sells(fc)) == 1 and t._trail_retry[oid] - clock.t >= NO_ROUTE_RETRY_S - 1
    _tick(t, clock, floor_px, dt=120)
    assert len(_sells(fc)) == 1
    clock.t = t._trail_retry[oid] + 1
    _tick(t, clock, floor_px, dt=0)
    assert len(_sells(fc)) == 2 and t._trail_retry[oid] - clock.t >= 2 * NO_ROUTE_RETRY_S - 1
