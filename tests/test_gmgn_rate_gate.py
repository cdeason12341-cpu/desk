"""DECISION 88: one process-wide GMGN gate. A 429 (esp. RATE_LIMIT_BANNED*) closes it until reset_at (default 6 min)
and nothing is sent until then; a weighted bucket paces calls; sells go first; per-order sell retries back off."""
import asyncio, json, time
import httpx, pytest
import gmgn, strat_b
from tests.helpers import run
from tests.test_gmgn import VEC, M


class Clock:
    def __init__(self, t=1_800_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


def _gate(clock):
    async def sleep(s):
        clock.t += s
    return gmgn.Gate(clock=clock, sleep=sleep)


def _client(handler, gate, clock):
    return gmgn.Gmgn(api_key="testkey", pem=VEC["pem"], transport=httpx.MockTransport(handler), clock=clock, gate=gate)


def _ok(calls):
    def h(req):
        calls.append(req.url.path)
        return httpx.Response(200, json={"code": 0, "data": {"order_id": "o1", "status": "pending"}})
    return h


SELL = {"from_address": gmgn.MAIN_WALLET, "input_token": M, "output_token": gmgn.SOL}
BUY = {"from_address": gmgn.MAIN_WALLET, "input_token": gmgn.SOL, "output_token": M, "input_amount": "1000"}


def test_banned_max_closes_gate_until_reset_at_and_nothing_is_sent():
    clock = Clock(); gate = _gate(clock); calls = []
    reset = int(clock.t) + 300

    def h(req):
        calls.append(req.url.path)
        return httpx.Response(429, json={"code": 429, "error": "RATE_LIMIT_BANNED_MAX", "message": "stop all requests",
                                         "reset_at": reset})
    g = _client(h, gate, clock)
    with pytest.raises(gmgn.GmgnError) as e:
        run(g.swap(SELL))
    assert e.value.error == "RATE_LIMIT_BANNED_MAX" and calls == ["/v1/trade/swap"]
    assert gate.closed() and gate.banned_until == reset + gmgn.BAN_PAD_S
    for fn in (lambda: g.swap(SELL), lambda: g.swap(BUY), g.gas_price, g.user_info, g.wallet_holdings,
               lambda: g.query_order("x"), lambda: g.quote(gmgn.SOL, M, 1, 15)):
        with pytest.raises(gmgn.GateClosed):
            run(fn())
    assert calls == ["/v1/trade/swap"]                        # every call above was refused locally
    clock.t = reset + gmgn.BAN_PAD_S + 0.1
    g.http._transport = httpx.MockTransport(_ok(calls))
    assert run(g.swap(SELL))["order_id"] == "o1"              # the sell goes as soon as the ban ends
    assert gate.buy_blocked() and "sells first" in gate.buy_blocked()
    with pytest.raises(gmgn.GateClosed):
        run(g.swap(BUY))                                      # buys wait BUY_AFTER_OPEN_S more
    clock.t += gmgn.BUY_AFTER_OPEN_S + 1
    assert gate.buy_blocked() is None and run(g.swap(BUY))["order_id"] == "o1"


def test_429_without_reset_defaults_to_six_minutes_and_past_reset_too():
    for body in ({"code": 429, "error": "RATE_LIMIT_BANNED", "message": "x"},
                 {"code": 429, "error": "RATE_LIMIT_EXCEEDED", "message": "x", "reset_at": 5}):
        clock = Clock(); gate = _gate(clock)
        g = _client(lambda r: httpx.Response(429, json=body), gate, clock)
        with pytest.raises(gmgn.GmgnError):
            run(g.gas_price())
        assert gate.remaining() == pytest.approx(gmgn.BAN_DEFAULT_S)


def test_header_reset_and_retry_after_are_used():
    clock = Clock(); gate = _gate(clock)
    gate.observe(429, {"error": "RATE_LIMIT_EXCEEDED"}, {"x-ratelimit-reset": str(int(clock.t) + 40)})
    assert gate.remaining() == pytest.approx(40 + gmgn.BAN_PAD_S)
    clock2 = Clock(); g2 = _gate(clock2)
    g2.observe(429, {}, {"retry-after": "30"})
    assert g2.remaining() == pytest.approx(30 + gmgn.BAN_PAD_S)


def test_no_get_retry_on_429():
    clock = Clock(); gate = _gate(clock); calls = []

    def h(req):
        calls.append(req.method)
        return httpx.Response(429, json={"code": 429, "error": "RATE_LIMIT_EXCEEDED", "reset_at": int(clock.t) + 3})
    with pytest.raises(gmgn.GmgnError):
        run(_client(h, gate, clock).query_order("x"))
    assert calls == ["GET"]


def test_swaps_are_spaced_and_bucket_paces_a_sell_loop():
    clock = Clock(); gate = _gate(clock); calls = []; sent_at = []

    def h(req):
        calls.append(req.url.path); sent_at.append(clock.t)
        return httpx.Response(200, json={"code": 0, "data": {}})
    g = _client(h, gate, clock)
    t0 = clock.t
    for _ in range(10):
        run(g.swap(SELL))
    gaps = [b - a for a, b in zip(sent_at, sent_at[1:])]
    assert min(gaps) >= gmgn.SWAP_GAP_S - 1e-9
    # 10 swaps x weight 10 at 10 units/s: at least ~8 s even with a full bucket to start
    assert clock.t - t0 >= (10 * 10 - gmgn.BUCKET_CAP) / gmgn.BUCKET_RATE - 1e-6


def test_price_quotes_fail_fast_and_reads_leave_room_for_a_swap():
    clock = Clock(); gate = _gate(clock); calls = []
    g = _client(_ok(calls), gate, clock)
    run(g.swap(SELL))
    gate.level = 15.0                                         # busy bucket
    with gmgn.kind("price"):
        with pytest.raises(gmgn.GateClosed, match="busy"):
            run(g.quote(M, gmgn.SOL, 1, 15))                  # would need to wait ~1 s+: refused, not sent
    assert calls == ["/v1/trade/swap"]
    t0 = clock.t
    run(g.quote(M, gmgn.SOL, 1, 15))                          # a normal read waits until a swap's worth stays free
    assert clock.t - t0 >= 1.5 - 1e-6
    assert gate.level + 10 <= gmgn.BUCKET_CAP + 1e-6          # a sell could still go right now


def test_server_remaining_header_fills_the_local_bucket():
    clock = Clock(); gate = _gate(clock)
    gate.observe(200, {}, {"x-ratelimit-limit": "20", "x-ratelimit-remaining": "3"})
    assert gate.level == pytest.approx(17)


def test_sells_go_before_queued_reads_and_buys():
    clock = Clock(); gate = _gate(clock)
    order = []

    async def scenario():
        gate.level = gmgn.BUCKET_CAP                           # full bucket: everyone must wait
        async def go(k, w, swap, tag):
            await gate.acquire(k, w, swap)
            order.append(tag)
        real_sleep = gate.sleep

        async def sleep(s):                                    # advance time and let other tasks run
            clock.t += s
            await asyncio.sleep(0)
        gate.sleep = sleep
        await asyncio.gather(go("read", 5, False, "read"), go("sell", 10, True, "sell"))
        gate.sleep = real_sleep
    run(scenario())
    assert order[0] == "sell"


def test_retry_at_backs_off_and_never_before_ban_end():
    clock = Clock(); gate = _gate(clock)
    assert gate.retry_at(100.0, 15, 1) == 115.0
    assert gate.retry_at(100.0, 15, 2) == 130.0
    assert gate.retry_at(100.0, 15, 9) == 100.0 + gmgn.SELL_BACKOFF_MAX_S
    gate.observe(429, {"error": "RATE_LIMIT_BANNED_MAX", "reset_at": clock.t + 300}, {})
    assert gate.retry_at(100.0, 15, 1) >= 100.0 + 300


def test_ban_survives_a_restart(tmp_path):
    clock = Clock(); p = str(tmp_path / "gate.json")
    g1 = gmgn.Gate(clock=clock, path=p)
    g1.observe(429, {"error": "RATE_LIMIT_BANNED_MAX", "reset_at": clock.t + 200}, {})
    g2 = gmgn.Gate(clock=clock, path=p)
    assert g2.closed() and g2.remaining() == pytest.approx(200 + gmgn.BAN_PAD_S) and g2.ban_reason == "RATE_LIMIT_BANNED_MAX"
    assert "key" not in open(p).read().lower()


def test_route_kinds():
    assert gmgn.route_kind("POST", "/v1/trade/swap", SELL) == "sell"
    assert gmgn.route_kind("POST", "/v1/trade/swap", BUY) == "buy"
    assert gmgn.route_kind("POST", "/v1/trade/strategy/create", {}) == "sell"
    assert gmgn.route_kind("GET", "/v1/trade/query_order", None) == "read"
    with gmgn.kind("price"):
        assert gmgn.route_kind("GET", "/v1/trade/quote", None) == "price"


# ---------- callers: A and B hold sells and skip buys while the gate is closed
def _ban(seconds=300):
    gmgn.GATE.observe(429, {"error": "RATE_LIMIT_BANNED_MAX", "reset_at": time.time() + seconds}, {})


def test_a_desk_sell_held_during_ban_then_backs_off_and_sells(monkeypatch):
    from tests.test_gmgn_desk_trail import _promoted_limit, _tick, _sells, _row
    from agents.gmgn_trader import trail_net_floor_price
    c, clock, t, fc = _promoted_limit(monkeypatch)
    _tick(t, clock, 1.30)
    floor_px = trail_net_floor_price(c.s, _row(c))
    _ban(300)
    for _ in range(5):
        _tick(t, clock, floor_px, dt=20)                      # trigger every tick, but nothing is sent
    assert not _sells(fc)
    oid = _row(c)["id"]
    assert t._trail_retry[oid] >= clock.t + 200                # next try is after the ban, not in 15 s
    gmgn.GATE.reset()                                          # ban over
    clock.t = t._trail_retry[oid] + 1
    _tick(t, clock, floor_px, dt=0)
    assert len(_sells(fc)) == 1 and _row(c)["status"] == "exiting"
    assert oid not in t._sell_fails


def test_a_buy_skipped_during_ban():
    from tests.test_gmgn import trader, go_live
    c, clock, t = trader()
    go_live(c)
    _ban(300)
    r = run(t.on_pass(M, "Cat", "CAT", 1.0, 50000, 80))
    assert r["status"] == "refused" and "ban" in r["reason"]
    assert "swap" not in t.client.kinds()
    assert not c.db.one("SELECT id FROM gmgn_orders WHERE mint=? AND status IN ('submitting','failed')", (M,))


def test_b_buy_skipped_and_sell_held_during_ban():
    from tests.test_strat_b_live_mode import _live, _signal, _swaps, _orders, _pair
    from tests.test_strat_b import _tick
    c, clock, sb, cl, chain = _live(wallet_sol=5.0)
    _ban(300)
    _signal(c, clock, sb, chain)
    assert not _swaps(cl) and not [o for o in _orders(c) if o["status"] in ("submitting", "failed", "open")]
    gmgn.GATE.reset()
    _tick(sb, clock, 0.85)                                    # same signal, gate open: it buys now
    o = [o for o in _orders(c) if o["status"] == "open"][0]
    e = o["entry_price"]
    _ban(300)
    n0 = len(cl.calls)
    clock.t += 5
    run(sb.live.once(pairs=[_pair(o["mint"], price=e * 2.05)]))
    assert not [b for b in _swaps(cl) if b["input_token"] == o["mint"]]
    assert sb.live._retry_sell[o["id"]] >= clock.t + 200
    gmgn.GATE.reset()
    clock.t = sb.live._retry_sell[o["id"]] + 1
    run(sb.live.once(pairs=[_pair(o["mint"], price=e * 2.05)]))
    o = [x for x in _orders(c) if x["id"] == o["id"]][0]
    assert o["status"] == "exiting" and o["exit_reason"] == strat_b.TP
    assert len([b for b in _swaps(cl) if b["input_token"] == o["mint"]]) == 1
