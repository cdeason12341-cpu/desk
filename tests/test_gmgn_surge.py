"""DECISION 49: surge-top sell (owner 2026-11-16 4:26 PM CT). While a GMGN position is open, a fast surge
(+25% within 180s, or +20% within 60s, above entry) arms it; a later 8% fall from the post-surge peak
market-sells the whole position (exit_reason 'surge top') and cancels the server-side exits."""
import asyncio
import config
from agents.gmgn_trader import EXIT_LABEL, SURGE_TOP, surge_check, trail_net_floor_price, trail_sol_in
from tests.helpers import make_ctx, pair, run
from tests.test_gmgn import M, trader
from tests.test_gmgn_grace import GraceClient, _held, _live, _nosleep


def _open(monkeypatch, fc=None, **settings):
    monkeypatch.setattr(asyncio, "sleep", _nosleep)
    fc = fc or GraceClient()
    c, clock, t = _live(fc, **settings)
    _held(t)
    r = run(t.on_pass(M, "Cat", "CAT", 1.0, 50000, 80))
    assert r["status"] == "open"
    fc.calls.clear()
    return c, clock, t, fc


def _tick(t, clock, price, dt=3):
    clock.t += dt
    return run(t.surge_once(pairs=[pair(M, price=price)]))


def _row(c):
    return c.db.one("SELECT * FROM gmgn_orders WHERE mint=?", (M,))


def _sells(fc):
    return [b for k, b in fc.calls if k == "swap" and b["input_token"] == M]


def test_settings_defaults_and_not_protected():
    c, _ = make_ctx()
    assert c.s["risk.surge_sell_enabled"] is True
    assert c.s["risk.surge_min_pct"] == 25 and c.s["risk.surge_window_s"] == 180 and c.s["risk.surge_top_drop_pct"] == 8
    for k in ("risk.surge_sell_enabled", "risk.surge_min_pct", "risk.surge_window_s", "risk.surge_top_drop_pct"):
        assert k not in config.PROTECTED
    assert EXIT_LABEL[SURGE_TOP] == "surge top"


def test_surge_then_top_sells_during_grace(monkeypatch):
    c, clock, t, fc = _open(monkeypatch)
    assert _row(c)["exits_phase"] == "grace"                  # first 5 minutes: the rule still works
    for p in (1.05, 1.15, 1.30):                               # +30% in ~9s
        _tick(t, clock, p)
    assert t._surge[_row(c)["id"]]["armed_at"] is not None
    _tick(t, clock, 1.40)                                      # new peak, still holding
    assert not _sells(fc)
    _tick(t, clock, 1.35)                                      # -3.6% from peak: hold
    assert not _sells(fc)
    _tick(t, clock, 1.28)                                      # -8.6% from 1.40: top -> sell
    assert len(_sells(fc)) == 1
    assert _sells(fc)[0]["input_amount_bps"] == "10000"       # the whole position
    o = _row(c)
    assert o["status"] == "exiting" and o["exit_reason"] == SURGE_TOP and o["last_check"] == 0
    assert "strategy_cancel" in [k for k, _ in fc.calls]
    assert fc.open_ids == []                                   # server-side exits cancelled
    assert any("surged +" in m["text"] and "topped" in m["text"] for m in c.notifier.sent)
    _tick(t, clock, 1.10)                                      # no second sell
    assert len(_sells(fc)) == 1


def test_fast_20pct_in_60s_also_arms(monkeypatch):
    c, clock, t, fc = _open(monkeypatch)
    clock.t += 400                                              # long after the buy (entry no longer in window)
    _tick(t, clock, 1.10, dt=1)
    _tick(t, clock, 1.20, dt=20)
    _tick(t, clock, 1.34, dt=20)                                # +21.8% from 1.10 in 40s, +34% vs entry
    assert t._surge[_row(c)["id"]]["armed_at"] is not None
    _tick(t, clock, 1.23)
    assert len(_sells(fc)) == 1


def test_slow_climb_never_sells(monkeypatch):
    c, clock, t, fc = _open(monkeypatch)
    p = 1.0
    for _ in range(120):                                        # +1% a minute for 2 hours, with dips
        p *= 1.01
        _tick(t, clock, p, dt=30)
        _tick(t, clock, p * 0.9, dt=15)
        _tick(t, clock, p, dt=15)
    assert not _sells(fc) and _row(c)["status"] == "open"
    assert t._surge[_row(c)["id"]].get("armed_at") is None


def test_surge_without_top_holds(monkeypatch):
    c, clock, t, fc = _open(monkeypatch)
    for p in (1.2, 1.4, 1.5, 1.48, 1.55, 1.6, 1.52):            # dips < 8% from the running peak
        _tick(t, clock, p)
    assert t._surge[_row(c)["id"]]["armed_at"] is not None
    assert not _sells(fc) and _row(c)["status"] == "open"


def test_below_entry_bounce_does_not_arm(monkeypatch):
    c, clock, t, fc = _open(monkeypatch)
    for p in (0.7, 0.75, 0.92, 0.84):                           # +31% off the low, but still under entry
        _tick(t, clock, p)
    assert not _sells(fc) and t._surge[_row(c)["id"]].get("armed_at") is None


def test_server_exit_already_fired_no_double_sell(monkeypatch):
    c, clock, t, fc = _open(monkeypatch)
    for p in (1.2, 1.4):
        _tick(t, clock, p)
    _held(t, 0)                                                 # GMGN's server-side exit sold it meanwhile
    _tick(t, clock, 1.2)
    assert not _sells(fc)
    o = _row(c)
    assert o["status"] == "open" and o["exit_reason"] in (None, "") and o["last_check"] == 0
    assert _row(c)["id"] not in t._surge


def test_already_exiting_or_sold_is_skipped(monkeypatch):
    c, clock, t, fc = _open(monkeypatch)
    for p in (1.2, 1.4):
        _tick(t, clock, p)
    c.db.x("UPDATE gmgn_orders SET status='exiting', exit_reason='server-side stop', sell_order_id='x1'")
    _tick(t, clock, 1.2)
    assert not _sells(fc) and _row(c)["exit_reason"] == "server-side stop"


def test_disabled_does_nothing(monkeypatch):
    c, clock, t, fc = _open(monkeypatch)
    c.s.set("risk.surge_sell_enabled", False, source="test")
    for p in (1.2, 1.5, 1.2):
        _tick(t, clock, p)
    assert not _sells(fc)


def test_paper_surge_top_closes(monkeypatch):
    monkeypatch.setattr(asyncio, "sleep", _nosleep)
    c, clock, t = trader()
    run(t.on_pass(M, "Cat", "CAT", 1.0, 50000, 80))
    assert _row(c)["status"] == "open" and _row(c)["mode"] == "PAPER"
    for p in (1.3, 1.5, 1.35):
        _tick(t, clock, p)
    o = _row(c)
    assert o["status"] == "closed" and o["exit_reason"] == SURGE_TOP


def test_surge_check_pure():
    c, _ = make_ctx()
    st = {}
    assert surge_check(c.s, st, 0, 1.0, 1.0) is None
    assert surge_check(c.s, st, 100, 1.26, 1.0) == "armed"      # +26% in 100s
    assert surge_check(c.s, st, 103, 1.30, 1.0) is None
    assert surge_check(c.s, st, 106, 1.196, 1.0) == "top"       # -8% from 1.30


def test_no_surge_opt_out_skips_only_that_order(monkeypatch):
    """Per-order no_surge=1 skips the surge-top watcher; the global setting stays on."""
    c, clock, t, fc = _open(monkeypatch)
    assert c.s["risk.surge_sell_enabled"] is True
    oid = _row(c)["id"]
    c.db.x("UPDATE gmgn_orders SET no_surge=1 WHERE id=?", (oid,))
    for p in (1.2, 1.5, 1.20):                                 # would sell if the rule were on
        n = _tick(t, clock, p)
    assert n == 0
    assert not _sells(fc) and _row(c)["status"] == "open" and _row(c)["exit_reason"] is None
    assert oid not in t._surge
    assert c.s["risk.surge_sell_enabled"] is True




# ---- DECISION 56: surge profit floor (owner 2026-11-16 6:45 PM CT: "Lock in at least +5% after a surge")
from agents.gmgn_trader import SURGE_FLOOR  # noqa: E402


def test_floor_setting_default_and_not_protected():
    c, _ = make_ctx()
    assert c.s["risk.surge_floor_pct"] == 5
    assert "risk.surge_floor_pct" not in config.PROTECTED
    assert config.resolve("surge floor").key == "risk.surge_floor_pct"
    assert EXIT_LABEL[SURGE_FLOOR] == "surge profit floor"


def _armed(c, peak=1.21):
    """Floor/top mechanics in isolation: the +25%-above-entry arming gate (DECISION 61) is off here."""
    c.s.set("risk.surge_min_entry_pct", 0, source="test")
    st = {}
    assert surge_check(c.s, st, 0, 1.0, 1.0) is None
    assert surge_check(c.s, st, 30, peak, 1.0) == "armed"
    return st


def test_surge_check_floor_fires_before_top():
    """Small surge (+21%). A jump under the net floor holds; a bounce back to the net floor sells as floor."""
    c, _ = make_ctx()
    st = _armed(c)
    floor_px = trail_net_floor_price(c.s, {"entry_price": 1.0})
    assert floor_px > 1.05
    assert surge_check(c.s, st, 33, 1.15, 1.0) is None          # -5% from peak, still above the net floor and top
    assert surge_check(c.s, st, 36, 1.049, 1.0) is None         # under the net floor: hold, hope for a bounce
    assert st.get("below_floor") is True
    assert surge_check(c.s, st, 39, floor_px, 1.0) == "floor"


def test_surge_check_floor_alone_without_8pct_drop():
    """A gap under the net floor, then a bounce back to it, sells as the floor even if the 8% top hasn't
    been the last print (the bounce lands on the floor line)."""
    c, _ = make_ctx()
    st = _armed(c, peak=1.32)
    floor_px = trail_net_floor_price(c.s, {"entry_price": 1.0})
    assert surge_check(c.s, st, 33, 1.31, 1.0) is None          # only -0.8% from peak: hold
    assert surge_check(c.s, st, 36, 0.98, 1.0) is None          # under the net floor: hold
    assert st.get("below_floor") is True
    assert surge_check(c.s, st, 39, floor_px, 1.0) == "floor"


def test_surge_check_top_first_when_peak_is_high():
    c, _ = make_ctx()
    st = _armed(c, peak=1.25)
    for t, p in ((33, 1.4), (36, 1.6)):
        assert surge_check(c.s, st, t, p, 1.0) is None
    assert surge_check(c.s, st, 39, 1.47, 1.0) == "top"         # -8.1% from 1.60, still +47%


def test_surge_check_under_entry_holds_for_bounce():
    """Fast crash past both lines to under entry: no surge sell; a bounce to the net floor sells."""
    c, _ = make_ctx()
    st = _armed(c)
    peak = st["peak"]
    floor_px = trail_net_floor_price(c.s, {"entry_price": 1.0})
    assert surge_check(c.s, st, 33, 0.95, 1.0) is None
    assert st.get("below_floor") is True and st["peak"] == peak
    assert surge_check(c.s, st, 36, 1.0, 1.0) is None           # back at entry, still under the net floor
    assert surge_check(c.s, st, 39, floor_px, 1.0) == "floor"
    assert st["peak"] == peak


def test_surge_check_floor_off():
    c, _ = make_ctx()
    c.s.set("risk.surge_floor_pct", 0, source="test")
    st = _armed(c, peak=1.21)
    assert surge_check(c.s, st, 33, 1.12, 1.0) is None          # +12%: above the 8% line (1.1132), no floor
    assert surge_check(c.s, st, 36, 1.11, 1.0) == "top"


def test_live_floor_sells_whole_position(monkeypatch):
    """After a surge, a gap under the net floor holds; a bounce back to the net floor market-sells."""
    c, clock, t, fc = _open(monkeypatch)
    for p in (1.1, 1.26):                                       # +26%: armed
        _tick(t, clock, p)
    assert t._surge[_row(c)["id"]]["armed_at"] is not None
    peak = t._surge[_row(c)["id"]]["peak"]
    _tick(t, clock, 1.22)                                       # small dip, still above the top line: hold
    assert not _sells(fc)
    _tick(t, clock, 0.98)                                       # gapped under the net floor: hold
    assert not _sells(fc)
    assert t._surge[_row(c)["id"]]["below_floor"] and t._surge[_row(c)["id"]]["peak"] == peak
    floor_px = trail_net_floor_price(c.s, _row(c))
    _tick(t, clock, floor_px)                                   # bounce to the net floor
    assert len(_sells(fc)) == 1 and _sells(fc)[0]["input_amount_bps"] == "10000"
    o = _row(c)
    assert o["status"] == "exiting" and o["exit_reason"] == SURGE_FLOOR and o["last_check"] == 0
    assert fc.open_ids == []                                    # server-side exits cancelled
    assert any("profit floor" in m["text"] for m in c.notifier.sent)
    _tick(t, clock, 1.05)
    assert len(_sells(fc)) == 1                                 # never a second sell


def test_live_default_floor_on_small_surge(monkeypatch):
    """+30% peak, then a slide. With the default +5% floor and 8% drop, the drop line (+19.6%) comes first
    and sells as 'surge top' while still in profit."""
    c, clock, t, fc = _open(monkeypatch)
    clock.t += 400
    _tick(t, clock, 1.05, dt=1)
    _tick(t, clock, 1.30, dt=20)                                # +23.8% in 20s, +30% vs entry
    assert t._surge[_row(c)["id"]]["armed_at"] is not None
    _tick(t, clock, 1.19)
    assert len(_sells(fc)) == 1 and _row(c)["exit_reason"] == SURGE_TOP


def test_live_crash_under_entry_leaves_it_to_stops(monkeypatch):
    """Both lines missed (price already under entry): no surge sell, exits stay on, the order stays open."""
    c, clock, t, fc = _open(monkeypatch)
    for p in (1.1, 1.3):
        _tick(t, clock, p)
    assert t._surge[_row(c)["id"]]["armed_at"] is not None
    _tick(t, clock, 0.8)                                        # crashed straight through floor and top
    assert not _sells(fc)
    o = _row(c)
    assert o["status"] == "open" and o["exit_reason"] is None
    assert "strategy_cancel" not in [k for k, _ in fc.calls]
    _tick(t, clock, 0.75)
    assert not _sells(fc)


def test_paper_floor_closes(monkeypatch):
    monkeypatch.setattr(asyncio, "sleep", _nosleep)
    c, clock, t = trader()
    run(t.on_pass(M, "Cat", "CAT", 1.0, 50000, 80))
    for p in (1.3, 1.32):
        _tick(t, clock, p)
    _tick(t, clock, 0.98)                                       # under the net floor: hold
    assert _row(c)["status"] == "open"
    floor_px = trail_net_floor_price(c.s, _row(c))
    _tick(t, clock, floor_px)
    o = _row(c)
    assert o["status"] == "closed" and o["exit_reason"] == SURGE_FLOOR


def test_no_surge_opt_out_also_skips_floor(monkeypatch):
    c, clock, t, fc = _open(monkeypatch)
    c.db.x("UPDATE gmgn_orders SET no_surge=1 WHERE id=?", (_row(c)["id"],))
    for p in (1.2, 1.5, 1.04):
        _tick(t, clock, p)
    assert not _sells(fc) and _row(c)["status"] == "open"


# ---- DECISION 61: a surge counts only once price is >= +25% above entry (owner 2026-11-16 7:38 PM CT)
def test_min_entry_setting_default_and_not_protected():
    c, _ = make_ctx()
    assert c.s["risk.surge_min_entry_pct"] == 25
    assert "risk.surge_min_entry_pct" not in config.PROTECTED
    assert config.resolve("surge min entry").key == "risk.surge_min_entry_pct"
    assert c.s["risk.surge_floor_pct"] == 5 and c.s["risk.surge_top_drop_pct"] == 8   # unchanged


def test_surge_check_needs_25pct_above_entry():
    """Share Bags shape: +26.9% inside the window but only +8.9% above entry -> not a surge."""
    c, _ = make_ctx()
    st = {}
    assert surge_check(c.s, st, 0, 0.858, 1.0) is None          # dipped under entry
    assert surge_check(c.s, st, 60, 1.089, 1.0) is None         # +26.9% off the low, +8.9% vs entry: no
    assert st.get("armed_at") is None
    assert surge_check(c.s, st, 70, 1.249, 1.0) is None         # +24.9% vs entry: still no
    assert surge_check(c.s, st, 80, 1.25, 1.0) == "armed"       # +25% vs entry and +45.7% off the low: armed


def test_surge_check_entry_gate_alone_is_not_enough():
    """Slow grind to +40% above entry with no fast rise in either window: never armed."""
    c, _ = make_ctx()
    st, p = {}, 1.0
    for i in range(1, 200):
        p *= 1.002                                              # +0.2% per 10s ~ +12% per 10 min
        assert surge_check(c.s, st, i * 10, p, 1.0) is None
    assert p > 1.4 and st.get("armed_at") is None


def test_live_share_bags_shape_does_not_arm_or_sell(monkeypatch):
    c, clock, t, fc = _open(monkeypatch)
    clock.t += 400
    for p in (0.86, 0.95, 1.089, 1.06, 1.03, 1.013):            # +26.9% off the low, top +8.9%, slide to +1.3%
        _tick(t, clock, p, dt=10)
    assert t._surge[_row(c)["id"]].get("armed_at") is None
    assert not _sells(fc) and _row(c)["status"] == "open"


def test_live_entry_gate_off_restores_old_arming(monkeypatch):
    c, clock, t, fc = _open(monkeypatch)
    c.s.set("risk.surge_min_entry_pct", 0, source="test")
    clock.t += 400
    for p in (0.86, 1.089):
        _tick(t, clock, p, dt=10)
    assert t._surge[_row(c)["id"]]["armed_at"] is not None

# ---- DECISION 82: surge uses the same net floor as the trail (owner 2026-11-18 4:37 AM CT)
def test_surge_top_below_net_floor_holds_then_bounces():
    """An 8% top that would fill under the net floor does not sell; a bounce to the net floor does."""
    c, _ = make_ctx(**{"gmgn.fee_pct": 1.0, "gmgn.priority_fee_sol": 0, "gmgn.tip_fee_sol": 0,
                       "risk.trail_floor_pct": 5})
    c.s.set("risk.surge_min_entry_pct", 0, source="test")
    st = {}
    assert surge_check(c.s, st, 0, 1.0, 1.0) is None
    assert surge_check(c.s, st, 30, 1.21, 1.0) == "armed"
    floor_px = trail_net_floor_price(c.s, {"entry_price": 1.0})
    assert floor_px > 1.05
    assert surge_check(c.s, st, 33, 1.02, 1.0) is None          # 8%+ off the peak but under the net floor
    assert st.get("below_floor") is True and st["peak"] == 1.21
    assert surge_check(c.s, st, 36, floor_px, 1.0) == "floor"


def test_mark_plus_five_is_not_enough_after_fees():
    c, _ = make_ctx(**{"gmgn.fee_pct": 1.0, "gmgn.priority_fee_sol": 0, "gmgn.tip_fee_sol": 0})
    st = _armed(c)
    from agents.gmgn_trader import trail_net_ok
    assert not trail_net_ok(c.s, {"entry_price": 1.0}, 1.05)
    assert surge_check(c.s, st, 33, 1.05, 1.0) is None
    assert st.get("below_floor") is True


def test_strong_bounce_above_top_line_keeps_trailing():
    c, _ = make_ctx(**{"gmgn.fee_pct": 0, "gmgn.priority_fee_sol": 0, "gmgn.tip_fee_sol": 0,
                       "risk.trail_floor_pct": 5})
    st = _armed(c, peak=1.40)
    assert surge_check(c.s, st, 33, 0.90, 1.0) is None
    assert st.get("below_floor") is True
    # 1.35 is above the 8% top line (1.40*0.92=1.288) and above the net floor: resume trailing
    assert surge_check(c.s, st, 36, 1.35, 1.0) is None
    assert st.get("below_floor") is False
    assert st["peak"] == 1.40
    assert surge_check(c.s, st, 39, 1.288, 1.0) == "top"


def test_live_quote_under_net_floor_blocks_surge_sell(monkeypatch):
    c, clock, t, fc = _open(monkeypatch)
    fc.plan_blocked = False
    for p in (1.1, 1.30):
        _tick(t, clock, p)
    sol_in = trail_sol_in(c.s, _row(c))
    assert sol_in and sol_in > 0

    async def cheap_quote(input_token, output_token, amount, slippage):
        fc.calls.append(("quote", (input_token, output_token, amount, slippage)))
        return {"output_amount": str(int(sol_in * 1.01 * 1e9)), "min_output_amount": "1"}
    fc.quote = cheap_quote
    _tick(t, clock, 1.19)                                       # would be a top sell on the mark
    assert not _sells(fc)
    assert t._surge[_row(c)["id"]].get("below_floor")

    async def good_quote(input_token, output_token, amount, slippage):
        fc.calls.append(("quote", (input_token, output_token, amount, slippage)))
        return {"output_amount": str(int(sol_in * 1.06 * 1e9)), "min_output_amount": "1"}
    fc.quote = good_quote
    floor_px = trail_net_floor_price(c.s, _row(c))
    _tick(t, clock, floor_px)
    assert len(_sells(fc)) == 1 and _row(c)["exit_reason"] == SURGE_FLOOR
