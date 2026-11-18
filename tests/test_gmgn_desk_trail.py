"""DECISION 52: desk-side trailing take-profit for GMGN LIVE positions.
DECISION 80: the trail still arms at +trail_start_pct, but never sells below entry + trail_floor_pct.
profit_stop_trace is no longer attached; the -stop_pct hard stop is unchanged."""
import asyncio, json
import config, gmgn
from agents.gmgn_trader import (DESK_STOP, DESK_TRAIL, EXIT_LABEL, LIMIT_STOP_MARK, conditions,
                                desk_trail_check, has_profit_stop_trace, limit_stop_only,
                                sim_exit, stop_strategy_body, trail_floor_price, trail_trigger)
from tests.helpers import make_ctx, pair, run
from tests.test_gmgn import M
from tests.test_gmgn_grace import GraceClient, _held, _live, _nosleep


def _row(c):
    return c.db.one("SELECT * FROM gmgn_orders WHERE mint=?", (M,))


def _sells(fc):
    return [b for k, b in fc.calls if k == "swap" and b["input_token"] == M]


def _promoted_limit(monkeypatch, **settings):
    """A LIVE desk buy whose post-grace smart_trade is refused by GMGN -> limit_order stop fallback."""
    monkeypatch.setattr(asyncio, "sleep", _nosleep)
    fc = GraceClient(smart_fail=True)
    base = {"risk.trail_pct": 20, "risk.stop_pct": 20, "risk.surge_sell_enabled": False}
    base.update(settings)
    c, clock, t = _live(fc, **base)
    _held(t)
    assert run(t.on_pass(M, "Cat", "CAT", 1.0, 50000, 80))["status"] == "open"
    clock.t += 301
    run(t.watch_once(pairs=[pair(M, price=1.0)]))
    fc.calls.clear()
    return c, clock, t, fc


def _tick(t, clock, price, dt=3):
    clock.t += dt
    return run(t.trail_once(pairs=[pair(M, price=price)]))


def test_check_arms_at_start_and_sells_on_live_trail_pct():
    c, _ = make_ctx(**{"risk.trail_start_pct": 20, "risk.trail_pct": 25, "risk.stop_pct": 20})
    o = {"entry_price": 1.0, "peak": 1.0, "exits_phase": "normal", "ts": 0}
    assert desk_trail_check(c.s, o, 1.19, 10) == (None, 1.19, False)
    assert desk_trail_check(c.s, {**o, "peak": 1.19}, 0.95, 10)[0] is None        # never armed: no trail sell
    r, peak, armed = desk_trail_check(c.s, o, 1.20, 10)
    assert armed and r is None and peak == 1.20
    o["peak"] = 1.60
    assert desk_trail_check(c.s, o, 1.21, 10)[0] is None                           # -24.4% from peak, trail 25
    assert desk_trail_check(c.s, o, 1.20, 10)[0] == DESK_TRAIL                     # -25%
    c.s.set("risk.trail_pct", 20, source="test")                                   # read live, not hardcoded
    assert desk_trail_check(c.s, o, 1.28, 10)[0] == DESK_TRAIL
    assert desk_trail_check(c.s, {**o, "peak": 1.0}, 0.80, 10)[0] == DESK_STOP     # backup -20% stop
    assert EXIT_LABEL[DESK_TRAIL] == "trailing take-profit (desk)"


def test_limit_stop_only_detection():
    smart = json.dumps([{"order_type": "loss_stop"}, {"order_type": "profit_stop_trace"}])
    assert limit_stop_only({"repair": "adopted manual buy; stop_loss strategy created", "conditions": smart})
    assert limit_stop_only({"repair": "grace_swap " + LIMIT_STOP_MARK, "conditions": smart})
    assert limit_stop_only({"repair": "", "conditions": json.dumps([{"order_type": "loss_stop", "price_scale": "20"}])})
    assert not limit_stop_only({"repair": "grace_swap", "conditions": smart})
    assert not limit_stop_only({"repair": None, "conditions": None})


def test_stop_body_check_price_is_usd():
    c, _ = make_ctx(**{"risk.stop_pct": 20})
    b = stop_strategy_body(c.s, M, 4.886e-05, 71.05, 1000)
    assert abs(float(b["check_price"]) - 4.886e-05 * 0.8) < 1e-15
    assert b["order_type"] == "limit_order" and b["sub_order_type"] == "stop_loss"


def test_promote_fallback_marks_limit_stop_and_says_desk_trail(monkeypatch):
    c, clock, t, fc = _promoted_limit(monkeypatch)
    o = _row(c)
    assert LIMIT_STOP_MARK in o["repair"] and o["exits_phase"] == "normal"
    assert [x["order_type"] for x in json.loads(o["conditions"])] == ["loss_stop"]
    assert any("runs from my computer" in m["text"] for m in c.notifier.sent)
    assert not any("now live on GMGN" in m["text"] and "trail" in m["text"] for m in c.notifier.sent)


def test_trail_arms_then_sells_whole_position_once(monkeypatch):
    c, clock, t, fc = _promoted_limit(monkeypatch)
    assert _tick(t, clock, 1.10) == 1
    assert _row(c)["id"] not in t._trail_armed
    _tick(t, clock, 1.25)                                     # +25% >= +20% start -> armed
    assert _row(c)["id"] in t._trail_armed
    _tick(t, clock, 1.50)                                     # new peak
    assert _row(c)["peak"] == 1.50
    _tick(t, clock, 1.21)                                     # -19.3% from peak: hold (trail 20)
    assert not _sells(fc)
    _tick(t, clock, 1.19)                                     # -20.7%: sell 100%
    s = _sells(fc)
    assert len(s) == 1 and s[0]["input_amount_bps"] == "10000"
    o = _row(c)
    assert o["status"] == "exiting" and o["exit_reason"] == DESK_TRAIL and o["last_check"] == 0
    cancels = [b for k, b in fc.calls if k == "strategy_cancel"]
    assert cancels and cancels[0]["order_type"] == "limit_order"     # the GMGN limit stop is cancelled after the sell
    assert any("trailing take-profit" in m["text"] and "market-selling" in m["text"] for m in c.notifier.sent)
    assert "buy" not in [k for k, _ in fc.calls] and all(b["input_token"] == M for k, b in fc.calls if k == "swap")
    _tick(t, clock, 1.00)                                     # no second sell
    assert len(_sells(fc)) == 1


def test_not_armed_holds_then_backup_stop(monkeypatch):
    c, clock, t, fc = _promoted_limit(monkeypatch)
    _tick(t, clock, 1.15)
    _tick(t, clock, 0.90)                                     # -22% from 1.15 but never armed
    assert not _sells(fc)
    _tick(t, clock, 0.80)                                     # -20% from entry: desk backup stop
    assert len(_sells(fc)) == 1 and _row(c)["exit_reason"] == DESK_STOP


def test_smart_trade_positions_are_desk_trailed(monkeypatch):
    monkeypatch.setattr(asyncio, "sleep", _nosleep)
    fc = GraceClient()
    c, clock, t = _live(fc, **{"risk.surge_sell_enabled": False, "risk.trail_pct": 20, "risk.trail_floor_pct": 5})
    _held(t)
    run(t.on_pass(M, "Cat", "CAT", 1.0, 50000, 80))
    assert "profit_stop_trace" not in {x["order_type"] for x in json.loads(_row(c)["conditions"])}
    assert _tick(t, clock, 1.30) == 1                         # desk trail watches smart_trade too
    assert _row(c)["id"] in t._trail_armed
    _tick(t, clock, 1.06)                                     # still above the +5% floor
    assert not _sells(fc)
    _tick(t, clock, 1.05)                                     # floor hit
    assert len(_sells(fc)) == 1 and _row(c)["exit_reason"] == DESK_TRAIL


def test_no_surge_adopted_buy_still_trailed_and_zero_held_skips(monkeypatch):
    c, clock, t, fc = _promoted_limit(monkeypatch)
    c.db.x("UPDATE gmgn_orders SET no_surge=1, repair='adopted manual buy; stop_loss strategy created' WHERE mint=?", (M,))
    _tick(t, clock, 1.30)
    _held(t, 0)                                               # the GMGN stop / owner already sold it
    _tick(t, clock, 1.00)
    assert not _sells(fc) and _row(c)["status"] == "open" and _row(c)["last_check"] == 0


def test_failed_sell_alerts_once_and_retries(monkeypatch):
    c, clock, t, fc = _promoted_limit(monkeypatch)
    _tick(t, clock, 1.30)

    async def boom(body):
        fc.calls.append(("swap", body)); raise gmgn.GmgnError(500, 500, "X", "down")
    fc.swap = boom
    _tick(t, clock, 1.00)
    _tick(t, clock, 1.00)                                     # inside the 15s retry wait: no new try
    assert len(_sells(fc)) == 1
    assert sum("sell failed" in m["text"] for m in c.notifier.sent) == 1
    _tick(t, clock, 1.00, dt=16)
    assert len(_sells(fc)) == 2
    assert sum("sell failed" in m["text"] for m in c.notifier.sent) == 1


def test_stale_peak_on_first_sight_does_not_sell(monkeypatch):
    c, clock, t, fc = _promoted_limit(monkeypatch)
    c.db.x("UPDATE gmgn_orders SET peak=1.60 WHERE mint=?", (M,))   # peak from before the desk trail existed
    _tick(t, clock, 1.02)                                     # 36% under the old peak: no surprise sell
    assert not _sells(fc)
    assert t._trail_peak[_row(c)["id"]] == 1.02
    assert _row(c)["id"] not in t._trail_armed
    _tick(t, clock, 1.30); _tick(t, clock, 1.03)              # re-arms from fresh samples, then trails
    assert len(_sells(fc)) == 1 and _row(c)["exit_reason"] == DESK_TRAIL


def test_armed_peak_on_first_sight_keeps_trailing(monkeypatch):
    c, clock, t, fc = _promoted_limit(monkeypatch)
    c.db.x("UPDATE gmgn_orders SET peak=1.60 WHERE mint=?", (M,))
    _tick(t, clock, 1.40)                                     # still above 1.60*0.8: keep the old peak, armed
    assert t._trail_peak[_row(c)["id"]] == 1.60 and _row(c)["id"] in t._trail_armed
    _tick(t, clock, 1.27)
    assert len(_sells(fc)) == 1


def test_unknown_balance_never_sells_and_manual_rows_skipped(monkeypatch):
    c, clock, t, fc = _promoted_limit(monkeypatch)
    _tick(t, clock, 1.30)
    t.c.net.rpc_map.pop("getTokenAccountsByOwner")

    async def nohold():
        raise RuntimeError("down")
    fc.wallet_holdings = nohold
    _tick(t, clock, 1.00)
    assert not _sells(fc)
    c.db.x("UPDATE gmgn_orders SET exit_reason='manual' WHERE mint=?", (M,))
    assert _tick(t, clock, 1.00, dt=20) == 0 and not _sells(fc)


def test_limit_stop_cancelled_reads_as_manual(monkeypatch):
    c, clock, t, fc = _promoted_limit(monkeypatch)
    o = _row(c)

    async def so(base_token=None, kind="open", group_tag="STMix"):
        if group_tag == "LimitOrder" and kind == "history":
            return {"list": [{"order_id": o["strategy_order_id"], "status": "canceled"}]}
        return {"list": []}
    fc.strategy_orders = so
    assert run(t.strategy_exit(o)) == "manual"
    assert EXIT_LABEL["manual"] == "sold"

def test_trail_floor_setting_defaults_and_hot_load():
    c, _ = make_ctx()
    assert c.s["risk.trail_floor_pct"] == 5
    assert "risk.trail_floor_pct" not in config.PROTECTED
    assert config.resolve("trail floor").key == "risk.trail_floor_pct"
    assert config.resolve("trail profit floor").key == "risk.trail_floor_pct"
    assert c.s.set("trail floor", "8", source="test") == "trail floor +5% -> +8%"
    c.s.set("risk.trail_floor_pct", 5, source="test")
    import pytest
    with pytest.raises(config.SettingError):
        c.s.set("risk.trail_floor_pct", -1, source="test")
    with pytest.raises(config.SettingError):
        c.s.set("risk.trail_floor_pct", 101, source="test")
    c.db.x("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
           ("risk.trail_floor_pct", "10"))
    c.db.x("INSERT INTO change_log(ts,key,old,new,source) VALUES(?,?,?,?,?)",
           (0, "risk.trail_floor_pct", "5", "10", "chat"))
    assert "risk.trail_floor_pct" in c.s.sync() and c.s["risk.trail_floor_pct"] == 10


def test_trail_trigger_is_max_of_drawdown_and_floor():
    c, _ = make_ctx(**{"risk.trail_pct": 20, "risk.trail_floor_pct": 5, "risk.trail_start_pct": 20})
    assert trail_floor_price(c.s, 1.0) == 1.05
    # just armed at +21%: raw trail is below entry, floor wins
    assert abs(trail_trigger(c.s, 1.0, 1.21) - 1.05) < 1e-12
    # +39% peak (JENSEN-shaped): raw trail +11.2% is already above the floor
    assert abs(trail_trigger(c.s, 1.0, 1.39) - 1.39 * 0.8) < 1e-12
    c.s.set("risk.trail_floor_pct", 15, source="test")
    assert abs(trail_trigger(c.s, 1.0, 1.39) - 1.15) < 1e-12   # live setting, floor now above the raw line


def test_desk_trail_never_sells_below_floor_and_stop_unchanged():
    c, _ = make_ctx(**{"risk.trail_start_pct": 20, "risk.trail_pct": 20, "risk.stop_pct": 50,
                       "risk.trail_floor_pct": 5})
    o = {"entry_price": 1.0, "peak": 1.0, "exits_phase": "normal", "ts": 0}
    assert desk_trail_check(c.s, o, 1.19, 10) == (None, 1.19, False)
    assert desk_trail_check(c.s, {**o, "peak": 1.19}, 1.05, 10)[0] is None   # not armed: floor does not sell
    r, peak, armed = desk_trail_check(c.s, o, 1.21, 10)                     # Rockstar-shaped: +21%
    assert armed and r is None and peak == 1.21
    o = {**o, "peak": 1.21}
    assert desk_trail_check(c.s, o, 1.06, 10)[0] is None                    # above the +5% floor
    assert desk_trail_check(c.s, o, 1.05, 10)[0] == DESK_TRAIL              # floor, not the raw -3% line
    # JENSEN-shaped +39% peak: sell at the 20% trail line (+11.2%), not at the floor
    o = {**o, "peak": 1.39}
    trig = trail_trigger(c.s, 1.0, 1.39)
    assert abs(trig - 1.39 * 0.8) < 1e-12
    assert desk_trail_check(c.s, o, trig + 0.001, 10)[0] is None
    assert desk_trail_check(c.s, o, trig, 10)[0] == DESK_TRAIL
    # -50% hard stop is unchanged, even when the trail never armed
    assert desk_trail_check(c.s, {"entry_price": 1.0, "peak": 1.0, "exits_phase": "normal", "ts": 0},
                            0.50, 10)[0] == DESK_STOP


def test_sim_exit_take_profit_trail_uses_floor():
    c, _ = make_ctx(**{"risk.trail_start_pct": 20, "risk.trail_pct": 20, "risk.stop_pct": 50,
                       "risk.trail_floor_pct": 5, "risk.timeout_h": 48, "risk.grace_min": 0})
    o = {"entry_price": 1.0, "peak": 1.21, "exits_phase": "normal", "ts": 0}
    assert sim_exit(c.s, o, 1.06, 10)[0] is None
    assert sim_exit(c.s, o, 1.05, 10)[0] == "profit_stop_trace"


def test_conditions_omit_profit_stop_trace():
    c, _ = make_ctx(**{"risk.stop_pct": 50, "risk.trail_pct": 20, "risk.trail_start_pct": 20, "risk.grace_min": 5})
    grace = {x["order_type"] for x in conditions(c.s, "grace")}
    normal = {x["order_type"] for x in conditions(c.s, "normal")}
    assert grace == {"loss_stop"}
    assert normal == {"loss_stop", "loss_stop_trace"}
    assert conditions(c.s, "normal")[0]["price_scale"] == "50"


def test_strip_server_trail_keeps_stop_and_phase(monkeypatch):
    monkeypatch.setattr(asyncio, "sleep", _nosleep)
    fc = GraceClient()
    c, clock, t = _live(fc, **{"risk.surge_sell_enabled": False})
    _held(t)
    run(t.on_pass(M, "Cat", "CAT", 1.0, 50000, 80))
    o = _row(c)
    stale = json.dumps([{"order_type": "loss_stop", "side": "sell", "price_scale": "30", "sell_ratio": "100"},
                        {"order_type": "profit_stop_trace", "side": "sell", "price_scale": "20",
                         "sell_ratio": "100", "drawdown_rate": "20"}])
    c.db.x("UPDATE gmgn_orders SET conditions=?, exits_phase='grace', strategy_order_id=? WHERE mint=?",
           (stale, "st-grace", M))
    o = _row(c)
    assert has_profit_stop_trace(o) and o["exits_phase"] == "grace"
    fc.calls.clear()
    assert run(t.strip_server_trails()) == 1
    o = _row(c)
    types = [x["order_type"] for x in json.loads(o["conditions"])]
    assert types == ["loss_stop"] and not has_profit_stop_trace(o)
    assert o["exits_phase"] == "grace"                         # not promoted early
    assert o["strategy_order_id"] == "st-new1"
    assert "trail_floor_strip" in (o["repair"] or "")
    creates = [b for k, b in fc.calls if k == "strategy_create"]
    cancels = [b for k, b in fc.calls if k == "strategy_cancel"]
    assert len(creates) == 1 and "profit_stop_trace" not in {x["order_type"] for x in creates[0]["condition_orders"]}
    assert creates[0]["condition_orders"][0]["order_type"] == "loss_stop"
    assert cancels and cancels[0]["order_id"] == "st-grace"
    assert run(t.strip_server_trails()) == 0                   # already clean


def test_strip_skips_limit_stop_only(monkeypatch):
    c, clock, t, fc = _promoted_limit(monkeypatch)
    stale = json.dumps([{"order_type": "loss_stop"}, {"order_type": "profit_stop_trace"}])
    c.db.x("UPDATE gmgn_orders SET conditions=? WHERE mint=?", (stale, M))
    fc.calls.clear()
    assert limit_stop_only(_row(c)) and has_profit_stop_trace(_row(c))
    assert run(t.strip_server_trails()) == 0
    assert not any(k == "strategy_create" for k, _ in fc.calls)
    assert _row(c)["exits_phase"] == "normal"
    assert LIMIT_STOP_MARK in _row(c)["repair"]
