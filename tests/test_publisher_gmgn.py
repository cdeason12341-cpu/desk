"""Publisher gmgn section: wallet + trades in the read-only app feed; no secrets; 60s cache."""
import json, time
import gmgn, links, secrets_store
from agents.gmgn_trader import GmgnTrader
from publisher import Builder
from tests.helpers import make_ctx, run
from tests.test_gmgn import FakeClient, M

SECRET_NAMES = ("GMGN_API_KEY", "GMGN_SIGNING_KEY", "TYPESAFE_API_KEY", "TG_TOKEN",
                "VAPID_PRIVATE_KEY", "APP_TOKEN")
LEAK_FIELDS = ("quote", "body", "conditions", "tx", "order_id", "strategy_order_id",
               "sell_order_id", "lamports")


class DeskStub:
    def __init__(self, ctx, trader=None):
        self.ctx = ctx
        self.gmgn = trader
        self.auditor = type("A", (), {"live": {}, "gpa_down_alerted": False})()
        self.analyst = type("A", (), {"live": {}})()
        self.executor = type("E", (), {"pending": {}})()


def _desk(mode="LIVE", cap=65.0, tickets=4, halted=False, sol=1.02, px=70.0, client=None):
    c, clock = make_ctx(**{"gmgn.mode": mode, "gmgn.cap_trade_usd": cap, "gmgn.max_tickets_night": tickets})
    if halted:
        c.db.set_meta("gmgn_halted", True)
    t = GmgnTrader(c, client)
    t._bal = (clock.t, int(round(sol * gmgn.LAMPORTS)))
    t._sol = (clock.t, px)
    return DeskStub(c, t), c, clock, t


def _order(c, **kw):
    cols = {
        "ts": c.clock(), "night": c.night, "mode": "LIVE", "mint": M, "name": "Nyajira", "symbol": "NYA",
        "status": "open", "reason": None, "size_usd": 65.0, "sol_usd": 70.0, "lamports": 900_000_000,
        "entry_price": 0.10, "peak": 0.12, "last_price": 0.11, "last_liq": 20000,
        "order_id": "ord_secretish", "strategy_order_id": "strat_ok", "repair": None,
        "tx": "Sig111111111111111111111111111111111111111111111",
        "exit_reason": None, "exit_price": None, "pnl_usd": None, "pct": None, "closed_at": None,
        "quote": json.dumps({"secret": "SHOULD_NOT_PUBLISH", "GMGN_API_KEY": "fake"}),
        "body": json.dumps({"from_address": gmgn.MAIN_WALLET, "input_amount": "1"}),
        "conditions": json.dumps([{"order_type": "loss_stop"}]),
    }
    cols.update(kw)
    c.db.x(f"INSERT INTO gmgn_orders({','.join(cols)}) VALUES({','.join('?'*len(cols))})", tuple(cols.values()))
    return c.db.one("SELECT * FROM gmgn_orders ORDER BY id DESC LIMIT 1")


def test_gmgn_section_shape_and_values():
    d, c, clock, t = _desk()
    _order(c)
    g = Builder(d).gmgn()
    assert g["mode"] == "LIVE" and g["halted"] is False and g["enabled"] is True
    assert g["wallet"] == gmgn.MAIN_WALLET
    assert g["walletShort"] == "JAP5…5zC2"
    assert g["sol"] == 1.02 and g["usd"] == 71.4 and g["solUsd"] == 70.0
    assert g["updatedAt"]
    assert g["capTradeUsd"] == 65.0 and g["ticketsUsed"] == 1 and g["ticketsMax"] == 4
    assert g["nightSpendUsd"] == 65.0 and g["nightCapUsd"] == 260.0 and g["reserveSol"] == 0.02
    assert len(g["open"]) == 1 and len(g["orders"]) == 1
    p = g["open"][0]
    assert p["name"] == "Nyajira" and p["sizeUsd"] == 65.0 and p["mode"] == "LIVE"
    assert p["pct"] == 10.0 and p["pnlUsd"] == 6.5
    assert p["exits"] == {"stopPct": 6, "trailPct": 12, "trailStartPct": 20, "attached": True, "repair": None}
    assert p["link"] == links.coin_url(M)
    o = g["orders"][0]
    assert o["status"] == "open" and o["sizeUsd"] == 65.0 and o["link"] == links.coin_url(M)
    st, acc = Builder(d).state(), Builder(d).account()
    assert st["gmgn"]["sol"] == 1.02 and acc["gmgn"]["wallet"] == gmgn.MAIN_WALLET
    assert st["balance"] == 200.0 and acc["stack"] == 200.0   # FOMO paper stack is unchanged


def test_gmgn_section_contains_no_secrets_or_broker_fields():
    d, c, _, _ = _desk()
    planted = "SUPERSECRET_PLANTED_VALUE_9f3a"
    _order(c, quote=json.dumps({"api": planted}), body=planted, tx=planted,
           order_id=planted, strategy_order_id="keep-me-out", conditions=planted)
    files = Builder(d).build()
    blob = json.dumps(files)
    g = files["state.json"]["gmgn"]
    assert planted not in blob
    assert "keep-me-out" not in blob
    assert "Sig111111111111111111111111111111111111111111111" not in blob
    flat = json.dumps(g)
    for k in LEAK_FIELDS:
        # allowed only as documentation in comments, never as keys in the published object
        assert f'"{k}"' not in flat, k
    for n in SECRET_NAMES:
        v = secrets_store.get(n)
        if v:
            assert v not in blob
            if "PRIVATE KEY-----" in v:
                for ln in v.splitlines():
                    if ln.strip() and "-----" not in ln and len(ln.strip()) >= 16:
                        assert ln.strip() not in blob


def test_gmgn_section_without_client_or_orders():
    d, c, _, t = _desk()
    t.client = None
    t._bal = (0.0, None)
    t._sol = (0.0, None)
    g = Builder(d).gmgn()
    assert g["mode"] == "LIVE" and g["sol"] is None and g["usd"] is None
    assert g["open"] == [] and g["orders"] == [] and g["ticketsUsed"] == 0
    # no trader at all (legacy stub, like test_risk)
    d.gmgn = None
    g2 = Builder(d).gmgn()
    assert g2["wallet"] == gmgn.MAIN_WALLET and g2["sol"] is None


def test_wallet_refresh_cached_at_most_60s(monkeypatch):
    calls = []

    class C(FakeClient):
        async def user_info(self):
            calls.append("info")
            return await super().user_info()

        async def gas_price(self):
            calls.append("gas")
            return await super().gas_price()

    now = [1_800_000_000.0]
    monkeypatch.setattr(time, "time", lambda: now[0])
    c, clock = make_ctx()
    t = GmgnTrader(c, C(sol=70.0, wallet_sol=1.02))
    snap = run(t.refresh_wallet(min_age=60))
    assert snap["sol"] == 1.02 and snap["usd"] == 71.4
    assert calls == ["gas", "info"]
    now[0] += 30
    run(t.refresh_wallet(min_age=60))
    assert calls == ["gas", "info"]          # still cached
    now[0] += 31
    snap2 = run(t.refresh_wallet(min_age=60))
    assert calls == ["gas", "info", "gas", "info"]
    assert snap2["sol"] == 1.02


def test_publisher_refresh_does_not_block_on_missing_trader():
    d, c, _, _ = _desk()
    d.gmgn = None
    from publisher import Publisher
    pub = Publisher(d)
    run(pub.refresh_gmgn())   # must not raise
