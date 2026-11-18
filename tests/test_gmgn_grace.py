"""DECISION 48: 5-minute stop-loss grace on new GMGN LIVE buys (owner 2026-11-16 2:44 PM CT).
Server-side loss_stop starts at -30% with no drawdown trail; after grace_min from the fill,
those exits are swapped for the normal -15% stop + trail. Existing open rows (no exits_phase)
are left alone."""
import asyncio, json
import config, gmgn
from agents.gmgn_trader import (
    DESK_STOP, GmgnTrader, buy_body, conditions, desk_trail_check, exits_label, grace_active,
    grace_due, grace_fields, grace_is_extended, grace_phase, grace_recover_price, grace_should_extend,
    grace_window_ended, sim_exit, stop_strategy_body, GRACE_RECOVER_EXTRA_PCT, GRACE_RECOVER_TICKS,
)
from tests.helpers import make_ctx, pair, run
from tests.test_gmgn import FakeClient, M, go_live, trader

LAMP = gmgn.LAMPORTS


async def _nosleep(*a, **k):
    return None


class GraceClient(FakeClient):
    """Simulated GMGN: records create/cancel, can refuse a second strategy or fail either call."""

    def __init__(self, reject_second=False, cancel_fail=False, create_fail_n=0, smart_fail=False,
                 held=1_000_000, **kw):
        kw.setdefault("strategy_id", "st-grace")
        super().__init__(plan_blocked=False, **kw)
        self.reject_second = reject_second
        self.cancel_fail = cancel_fail
        self.create_fail_n = create_fail_n
        self.smart_fail = smart_fail
        self.held = held
        self.open_ids = ["st-grace"]
        self.n_create = 0

    async def strategy_create(self, body):
        self.calls.append(("strategy_create", body))
        gmgn.Gmgn._check_wallet(body)
        if self.create_fail_n:
            self.create_fail_n -= 1
            raise gmgn.GmgnError(400, 400, "BAD", "create failed")
        if self.smart_fail and body.get("order_type") == "smart_trade":
            raise gmgn.GmgnError(400, 400, "BAD", "smart_trade not allowed")
        if self.reject_second and self.open_ids:
            raise gmgn.GmgnError(400, 400, "EXISTS", "strategy already exists")
        self.n_create += 1
        sid = f"st-new{self.n_create}"
        self.open_ids.append(sid)
        return {"order_id": sid}

    async def strategy_cancel(self, body):
        self.calls.append(("strategy_cancel", body))
        gmgn.Gmgn._check_wallet(body)
        if self.cancel_fail:
            raise gmgn.GmgnError(400, 400, "BAD", "cancel failed")
        oid = body.get("order_id")
        if oid in self.open_ids:
            self.open_ids.remove(oid)
        return {"ok": True}

    async def strategy_orders(self, base_token=None, kind="open"):
        self.calls.append(("strategy_orders", kind))
        return {"list": [{"base_token": M, "order_id": sid, "status": "open", "order_type": "smart_trade"}
                         for sid in self.open_ids]}

    async def wallet_holdings(self):
        return {"list": [{"token_address": M, "balance": self.held, "decimals": 0}]}


def _live(client=None, **settings):
    base = {"risk.stop_pct": 15, "risk.trail_pct": 10, "risk.trail_start_pct": 20}
    base.update(settings)
    c, clock, t = trader(client or GraceClient(), **base)
    go_live(c)
    return c, clock, t


def _held(t, amount=1_000_000):
    t.c.net.rpc_map["getTokenAccountsByOwner"] = {
        "value": [{"account": {"data": {"parsed": {"info": {"tokenAmount": {"amount": str(amount)}}}}}}]
    }


def test_grace_settings_defaults_range_and_hot_load():
    c, _ = make_ctx()
    assert c.s["risk.grace_min"] == 5 and c.s["risk.grace_stop_pct"] == 30
    assert c.s["risk.grace_extend_below_stop"] is True
    assert "risk.grace_min" not in config.PROTECTED and "risk.grace_stop_pct" not in config.PROTECTED
    assert "risk.grace_extend_below_stop" not in config.PROTECTED
    assert c.s.set("grace", "7", source="test") == "grace 5m -> 7m"
    assert c.s.set("grace stop", "-25%", source="test") == "grace stop -30% -> -25%"
    import pytest
    with pytest.raises(config.SettingError):
        c.s.set("risk.grace_min", 61, source="test")
    with pytest.raises(config.SettingError):
        c.s.set("risk.grace_stop_pct", 0, source="test")
    # hot-load via sync (as if written outside the desk)
    c.db.x("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
           ("risk.grace_min", "0"))
    c.db.x("INSERT INTO change_log(ts,key,old,new,source) VALUES(?,?,?,?,?)",
           (c.clock(), "risk.grace_min", "7", "0", "chat"))
    assert "risk.grace_min" in c.s.sync() and c.s["risk.grace_min"] == 0
    assert c.s.set("grace extend", "off", source="test") == "grace extend True -> False"
    c.db.x("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
           ("risk.grace_extend_below_stop", "true"))
    c.db.x("INSERT INTO change_log(ts,key,old,new,source) VALUES(?,?,?,?,?)",
           (c.clock(), "risk.grace_extend_below_stop", "false", "true", "chat"))
    assert "risk.grace_extend_below_stop" in c.s.sync() and c.s["risk.grace_extend_below_stop"] is True


def test_grace_label_and_buy_body():
    c, _ = make_ctx(**{"risk.stop_pct": 15, "risk.trail_pct": 10})
    assert exits_label(c.s) == "stop -30% for first 5 min, then -15%"
    types = [x["order_type"] for x in buy_body(c.s, M, 1000)["condition_orders"]]
    assert types == ["loss_stop"]
    c.s.set("risk.grace_min", 0, source="test")
    assert exits_label(c.s) == "stop -15% · trail 10%"
    assert [x["order_type"] for x in buy_body(c.s, M, 1000)["condition_orders"]] == [
        "loss_stop", "loss_stop_trace"]


def test_live_buy_attaches_grace_stop_and_states_it(monkeypatch):
    monkeypatch.setattr(asyncio, "sleep", _nosleep)
    fc = GraceClient()
    c, clock, t = _live(fc)
    r = run(t.on_pass(M, "Cat", "CAT", 1.0, 50000, 80))
    assert r["status"] == "open" and r["strategy_order_id"] == "st-grace"
    body = [b for k, b in fc.calls if k == "swap"][0]
    assert body["condition_orders"] == conditions(c.s, "grace")
    assert body["condition_orders"][0]["price_scale"] == "30"
    o = c.db.one("SELECT * FROM gmgn_orders WHERE mint=?", (M,))
    assert o["exits_phase"] == "grace" and o["grace_until"] == clock.t + 300
    texts = [m["text"] for m in c.notifier.sent]
    assert any("stop -30% for first 5 min, then -15%" in x for x in texts)
    assert any("Buying Cat" in x and "stop -30% for first 5 min, then -15%" in x for x in texts)


def test_no_swap_before_grace_ends_then_create_before_cancel(monkeypatch):
    monkeypatch.setattr(asyncio, "sleep", _nosleep)
    fc = GraceClient()
    c, clock, t = _live(fc)
    _held(t)
    run(t.on_pass(M, "Cat", "CAT", 1.0, 50000, 80))
    fc.calls.clear()
    run(t.watch_once(pairs=[pair(M, price=0.90)]))          # -10%, still in grace, no promote
    assert "strategy_create" not in [k for k, _ in fc.calls]
    assert c.db.one("SELECT exits_phase FROM gmgn_orders")["exits_phase"] == "grace"
    clock.t += 301
    run(t.watch_once(pairs=[pair(M, price=0.90)]))
    kinds = [k for k, _ in fc.calls]
    assert kinds.index("strategy_create") < kinds.index("strategy_cancel")
    create = [b for k, b in fc.calls if k == "strategy_create"][0]
    assert create["from_address"] == gmgn.MAIN_WALLET
    assert create["condition_orders"] == conditions(c.s, "normal")
    assert create["condition_orders"][0]["price_scale"] == "15"
    cancel = [b for k, b in fc.calls if k == "strategy_cancel"][0]
    assert cancel["order_id"] == "st-grace" and cancel["from_address"] == gmgn.MAIN_WALLET
    o = c.db.one("SELECT * FROM gmgn_orders")
    assert o["exits_phase"] == "normal" and o["strategy_order_id"] == "st-new1"
    assert any("grace over" in m["text"] and "stop -15%" in m["text"] for m in c.notifier.sent)


def test_promote_on_resume_if_box_paused_past_grace(monkeypatch):
    monkeypatch.setattr(asyncio, "sleep", _nosleep)
    fc = GraceClient()
    c, clock, t = _live(fc)
    _held(t)
    run(t.on_pass(M, "Cat", "CAT", 1.0, 50000, 80))
    clock.t += 3600                                        # hour-long pause past the 5 min window
    fc.calls.clear()
    run(t.watch_once(pairs=[pair(M, price=0.95)]))
    assert "strategy_create" in [k for k, _ in fc.calls]
    assert c.db.one("SELECT exits_phase FROM gmgn_orders")["exits_phase"] == "normal"


def test_existing_open_position_without_phase_is_left_alone(monkeypatch):
    """The Great Blackout (id 16) style: open before this change, exits_phase NULL."""
    monkeypatch.setattr(asyncio, "sleep", _nosleep)
    fc = GraceClient(strategy_id=None)
    fc.open_ids = ["st-old"]
    c, clock, t = _live(fc)
    _held(t)
    t._row(ts=clock.t - 600, night=c.night, mode="LIVE", mint=M, name="The Great Blackout",
           status="open", size_usd=20, sol_usd=70, lamports=int(20 / 70 * LAMP),
           entry_price=1.0, peak=1.0, last_price=1.0, strategy_order_id="st-old",
           last_check=clock.t)
    # no exits_phase, no grace_until
    clock.t += 10
    run(t.watch_once(pairs=[pair(M, price=0.95)]))
    assert "strategy_create" not in [k for k, _ in fc.calls]
    assert "strategy_cancel" not in [k for k, _ in fc.calls]
    o = c.db.one("SELECT * FROM gmgn_orders")
    assert o["exits_phase"] is None and o["strategy_order_id"] == "st-old"


def test_second_strategy_rejected_cancels_then_creates(monkeypatch):
    monkeypatch.setattr(asyncio, "sleep", _nosleep)
    fc = GraceClient(reject_second=True)
    c, clock, t = _live(fc)
    _held(t)
    run(t.on_pass(M, "Cat", "CAT", 1.0, 50000, 80))
    clock.t += 301
    # first create (while old is open) fails with EXISTS; after cancel, reject_second sees empty open_ids
    def after_cancel():
        pass
    run(t.watch_once(pairs=[pair(M, price=0.95)]))
    kinds = [k for k, _ in fc.calls]
    # at least one failed create, then a cancel, then a successful create
    assert kinds.count("strategy_cancel") == 1
    o = c.db.one("SELECT * FROM gmgn_orders")
    assert o["exits_phase"] == "normal" and o["strategy_order_id"].startswith("st-new")
    assert "cancel-then-create" in (o["repair"] or "")


def test_cancel_fails_keeps_grace_stop_and_alerts(monkeypatch):
    monkeypatch.setattr(asyncio, "sleep", _nosleep)
    fc = GraceClient(reject_second=True, cancel_fail=True)
    c, clock, t = _live(fc)
    _held(t)
    run(t.on_pass(M, "Cat", "CAT", 1.0, 50000, 80))
    clock.t += 301
    run(t.watch_once(pairs=[pair(M, price=0.95)]))
    o = c.db.one("SELECT * FROM gmgn_orders")
    assert o["exits_phase"] == "grace" and o["strategy_order_id"] == "st-grace"
    assert any("ALERT" in m["text"] and "grace stop is still on" in m["text"] for m in c.notifier.sent)
    # second tick does not spam another alert
    n_alerts = sum(1 for m in c.notifier.sent if "grace stop is still on" in m["text"])
    run(t.watch_once(pairs=[pair(M, price=0.95)]))
    assert sum(1 for m in c.notifier.sent if "grace stop is still on" in m["text"]) == n_alerts


def test_create_after_cancel_fails_market_sells_and_alerts(monkeypatch):
    monkeypatch.setattr(asyncio, "sleep", _nosleep)
    fc = GraceClient(reject_second=True)
    c, clock, t = _live(fc)
    _held(t)
    run(t.on_pass(M, "Cat", "CAT", 1.0, 50000, 80))
    clock.t += 301
    # After the first EXISTS, cancel succeeds and open_ids is empty; force every later create to fail.
    orig_create = fc.strategy_create

    async def boom(body):
        if fc.open_ids:
            return await orig_create(body)                 # first: EXISTS
        fc.calls.append(("strategy_create", body))
        raise gmgn.GmgnError(400, 400, "BAD", "create failed")
    fc.strategy_create = boom
    run(t.watch_once(pairs=[pair(M, price=0.95)]))
    sells = [b for k, b in fc.calls if k == "swap" and b.get("input_token") == M]
    assert sells and c.db.one("SELECT status FROM gmgn_orders")["status"] == "exiting"
    assert any("ALERT" in m["text"] and "market-sold" in m["text"] for m in c.notifier.sent)


def test_new_stop_on_old_cancel_failed_keeps_both(monkeypatch):
    monkeypatch.setattr(asyncio, "sleep", _nosleep)
    fc = GraceClient(cancel_fail=True)                     # create-first succeeds, cancel fails
    c, clock, t = _live(fc)
    _held(t)
    run(t.on_pass(M, "Cat", "CAT", 1.0, 50000, 80))
    clock.t += 301
    run(t.watch_once(pairs=[pair(M, price=0.95)]))
    o = c.db.one("SELECT * FROM gmgn_orders")
    assert o["exits_phase"] == "normal" and o["strategy_order_id"] == "st-new1"
    assert any("couldn't cancel the old" in m["text"] for m in c.notifier.sent)


def test_paper_grace_then_normal_sim_exit():
    c, clock, t = trader(FakeClient(), **{"risk.stop_pct": 15, "risk.trail_pct": 10})
    run(t.on_pass(M, "Cat", "CAT", 1.0, 50000, 80))
    o = c.db.one("SELECT * FROM gmgn_orders")
    assert o["exits_phase"] == "grace"
    # -20% during grace: not a 30% steep drop, and no drawdown trail
    reason, _ = sim_exit(c.s, o, 0.80, clock.t)
    assert reason is None
    run(t.watch_once(pairs=[pair(M, price=0.80)]))
    assert c.db.one("SELECT status FROM gmgn_orders")["status"] == "open"
    # -30% steep drop during grace: sells
    c2, clock2, t2 = trader(FakeClient(), **{"risk.stop_pct": 15, "risk.trail_pct": 10})
    run(t2.on_pass(M, "Cat", "CAT", 1.0, 50000, 80))
    run(t2.watch_once(pairs=[pair(M, price=0.70)]))
    assert c2.db.one("SELECT exit_reason FROM gmgn_orders")["exit_reason"] == "loss_stop"
    # after grace, -16% is below the 15% stop: grace is extended, the 30% stop does not fire
    c3, clock3, t3 = trader(FakeClient(), **{"risk.stop_pct": 15, "risk.trail_pct": 10})
    run(t3.on_pass(M, "Cat", "CAT", 1.0, 50000, 80))
    clock3.t += 301
    run(t3.watch_once(pairs=[pair(M, price=0.84)]))        # -16%
    o3 = c3.db.one("SELECT * FROM gmgn_orders")
    assert o3["exits_phase"] == "grace" and o3["status"] == "open"
    run(t3.watch_once(pairs=[pair(M, price=0.70)]))        # -30% steep drop still sells
    o3 = c3.db.one("SELECT * FROM gmgn_orders")
    assert o3["exits_phase"] == "grace" and o3["status"] == "closed" and o3["exit_reason"] == "loss_stop"
    # after grace, a recovered price swaps immediately, then the normal -15% stop applies
    c4, clock4, t4 = trader(FakeClient(), **{"risk.stop_pct": 15, "risk.trail_pct": 10})
    run(t4.on_pass(M, "Cat", "CAT", 1.0, 50000, 80))
    clock4.t += 301
    run(t4.watch_once(pairs=[pair(M, price=0.95)]))        # recovered, above the 10% drawdown trail
    o4 = c4.db.one("SELECT * FROM gmgn_orders")
    assert o4["exits_phase"] == "normal" and o4["status"] == "open"
    run(t4.watch_once(pairs=[pair(M, price=0.84)]))        # -16%
    o4 = c4.db.one("SELECT * FROM gmgn_orders")
    assert o4["exits_phase"] == "normal" and o4["status"] == "closed" and o4["exit_reason"] == "loss_stop"


def test_paper_take_profit_trail_still_runs_during_grace():
    c, clock, t = trader(FakeClient(), **{"risk.stop_pct": 15, "risk.trail_pct": 10, "risk.trail_start_pct": 20})
    run(t.on_pass(M, "Cat", "CAT", 1.0, 50000, 80))
    run(t.watch_once(pairs=[pair(M, price=1.25)]))         # +25%, trail armed
    run(t.watch_once(pairs=[pair(M, price=1.10)]))         # 12% off peak > 10% trail
    o = c.db.one("SELECT * FROM gmgn_orders")
    assert o["status"] == "closed" and o["exit_reason"] == "profit_stop_trace"
    assert o["exits_phase"] == "grace"                     # still in the window


def test_reentry_buy_also_gets_grace(monkeypatch):
    monkeypatch.setattr(asyncio, "sleep", _nosleep)
    fc = GraceClient()
    c, clock, t = _live(fc)
    # a prior closed buy tonight so a normal on_pass would be refused; re-entry is allowed
    t._row(ts=clock.t - 100, night=c.night, mode="LIVE", mint=M, name="Cat", status="closed",
           reason="first", size_usd=20, closed_at=clock.t - 50)
    r = run(t.on_pass(M, "Cat", "CAT", 1.0, 50000, 80, reentry={"bouncePct": 16}))
    assert r["status"] == "open"
    body = [b for k, b in fc.calls if k == "swap"][0]
    assert body["condition_orders"][0]["price_scale"] == "30"
    o = c.db.one("SELECT * FROM gmgn_orders WHERE status='open'")
    assert o["exits_phase"] == "grace" and o["reason"] == "re-entry"
    assert any("RE-ENTRY" in m["text"] and "stop -30% for first 5 min, then -15%" in m["text"] for m in c.notifier.sent)


def test_below_normal_stop_extends_grace_instead_of_swapping(monkeypatch):
    """DECISION 65: at window end, price below -stop_pct but above -grace_stop_pct -> keep grace stop."""
    monkeypatch.setattr(asyncio, "sleep", _nosleep)
    fc = GraceClient()
    c, clock, t = _live(fc)
    _held(t)
    run(t.on_pass(M, "Cat", "CAT", 1.0, 50000, 80))
    fc.calls.clear()
    clock.t += 301
    run(t.watch_once(pairs=[pair(M, price=0.80)]))         # already -20%, below the 15% stop
    o = c.db.one("SELECT * FROM gmgn_orders")
    assert o["exits_phase"] == "grace" and o["strategy_order_id"] == "st-grace"
    assert "strategy_create" not in [k for k, _ in fc.calls]
    assert o["id"] in t._grace_extended


def test_publisher_grace_label():
    from publisher import Builder
    from tests.test_publisher_gmgn import DeskStub, _order, _desk
    d, c, clock, t = _desk()
    c.s.set("risk.stop_pct", 15, source="test")
    c.s.set("risk.trail_pct", 10, source="test")
    _order(c, exits_phase="grace", grace_until=clock.t + 120)
    p = Builder(d).gmgn()["open"][0]
    assert p["exits"]["phase"] == "grace" and p["exits"]["stopPct"] == 30
    assert p["exits"]["label"] == "stop -30% for first 5 min, then -15%"
    assert p["exits"]["graceMin"] == 5 and p["exits"]["normalStopPct"] == 15
    assert p["exits"]["graceExtended"] is False


def test_extend_off_swaps_even_when_price_is_below_normal_stop(monkeypatch):
    """With the setting off, the pre-65 behaviour remains: swap even if price is already below -stop_pct."""
    monkeypatch.setattr(asyncio, "sleep", _nosleep)
    fc = GraceClient()
    c, clock, t = _live(fc, **{"risk.grace_extend_below_stop": False})
    _held(t)
    run(t.on_pass(M, "Cat", "CAT", 1.0, 50000, 80))
    clock.t += 301
    run(t.watch_once(pairs=[pair(M, price=0.80)]))
    o = c.db.one("SELECT * FROM gmgn_orders")
    assert o["exits_phase"] == "normal"
    create = [b for k, b in fc.calls if k == "strategy_create"]
    assert create and create[0]["condition_orders"][0]["price_scale"] == "15"


def test_grace_due_extend_helpers():
    c, _ = make_ctx(**{"risk.stop_pct": 20, "risk.grace_stop_pct": 50, "risk.grace_min": 5})
    o = {"exits_phase": "grace", "grace_until": 300, "entry_price": 1.0, "ts": 0}
    assert not grace_window_ended(c.s, o, 299)
    assert grace_window_ended(c.s, o, 300)
    assert abs(grace_recover_price(c.s, o) - 0.81) < 1e-12          # -19% = stop 20% + 1% hysteresis
    assert GRACE_RECOVER_EXTRA_PCT == 1.0 and GRACE_RECOVER_TICKS == 2
    assert grace_should_extend(c.s, o, 400, 0.75)
    assert grace_should_extend(c.s, o, 400, 0.80)                    # at the stop: still extend
    assert grace_should_extend(c.s, o, 400, None)                    # no price: keep grace
    assert not grace_should_extend(c.s, o, 400, 0.81)
    assert not grace_due(c.s, o, 400, 0.75)
    assert not grace_due(c.s, o, 400, 0.81, extended=True, recover_streak=0)
    assert grace_due(c.s, o, 400, 0.81, extended=True, recover_streak=1)
    assert grace_due(c.s, o, 400, 0.90)                             # first look already recovered
    assert not grace_due(c.s, o, 200, 0.90)                         # still inside the window
    c.s.set("risk.grace_extend_below_stop", False, source="test")
    assert grace_due(c.s, o, 400, 0.75)


def test_extended_grace_waits_two_recovered_ticks(monkeypatch):
    monkeypatch.setattr(asyncio, "sleep", _nosleep)
    fc = GraceClient()
    c, clock, t = _live(fc, **{"risk.stop_pct": 20, "risk.grace_stop_pct": 50})
    _held(t)
    run(t.on_pass(M, "Cat", "CAT", 1.0, 50000, 80))
    fc.calls.clear()
    clock.t += 301
    run(t.watch_once(pairs=[pair(M, price=0.75)]))                  # -25%: extend
    assert c.db.one("SELECT exits_phase FROM gmgn_orders")["exits_phase"] == "grace"
    assert "strategy_create" not in [k for k, _ in fc.calls]
    clock.t += 2
    run(t.watch_once(pairs=[pair(M, price=0.82)]))                  # recovered, tick 1 of 2
    assert c.db.one("SELECT exits_phase FROM gmgn_orders")["exits_phase"] == "grace"
    assert "strategy_create" not in [k for k, _ in fc.calls]
    clock.t += 2
    run(t.watch_once(pairs=[pair(M, price=0.82)]))                  # tick 2: swap
    o = c.db.one("SELECT * FROM gmgn_orders")
    assert o["exits_phase"] == "normal" and o["strategy_order_id"] == "st-new1"
    create = [b for k, b in fc.calls if k == "strategy_create"]
    assert create and create[0]["condition_orders"][0]["price_scale"] == "20"
    assert any("price back above -20%" in m["text"] for m in c.notifier.sent)


def test_recover_tick_resets_if_price_dips_again(monkeypatch):
    monkeypatch.setattr(asyncio, "sleep", _nosleep)
    fc = GraceClient()
    c, clock, t = _live(fc, **{"risk.stop_pct": 20, "risk.grace_stop_pct": 50})
    _held(t)
    run(t.on_pass(M, "Cat", "CAT", 1.0, 50000, 80))
    fc.calls.clear()
    clock.t += 301
    run(t.watch_once(pairs=[pair(M, price=0.75)]))
    clock.t += 2
    run(t.watch_once(pairs=[pair(M, price=0.82)]))                  # recover tick 1
    clock.t += 2
    run(t.watch_once(pairs=[pair(M, price=0.70)]))                  # dips again: streak resets
    assert c.db.one("SELECT exits_phase FROM gmgn_orders")["exits_phase"] == "grace"
    assert t._grace_recover.get(c.db.one("SELECT id FROM gmgn_orders")["id"], 0) == 0
    clock.t += 2
    run(t.watch_once(pairs=[pair(M, price=0.82)]))                  # tick 1 again, not enough
    assert c.db.one("SELECT exits_phase FROM gmgn_orders")["exits_phase"] == "grace"
    assert "strategy_create" not in [k for k, _ in fc.calls]


def test_no_fresh_price_at_window_end_keeps_grace(monkeypatch):
    monkeypatch.setattr(asyncio, "sleep", _nosleep)
    fc = GraceClient()
    c, clock, t = _live(fc)
    _held(t)
    run(t.on_pass(M, "Cat", "CAT", 1.0, 50000, 80))
    fc.calls.clear()
    clock.t += 301
    run(t.watch_once(pairs=[]))                                     # no price for the mint
    o = c.db.one("SELECT * FROM gmgn_orders")
    assert o["exits_phase"] == "grace" and "strategy_create" not in [k for k, _ in fc.calls]
    assert o["id"] in t._grace_extended


def test_price_at_or_below_grace_stop_does_not_swap(monkeypatch):
    """If it never recovers, the -grace_stop stays on; we do not attach -stop_pct (which would fire now)."""
    monkeypatch.setattr(asyncio, "sleep", _nosleep)
    fc = GraceClient()
    c, clock, t = _live(fc, **{"risk.stop_pct": 20, "risk.grace_stop_pct": 50})
    _held(t)
    run(t.on_pass(M, "Cat", "CAT", 1.0, 50000, 80))
    fc.calls.clear()
    clock.t += 301
    run(t.watch_once(pairs=[pair(M, price=0.50)]))                  # at the -50% grace stop
    o = c.db.one("SELECT * FROM gmgn_orders")
    assert o["exits_phase"] == "grace" and "strategy_create" not in [k for k, _ in fc.calls]


def test_desk_trail_keeps_grace_stop_while_extended():
    c, _ = make_ctx(**{"risk.stop_pct": 20, "risk.grace_stop_pct": 50, "risk.grace_min": 5,
                       "risk.trail_start_pct": 20, "risk.trail_pct": 20})
    o = {"entry_price": 1.0, "peak": 1.0, "exits_phase": "grace", "ts": 0, "grace_until": 300}
    now = 400
    assert desk_trail_check(c.s, o, 0.75, now)[0] is None           # -25%: still the 50% grace stop
    assert desk_trail_check(c.s, o, 0.50, now)[0] == DESK_STOP      # -50% fires
    assert desk_trail_check(c.s, o, 0.90, now)[0] is None           # recovered: no fire
    # inside the window, same prices
    assert desk_trail_check(c.s, o, 0.75, 100)[0] is None
    assert desk_trail_check(c.s, o, 0.50, 100)[0] == DESK_STOP


def test_publisher_grace_extended_label():
    from publisher import Builder
    from tests.test_publisher_gmgn import DeskStub, _order, _desk
    d, c, clock, t = _desk()
    c.s.set("risk.stop_pct", 20, source="test")
    c.s.set("risk.grace_stop_pct", 50, source="test")
    _order(c, exits_phase="grace", grace_until=clock.t - 10, entry_price=1.0, last_price=0.75)
    p = Builder(d).gmgn()["open"][0]
    assert p["exits"]["phase"] == "grace" and p["exits"]["graceExtended"] is True
    assert p["exits"]["stopPct"] == 50
    assert p["exits"]["label"] == "grace extended · stop -50% until back above -20%"

