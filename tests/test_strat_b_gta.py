"""DECISION 87: Strategy B GTA profile — name+spike entry, GTA exits, in-process kill switch."""
import pytest
import config, presets, strat_b, strat_b_gta as G, strat_b_live
from strat_b import StratB, entry_check, exit_check, b_stop_pct, b_exits_text, early_fields, TIME, TRAIL, STOP
from tests.helpers import make_ctx, pair, run
from tests.test_strat_b import _audit, _pair, _sb, _tick, _orders, M, M2

GTA = "Gta11111111111111111111111111111111111pump"
DOG = "Dog11111111111111111111111111111111111pump"


def _gta_sb(**kw):
    settings = {"strat_b.profile": "gta", "strat_b.enabled": True, "gmgn.fee_pct": 1.0}
    settings.update(kw)
    return _sb(**settings)


def _gta_watch(c, clock, sb, mint=GTA, name="GTA VI", symbol="GTAVI", **scan):
    if not c.db.one("SELECT mint FROM coins WHERE mint=?", (mint,)):
        c.db.x("INSERT INTO coins(mint,name,symbol,migrated_at,night) VALUES(?,?,?,?,?)",
               (mint, name, symbol, clock.t, c.night))
    sb.offer({"mint": mint, "name": name, "symbol": symbol, "ageMin": scan.get("ageMin", 0),
              "b_source": scan.get("b_source", "test")}, _audit(top10=90.0, holders=3, bundle_pct=80.0))
    return c.db.one("SELECT * FROM strat_b_watch WHERE mint=?", (mint,))


def _gta_pair(mint=GTA, price=1.0, **kw):
    p = _pair(mint, price=price, liq=kw.pop("liq", 5), mc=kw.pop("mc", 100), **kw)
    p["baseToken"] = {"address": mint, "name": "GTA VI", "symbol": "GTAVI"}
    return p



def _open_gta(c, clock, mint=GTA, name="GTA VI", symbol="GTAVI", entry=1.0, size=50.0, peak=None):
    """Insert a filled GTA paper row at a known entry (avoids paper-slippage from a tiny pool)."""
    now = clock.t
    peak = entry if peak is None else peak
    c.db.x("INSERT INTO strat_b_orders(ts,night,mint,name,symbol,status,size_usd,signal_price,entry_price,tokens,"
           "buy_fees_usd,peak,last_price,last_liq,entry_liq,mc_entry,trail_armed,opened_at,why,last_seen,mode,"
           "exits_phase,early_until,profile,profit_since,spike_high_at) "
           "VALUES(?,?,?,?,?,'open',?,?,?,?,0,?,?,?,?,?,0,?,?,?,'PAPER','normal',NULL,'gta',NULL,?)",
           (now, c.night, mint, name, symbol, size, entry, entry, size / entry,
            peak, entry, 50000, 50000, 200000, now, '{"reason":"test"}', now, now))
    return c.db.one("SELECT * FROM strat_b_orders WHERE mint=?", (mint,))

def _gta_tick(sb, clock, price, dt=5, mint=GTA, **kw):
    clock.t += dt
    return run(sb.tick(pairs=[_gta_pair(mint, price=price, **kw)]))


def test_gta_related_matches_and_false_positives():
    assert G.gta_related("GTA VI", "GTAVI")
    assert G.gta_related("Grand Theft Auto 6", "GTA6")
    assert G.gta_related("gta 6", None)
    assert G.gta_related("GTA V Coin", "GTAV")
    assert G.gta_related(None, "gta")
    assert G.gta_related("Rockstar Games", "RSTAR")
    assert G.gta_related("Vice City Pump", "VICE")
    assert G.gta_related("GTA6INU", "GTA6INU")
    assert not G.gta_related("GTable Finance", "GTABLE")
    assert not G.gta_related("Dogtag", "DOGTAG")
    assert not G.gta_related("Cat Coin", "CAT")
    assert not G.gta_related("", "")
    assert not G.gta_related(None, None)


def test_defaults_stay_on_standard_profile():
    c, _ = make_ctx()
    assert c.s["strat_b.profile"] == "default"
    assert c.s["strat_b.gta_stop_pct"] == 30
    assert c.s["strat_b.gta_tp_pct"] == 20
    assert c.s["strat_b.gta_profit_hold_s"] == 60
    assert c.s["strat_b.gta_spike_min_pct"] == 20
    assert c.s["strat_b.gta_spike_window_s"] == 60
    assert c.s["strat_b.gta_spike_drop_pct"] == 8
    assert c.s["strat_b.gta_spike_stall_s"] == 15
    assert c.s["strat_b.gta_kill_pct"] == 50
    assert "strat_b.profile" not in config.PROTECTED
    assert "strat_b.gta_kill_pct" not in config.PROTECTED
    # standard B still buys the pullback and ignores a GTA-looking name
    from tests.test_strat_b import _buy
    c, clock, sb = _sb()
    o = _buy(c, clock, sb)
    assert o["status"] == "open"
    assert (o["profile"] or "default") == "default"
    assert o["exits_phase"] == "early"
    assert b_stop_pct(c.s, o, clock.t) == c.s["strat_b.early_stop_pct"]



def test_gta_can_buy_ignores_daily_loss():
    c, clock, sb = _gta_sb(**{"strat_b.mode": "LIVE", "strat_b.live_daily_loss_usd": 1})
    c.db.x("INSERT INTO strat_b_orders(ts,night,mint,name,status,size_usd,entry_price,tokens,pnl_usd,mode,closed_at,profile) "
           "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
           (clock.t, c.night, "old", "Old", "closed", 50, 1, 50, -5000, "LIVE", clock.t, "gta"))
    assert sb.can_buy() is None
    c.s.set("strat_b.profile", "default", source="test")
    why = sb.can_buy()
    assert why and "daily loss" in why

def test_gta_offer_skips_scanner_filters_and_needs_a_spike():
    c, clock, sb = _gta_sb()
    w = _gta_watch(c, clock, sb)
    assert w is not None and w["status"] == "watching", w
    # no spike yet (flat price) — do not buy, even with tiny mc/liq and a terrible audit
    _gta_tick(sb, clock, 1.0, dt=1)
    assert not _orders(c)
    # same coin if it weren't GTA-named is skipped
    w2 = _gta_watch(c, clock, sb, mint=DOG, name="Dogtag", symbol="DOGTAG")
    assert w2["status"] == "skipped" and "not a GTA-related" in (w2["reason"] or "")


def test_gta_buys_only_on_an_active_spike_and_stamps_profile():
    c, clock, sb = _gta_sb()
    _gta_watch(c, clock, sb)
    # build a +20% spike in 60s: 1.0 -> 1.25, even in a $5 pool (no min-liq filter)
    _gta_tick(sb, clock, 1.0, dt=1, liq=5, mc=100)
    assert not _orders(c)
    _gta_tick(sb, clock, 1.25, dt=10, liq=5, mc=100)
    o = _orders(c)
    assert len(o) == 1 and o[0]["status"] == "open"
    assert o[0]["profile"] == "gta"
    assert o[0]["exits_phase"] == "normal" and o[0]["early_until"] is None
    assert o[0]["name"] == "GTA VI"
    assert b_stop_pct(c.s, o[0], clock.t) == 30
    assert "flat stop -30%" in b_exits_text(c.s, o[0], clock.t)
    assert TRAIL.lower() not in b_exits_text(c.s, o[0], clock.t).lower() or "no trail" in b_exits_text(c.s, o[0], clock.t).lower()


def test_gta_exits_tp_hold_stop_and_spike_over():
    # +20% net after 1% fees needs about +22.22% mark
    c, clock, sb = _gta_sb()
    _open_gta(c, clock)
    _gta_tick(sb, clock, 1.21, dt=3, liq=50000)
    assert _orders(c)[0]["status"] == "open"
    _gta_tick(sb, clock, 1.25, dt=3, liq=50000)
    o = _orders(c)[0]
    assert o["status"] == "closed" and o["exit_reason"] == G.GTA_TP

    # 60s in net profit (net>0 at ~+3% mark, well below +20% TP).
    # Hold stall/drop so the isolated hold clock is what fires, not spike-over.
    c, clock, sb = _gta_sb(**{"strat_b.gta_spike_stall_s": 600, "strat_b.gta_spike_drop_pct": 90})
    mint = GTA.replace("Gta", "Gtb")
    _open_gta(c, clock, mint=mint)
    clock.t += 5
    run(sb.tick(pairs=[_gta_pair(mint, 1.05, liq=50000)]))  # net ~2.95% > 0, starts the clock
    assert _orders(c)[0]["status"] == "open" and _orders(c)[0]["profit_since"]
    clock.t += 30
    run(sb.tick(pairs=[_gta_pair(mint, 1.00, liq=50000)]))  # dips to 0 net / below: clock resets
    assert _orders(c)[0]["status"] == "open" and _orders(c)[0]["profit_since"] is None
    clock.t += 5
    run(sb.tick(pairs=[_gta_pair(mint, 1.05, liq=50000)]))
    clock.t += 61
    run(sb.tick(pairs=[_gta_pair(mint, 1.05, liq=50000)]))
    assert _orders(c)[0]["status"] == "closed" and _orders(c)[0]["exit_reason"] == G.GTA_HOLD

    # flat -30% stop
    c, clock, sb = _gta_sb()
    mint = GTA.replace("Gta", "Gtc")
    _open_gta(c, clock, mint=mint)
    clock.t += 3
    run(sb.tick(pairs=[_gta_pair(mint, 0.70, liq=50000)]))
    o = _orders(c)[0]
    assert o["status"] == "closed" and o["exit_reason"] == STOP


def test_gta_spike_over_sells_only_in_net_profit():
    """If the spike ends while expected net after fees is <= 0, hold; sell only when net is still > 0."""
    c, clock, sb = _gta_sb()
    _open_gta(c, clock, peak=1.01)
    _gta_tick(sb, clock, 0.929, dt=3, liq=50000)   # 8%+ off 1.01, not in net profit, still above -30%
    o = _orders(c)[0]
    assert o["status"] == "open", o["exit_reason"]
    # in profit: peak 1.20, drop 8% to 1.104 — still net > 0, sell
    c, clock, sb = _gta_sb()
    mint = GTA.replace("Gta", "Gtd")
    _open_gta(c, clock, mint=mint, peak=1.20)
    clock.t += 3
    run(sb.tick(pairs=[_gta_pair(mint, 1.104, liq=50000)]))
    o = _orders(c)[0]
    assert o["status"] == "closed" and o["exit_reason"] == G.GTA_SPIKE


def test_gta_no_trail_or_time_stop():
    c, clock, sb = _gta_sb()
    _open_gta(c, clock)
    # stay around entry (not in net profit, not at the -30% stop) so hold/spike-over/TP do not fire
    _gta_tick(sb, clock, 1.00, dt=30, liq=50000)
    o = _orders(c)[0]
    assert o["status"] == "open" and o["trail_armed"] == 0
    hours = float(c.s["strat_b.time_stop_h"])
    _gta_tick(sb, clock, 1.00, dt=hours * 3600 + 60, liq=50000)
    o = _orders(c)[0]
    assert o["status"] == "open"
    assert (o["exit_reason"] or None) is None
    # no-price path must also not time-stop a GTA row
    clock.t += hours * 3600 + 60
    run(sb.tick(pairs=[]))  # opens are still managed; pair missing
    # tick with empty mints might skip manage if we pass pairs=[] and mint not in pairs
    # call _manage directly with no pair
    run(sb._manage(dict(_orders(c)[0]), None, clock.t))
    assert _orders(c)[0]["status"] == "open"

    # stamped GTA exits survive a profile switch back to default
    c.s.set("strat_b.profile", "default", source="test")
    o = dict(_orders(c)[0])
    reason, _ = exit_check(c.s, o, 0.70, 5, [], clock.t)
    assert reason == STOP
    assert G.is_gta_order(o, c.s)


def test_gta_live_stop_is_flat_30_no_early_window():
    c, _ = make_ctx(**{"strat_b.profile": "gta"})
    cond = strat_b_live.b_conditions(c.s)
    assert cond == [{"order_type": "loss_stop", "side": "sell", "price_scale": "30", "sell_ratio": "100"}]
    ef = early_fields(c.s, 1000.0, profile="gta")
    assert ef == {"exits_phase": "normal", "early_until": None}


def test_kill_switch_restores_standard_b_on_the_same_settings_instance():
    c, clock, sb = _gta_sb(**{"strat_b.mode": "PAPER", "strat_b.live_wallet_share_pct": 10,
                              "gmgn.live_wallet_share_pct": 90})
    c.gta_wallet_usd = 1000.0
    # first enable this night records the baseline
    assert run(G.ensure_baseline(c)) == 1000.0
    assert G.baseline_usd(c) == 1000.0
    assert c.db.meta(G.META_BASE_NIGHT) == c.night
    # -40% is not enough
    c.db.x("INSERT INTO strat_b_orders(ts,night,mint,name,status,size_usd,entry_price,last_price,tokens,pnl_usd,mode,profile,closed_at) "
           "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
           (clock.t, c.night, "x", "X", "closed", 50, 1, 0.2, 50, -400, "LIVE", "gta", clock.t))
    assert run(G.check_kill(c)) is None
    assert c.s["strat_b.profile"] == "gta" and c.s["strat_b.enabled"] is True
    # open GTA position + more realized: total <= -50% of 1000
    c.db.x("INSERT INTO strat_b_orders(ts,night,mint,name,status,size_usd,entry_price,last_price,tokens,mode,profile,opened_at) "
           "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
           (clock.t, c.night, "open_gta", "Open GTA", "open", 200, 1.0, 0.4, 200, "LIVE", "gta", clock.t))
    # realized -400 + unrealized 200*(0.4/1-1)=-120 → -520 <= -500
    s_id = id(c.s)
    fired = run(G.check_kill(c, notifier=c.notifier))
    assert fired and fired["fired"] is True
    assert id(c.s) == s_id
    assert c.s["strat_b.profile"] == "default"
    assert c.s["strat_b.enabled"] is True
    assert c.s["strat_b.mode"] == "LIVE"
    assert c.s["strat_b.live_wallet_share_pct"] == 50
    assert c.s["gmgn.live_wallet_share_pct"] == 50
    # protected keys changed on this same instance (not only the db)
    assert c.s._cache.get("strat_b.mode") == "LIVE" or c.s["strat_b.mode"] == "LIVE"
    open_rows = c.db.rows("SELECT * FROM strat_b_orders WHERE status='open'")
    assert len(open_rows) == 1 and open_rows[0]["profile"] == "gta"
    # later ticks must not force-sell it; GTA exits still apply
    assert G.is_gta_order(open_rows[0], c.s)
    notes = [n["text"] for n in c.notifier.sent]
    assert any("restored the saved standard Strat B" in n for n in notes)
    log = c.db.rows("SELECT key, new, source FROM change_log WHERE key='strat_b.gta_kill'")
    assert log and "kill switch" in (log[0]["source"] or "")


def test_paused_b_cannot_buy_gta():
    c, clock, sb = _gta_sb(**{"strat_b.enabled": False})
    _gta_watch(c, clock, sb)
    _gta_tick(sb, clock, 1.0, dt=1)
    _gta_tick(sb, clock, 1.50, dt=10)
    assert not _orders(c)
    # offer itself is a no-op when paused (no new watch)
    assert c.db.one("SELECT mint FROM strat_b_watch WHERE mint=?", (GTA,)) is None
