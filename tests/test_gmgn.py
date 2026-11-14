"""GMGN hands-off mode: signing vs the official gmgn-cli signer, caps, LIVE gate, missing-strategy repair, mode isolation, halt."""
import asyncio, base64, json, pathlib, types
import httpx, pytest
import config, gmgn
from agents.gmgn_trader import GmgnTrader, CapError, check_caps, buy_body, conditions, sell_all_body
from tests.helpers import make_ctx, pair, run

VEC = json.loads((pathlib.Path(__file__).parent / "gmgn_vector.json").read_text())
M = "Gmgn111111111111111111111111111111111pump"
LAMP = gmgn.LAMPORTS


# ---------- signing (vector produced by gmgn-cli 1.7.2 src/client/signer.ts under node, dummy test key)
def test_signing_matches_official_vector():
    for case in VEC["cases"]:
        msg = gmgn.build_message(case["path"], case["query"], case["body"], VEC["ts"])
        assert msg == case["message"]
        assert gmgn.sign(msg, VEC["pem"]) == case["sig"]


def test_body_serialization_matches_json_stringify():
    body = json.loads(VEC["body"])
    assert gmgn.dumps(body) == VEC["body"]


def test_prepare_fresh_client_id_and_verifiable_signature():
    from cryptography.hazmat.primitives import serialization
    g = gmgn.Gmgn(api_key="k" * 20, pem=VEC["pem"], clock=lambda: 1763163720)
    u1, h1, q1 = g.prepare("POST", "/v1/trade/swap", {}, '{"a":1}', True)
    u2, h2, q2 = g.prepare("POST", "/v1/trade/swap", {}, '{"a":1}', True)
    assert q1["client_id"] != q2["client_id"] and q1["timestamp"] == 1763163720
    pub = serialization.load_pem_private_key(VEC["pem"].encode(), None).public_key()
    pub.verify(base64.b64decode(h1["X-Signature"]), gmgn.build_message("/v1/trade/swap", q1, '{"a":1}', 1763163720).encode())
    assert h1["User-Agent"].startswith("gmgn-cli/") and "k" * 20 not in repr(g)


def _client(handler):
    return gmgn.Gmgn(api_key="testkey", pem=VEC["pem"], transport=httpx.MockTransport(handler))


def test_post_never_retried_get_retried_once_and_wallet_lock():
    calls = []
    def h(req):
        calls.append(req.method)
        return httpx.Response(429, json={"code": 429, "error": "RATE_LIMIT_EXCEEDED", "message": "x", "reset_at": int(__import__("time").time())})
    g = _client(h)
    with pytest.raises(gmgn.GmgnError):
        run(g.swap({"from_address": gmgn.MAIN_WALLET}))
    assert calls == ["POST"]
    calls.clear()
    with pytest.raises(gmgn.GmgnError):
        run(g.gas_price())
    assert calls == ["GET", "GET"]
    calls.clear()
    with pytest.raises(gmgn.WalletRefused):
        run(g.swap({"from_address": "So11111111111111111111111111111111111111112"}))
    assert calls == []


def test_plan_blocked_error_is_flagged():
    g = _client(lambda r: httpx.Response(403, json={"code": 403, "error": "PLAN_UPGRADE_REQUIRED", "message": "weight 11"}))
    with pytest.raises(gmgn.GmgnError) as e:
        run(g.quote(gmgn.SOL, M, 1000, 15))
    assert e.value.plan_blocked


# ---------- fakes
class FakeClient:
    configured = True

    def __init__(self, sol=70.0, wallet_sol=1.43, plan_blocked=True, strategy_id=None, strat_fail=False, confirm=True):
        self.sol, self.wallet_sol, self.plan_blocked = sol, wallet_sol, plan_blocked
        self.strategy_id, self.strat_fail, self.confirm = strategy_id, strat_fail, confirm
        self.calls = []

    async def gas_price(self):
        return {"native_token_usd_price": self.sol}

    async def user_info(self):
        return {"wallets": [{"chain": "sol", "address": gmgn.MAIN_WALLET,
                             "balances": [{"symbol": "SOL", "token_address": gmgn.SOL, "balance": str(self.wallet_sol)}]}]}

    async def quote(self, *a):
        self.calls.append(("quote", a))
        if self.plan_blocked:
            raise gmgn.GmgnError(403, 403, "PLAN_UPGRADE_REQUIRED", "x")
        return {"output_amount": "123", "min_output_amount": "100"}

    async def swap(self, body):
        gmgn.Gmgn._check_wallet(body)
        self.calls.append(("swap", body))
        r = {"order_id": f"o{len(self.calls)}", "status": "pending", "hash": "h"}
        if self.strategy_id and body["input_token"] == gmgn.SOL:
            r["strategy_order_id"] = self.strategy_id
        return r

    async def query_order(self, oid):
        self.calls.append(("query_order", oid))
        return {"order_id": oid, "status": "confirmed" if self.confirm else "pending", "report": {"price_usd": "1.0"}}

    async def strategy_create(self, body):
        self.calls.append(("strategy_create", body))
        if self.strat_fail:
            raise gmgn.GmgnError(400, 400, "BAD", "nope")
        return {"order_id": "s1"}

    async def strategy_orders(self):
        return {"list": []}

    async def wallet_holdings(self):
        return {"list": []}

    def kinds(self):
        return [k for k, _ in self.calls]


def trader(client=None, **settings):
    c, clock = make_ctx(**settings)
    t = GmgnTrader(c, client or FakeClient())
    c.gmgn = t
    return c, clock, t


def go_live(c):
    c.s.set("gmgn.mode", "LIVE", source="test", force=True)


# ---------- caps
def test_caps_hard_limits():
    c, _, t = trader()
    s, n = c.s, c.night
    lam = lambda usd: int(usd / 70 * LAMP)
    check_caps(s, c.db, n, "LIVE", 47.5, lam(47.5), 70, 10 * LAMP)
    with pytest.raises(CapError, match="per-trade cap"):
        check_caps(s, c.db, n, "LIVE", 47.51, lam(47.51), 70, 10 * LAMP)
    with pytest.raises(CapError, match="per-trade cap"):          # lamports over cap even if size_usd says ok
        check_caps(s, c.db, n, "LIVE", 47.5, lam(60), 70, 10 * LAMP)
    with pytest.raises(CapError, match="wallet has"):
        check_caps(s, c.db, n, "LIVE", 47.5, lam(47.5), 70, lam(47.5))
    with pytest.raises(CapError, match="unknown"):
        check_caps(s, c.db, n, "LIVE", 47.5, lam(47.5), 70, None)
    for i in range(4):
        t._row(ts=1, night=n, mode="LIVE", mint=f"m{i}", status="closed", size_usd=47.5)
    with pytest.raises(CapError, match="ticket cap reached"):
        check_caps(s, c.db, n, "LIVE", 10, lam(10), 70, 10 * LAMP)
    check_caps(s, c.db, n, "PAPER", 47.5, lam(47.5), 70, 10 * LAMP)   # PAPER tickets counted separately


def test_night_spend_cap():
    c, _, t = trader()
    c.s.set("gmgn.max_tickets_night", 5, source="test", force=True)
    for i in range(4):
        t._row(ts=1, night=c.night, mode="LIVE", mint=f"m{i}", status="open", size_usd=47.5)
    t._row(ts=1, night=c.night, mode="LIVE", mint="x", status="refused", size_usd=47.5)      # refusals don't count
    check_caps(c.s, c.db, c.night, "LIVE", 47.5, int(47.5 / 70 * LAMP), 70, 100 * LAMP)     # 5th fits: 237.50
    c.s.set("gmgn.cap_trade_usd", 40, source="test", force=True)                             # night cap now 200
    with pytest.raises(CapError, match="night spend cap"):
        check_caps(c.s, c.db, c.night, "LIVE", 40, int(40 / 70 * LAMP), 70, 100 * LAMP)


def test_caps_only_raisable_by_owner_confirm():
    c, _, t = trader()
    for name in ("cap trade", "gmgn.cap_trade_usd", "cap tickets", "gmgn mode", "gmgn gate"):
        with pytest.raises(config.SettingError):
            c.s.set(name, "100")
    from telegram import Telegram
    d = types.SimpleNamespace(ctx=c, gmgn=t)
    tg = Telegram(d)
    out = run(tg.command("cap trade 60"))
    assert out[0].startswith("That command only works from the owner")
    tg.from_owner = True
    assert "Type CONFIRM CAP" in run(tg.command("cap trade 60"))[0]
    assert c.s["gmgn.cap_trade_usd"] == 47.5
    assert run(tg.command("confirm cap"))[0] == "Type CONFIRM CAP in capitals."
    assert "47.50 -> $60.00" in run(tg.command("CONFIRM CAP"))[0]
    assert c.s["gmgn.cap_trade_usd"] == 60.0
    row = c.db.one("SELECT * FROM change_log WHERE key='gmgn.cap_trade_usd' ORDER BY id DESC LIMIT 1")
    assert row["source"] == "telegram owner CONFIRM CAP" and json.loads(row["new"]) == 60.0
    assert run(tg.command("CONFIRM CAP"))[0].startswith("Nothing to confirm")
    assert "Tickets per night 4 -> 6" in run(tg.command("cap tickets 6"))[0]


# ---------- PAPER, halt, isolation
def test_paper_never_swaps_records_conditions():
    fc = FakeClient()
    c, _, t = trader(fc)
    r = run(t.on_pass(M, "Gm", "GM", 1.0, 50000, 80))
    assert r["status"] == "paper" and "swap" not in fc.kinds() and fc.kinds() == ["quote"]
    o = c.db.one("SELECT * FROM gmgn_orders WHERE mint=?", (M,))
    assert o["mode"] == "PAPER" and o["size_usd"] == 47.5 and o["status"] == "open"
    conds = json.loads(o["conditions"])
    assert conds[0] == {"order_type": "loss_stop", "side": "sell", "price_scale": "6", "sell_ratio": "100"}
    assert conds[1]["order_type"] == "loss_stop_trace" and conds[1]["drawdown_rate"] == "12"
    assert json.loads(o["body"])["from_address"] == gmgn.MAIN_WALLET
    msg = c.notifier.sent[-1]["text"]
    assert msg.startswith("PAPER · GMGN · Buy Gm · $47.50") and "not available on your GMGN plan" in msg
    assert c.db.one("SELECT COUNT(*) n FROM positions")["n"] == 0          # FOMO book untouched
    assert run(t.on_pass(M, "Gm", "GM", 1.0, 50000, 80)) is None           # one per coin per night
    # plan-blocked quote is not re-called within 10 min
    run(t.on_pass(M[:-5] + "Xpump", "Gm2", "G2", 1.0, 50000, 80))
    assert fc.kinds() == ["quote"]


def test_paper_exits_simulated():
    c, clock, t = trader(FakeClient())
    run(t.on_pass(M, "Gm", "GM", 1.0, 50000, 80))
    run(t.watch_once(pairs=[pair(M, price=1.25)]))
    run(t.watch_once(pairs=[pair(M, price=1.08)]))
    o = c.db.one("SELECT * FROM gmgn_orders WHERE mint=?", (M,))
    assert o["status"] == "closed" and o["exit_reason"] == "profit_stop_trace"
    assert c.notifier.sent[-1]["text"].startswith("PAPER · GMGN · Sold Gm · trailing take-profit")


def test_halt_blocks_buys():
    fc = FakeClient(plan_blocked=False)
    c, _, t = trader(fc)
    go_live(c)
    t.halt(True)
    r = run(t.on_pass(M, "Gm", "GM", 1.0, 50000, 80))
    assert r["status"] == "refused" and r["reason"] == "halted" and "swap" not in fc.kinds()
    assert c.db.one("SELECT new FROM change_log WHERE key='gmgn.halted'")["new"] == "true"


def test_live_refuses_over_balance_and_alerts():
    fc = FakeClient(plan_blocked=False, wallet_sol=0.5)
    c, _, t = trader(fc)
    go_live(c)
    r = run(t.on_pass(M, "Gm", "GM", 1.0, 50000, 80))
    assert r["status"] == "refused" and "wallet has" in r["reason"] and "swap" not in fc.kinds()
    assert c.notifier.sent[-1]["text"].startswith("GMGN · Refused Gm: wallet has")


def test_live_swap_with_exits_attached(monkeypatch):
    monkeypatch.setattr(asyncio, "sleep", _nosleep)
    fc = FakeClient(plan_blocked=False, strategy_id="st1")
    c, _, t = trader(fc)
    go_live(c)
    r = run(t.on_pass(M, "Gm", "GM", 1.0, 50000, 80))
    assert r["status"] == "open" and r["strategy_order_id"] == "st1"
    body = [b for k, b in fc.calls if k == "swap"][0]
    assert body["from_address"] == gmgn.MAIN_WALLET and body["condition_orders"] == conditions(c.s)
    assert int(body["input_amount"]) == int(47.5 / 70 * LAMP) and body["priority_fee"] and body["tip_fee"]
    assert "strategy_create" not in fc.kinds()


async def _nosleep(*a, **k):
    return None


def test_missing_strategy_repair_creates_stop(monkeypatch):
    monkeypatch.setattr(asyncio, "sleep", _nosleep)
    fc = FakeClient(plan_blocked=False, strategy_id=None)
    c, _, t = trader(fc)
    go_live(c)
    r = run(t.on_pass(M, "Gm", "GM", 1.0, 50000, 80))
    assert r == {"status": "open", "repair": "strategy"}
    sc = [b for k, b in fc.calls if k == "strategy_create"][0]
    assert sc["sub_order_type"] == "stop_loss" and sc["amount_in_percent"] == "100" and sc["from_address"] == gmgn.MAIN_WALLET
    assert abs(float(sc["check_price"]) - 0.94 / 70) < 1e-9
    assert c.db.one("SELECT strategy_order_id FROM gmgn_orders")["strategy_order_id"] == "s1"


def test_missing_strategy_repair_fails_market_sells_and_alerts(monkeypatch):
    monkeypatch.setattr(asyncio, "sleep", _nosleep)
    fc = FakeClient(plan_blocked=False, strategy_id=None, strat_fail=True)
    c, _, t = trader(fc)
    go_live(c)
    r = run(t.on_pass(M, "Gm", "GM", 1.0, 50000, 80))
    assert r == {"status": "exiting", "repair": "market_sell"}
    sells = [b for k, b in fc.calls if k == "swap" and b["input_token"] == M]
    assert sells and sells[0]["input_amount_bps"] == "10000" and sells[0]["output_token"] == gmgn.SOL
    assert any("ALERT Gm" in m["text"] and "market-sold" in m["text"] for m in c.notifier.sent)


def test_gate_2h_then_owner_types_live(monkeypatch):
    fc = FakeClient(plan_blocked=True)
    c, clock, t = trader(fc)
    from telegram import Telegram
    tg = Telegram(types.SimpleNamespace(ctx=c, gmgn=t)); tg.from_owner = True
    assert "needs 2h of GMGN PAPER" in run(tg.command("gmgn live"))[0]
    clock.t += 2 * 3600 - 60
    assert "needs 2h" in run(tg.command("gmgn live"))[0]
    clock.t += 61
    assert "can't place trades through the API" in run(tg.command("gmgn live"))[0]     # free plan
    fc.plan_blocked = False
    assert run(tg.command("GMGN LIVE"))[0].startswith("Send 'gmgn live' first")
    assert "Type GMGN LIVE" in run(tg.command("gmgn live"))[0]
    assert c.s["gmgn.mode"] == "PAPER"
    assert run(tg.command("live"))[0] != "" and c.s["gmgn.mode"] == "PAPER"           # FOMO 'live' never flips GMGN
    assert run(tg.command("GMGN LIVE"))[0].startswith("GMGN PAPER -> LIVE")
    assert c.s["gmgn.mode"] == "LIVE" and c.s["desk.mode"] == "PAPER"
    t.record_gate_rule(); t.record_gate_rule()
    rows = c.db.rows("SELECT * FROM change_log WHERE key='gmgn.live_gate'")
    assert len(rows) == 1 and "1 full PAPER night -> 2h PAPER" in rows[0]["source"]


def test_executor_isolation_fomo_unchanged():
    """FOMO push/position identical with GMGN on; a GMGN crash can't break FOMO."""
    from agents.executor import Executor
    async def body(with_gmgn, broken=False):
        c, _ = make_ctx()
        if with_gmgn:
            t = GmgnTrader(c, FakeClient())
            if broken:
                async def boom(*a, **k): raise RuntimeError("x")
                t._on_pass = boom
            c.gmgn = t
        ex = Executor(c)
        out = await ex.run({"scan": {"mint": M, "name": "Gm", "liquidityUsd": 50000}, "analyst": {"score": 80}}, pair=pair(M))
        await asyncio.gather(*list(ex.gmgn_tasks))
        fomo = [m for m in c.notifier.sent if m["kind"] == "buy"]
        return out, fomo, c
    a, fa, _ = run(body(False))
    b, fb, cb = run(body(True))
    x, fx, cx = run(body(True, broken=True))
    assert a == b == x and fa == fb == fx and len(fa) == 1
    assert cb.db.one("SELECT COUNT(*) n FROM gmgn_orders")["n"] == 1
    assert cx.db.one("SELECT COUNT(*) n FROM errors WHERE agent='gmgn'")["n"] == 1
    assert all(btn.get("url", "").find("r=savipww") > 0 for row in fb[0]["buttons"] for btn in row if "url" in btn)
