"""DECISION 54: Strategy B · PAPER ONLY. Own filters, own sizing, own exits, own table/log; never a real order."""
import json, pathlib
import pytest
import config, strat_b
from strat_b import StratB, entry_check, exit_check, fill_buy, fill_sell, static_check, STOP, TP, SPIKE, TRAIL, TIME
from tests.helpers import make_ctx, pair, run

M = "StrB1111111111111111111111111111111111pump"
M2 = "StrB2222222222222222222222222222222222pump"


def _audit(top10=15.0, holders=150, bundle_pct=0.0):
    return {"passed": True, "holders": holders, "bundle_pct": bundle_pct, "bundled": 0,
            "layers": [{"n": 4, "name": "Top 10 holders", "pass": True, "value": f"{top10}%"}]}


def _pair(mint=M, price=1.0, liq=50000, mc=200000, vol24=300000, buys=10, sells=5):
    p = pair(mint, price=price, liq=liq, buys_m5=buys, sells_m5=sells)
    p["marketCap"] = mc
    p["volume"]["h24"] = vol24
    return p


def _sb(**settings):
    c, clock = make_ctx(**settings)
    sb = StratB(c, log_files=False)
    c.strat_b = sb
    return c, clock, sb


def _watch(c, clock, sb, mint=M, age_min=10, **audit):
    c.db.x("INSERT INTO coins(mint,name,symbol,migrated_at,night) VALUES(?,?,?,?,?)", (mint, "Cat", "CAT", clock.t - age_min * 60, c.night))
    sb.offer({"mint": mint, "name": "Cat", "symbol": "CAT", "ageMin": age_min}, _audit(**audit))
    return c.db.one("SELECT * FROM strat_b_watch WHERE mint=?", (mint,))


def _tick(sb, clock, price, dt=10, mint=M, **kw):
    clock.t += dt
    return run(sb.tick(pairs=[_pair(mint, price=price, **kw)]))


def _orders(c):
    return c.db.rows("SELECT * FROM strat_b_orders ORDER BY id")


def _buy(c, clock, sb):
    """Watch from age 10m, high 1.0, wait to 60m, dip to 0.80, bounce to 0.85 -> buy."""
    _watch(c, clock, sb)
    _tick(sb, clock, 1.0)
    clock.t += 50 * 60
    _tick(sb, clock, 0.80)
    assert not _orders(c)
    _tick(sb, clock, 0.85)
    o = _orders(c)
    assert len(o) == 1 and o[0]["status"] == "open"
    return o[0]


# ---------- settings / safety
def test_settings_namespace_mode_protected():
    """DECISION 66: strat_b.mode is PAPER|LIVE, default PAPER, force-only (owner command), like gmgn.mode."""
    c, _ = make_ctx()
    assert c.s["strat_b.enabled"] is True and c.s["strat_b.mode"] == "PAPER"
    assert config.BY_KEY["strat_b.mode"].choices == ("PAPER", "LIVE")
    assert "strat_b.mode" in config.PROTECTED
    with pytest.raises(config.SettingError):
        c.s.set("strat_b.mode", "LIVE")                      # protected: not with a plain set
    with pytest.raises(config.SettingError):
        c.s.set("strat_b.mode", "PAPER")
    with pytest.raises(config.SettingError):
        c.s.set("strat_b.mode", "MOON", force=True)          # only the two choices
    c.s.set("strat_b.mode", "LIVE", source="test", force=True)
    assert c.s["strat_b.mode"] == "LIVE"
    for bare, key in (("stop", "risk.stop_pct"), ("stop pct", "risk.stop_pct"), ("trail", "risk.trail_pct"),
                      ("max open", "gmgn.max_open"), ("trail start", "risk.trail_start_pct"), ("enabled", "auditor.enabled")):
        assert config.resolve(bare).key == key            # B never takes A's bare names
    assert config.resolve("b stop").key == "strat_b.stop_pct"
    assert config.resolve("strat b trade usd").key == "strat_b.trade_usd"


def _imports(path):
    import ast
    src = pathlib.Path(path).read_text()
    mods = set()
    for n in ast.walk(ast.parse(src)):
        if isinstance(n, ast.Import):
            mods |= {a.name for a in n.names}
        elif isinstance(n, ast.ImportFrom):
            mods.add(n.module)
    return src, mods


def test_module_reaches_gmgn_only_through_strat_b_live():
    """DECISION 66: strat_b.py (entry + paper) still never imports GMGN / wallet / secrets code; the only path to a real
    order is strat_b_live, which uses Strategy A's client and never touches keys."""
    src, mods = _imports(strat_b.__file__)
    assert mods <= {"__future__", "asyncio", "collections", "json", "time", "fields", "links", "ctx", "agents.scanner",
                    "strat_b_live", "agents"}, mods
    code = src.split('"""', 2)[2]
    for bad in ("import gmgn", "gmgn_trader", "import solana", "secrets_store", "keypair", "private", "swap(", "notifier.send"):
        assert bad not in code, bad
    import strat_b_live
    lsrc, lmods = _imports(strat_b_live.__file__)
    assert "solana" not in lmods and "crypto" not in lmods, lmods
    lcode = lsrc.split('"""', 2)[2].lower()
    for bad in ("keypair", "private_key", "seed", "signing_key", "gmgn_api_key", "notifier.send"):
        assert bad not in lcode, bad


def test_a_settings_unchanged_by_b():
    c, clock, sb = _sb()
    before = {k: v for k, v in c.s.as_dict().items() if not k.startswith("strat_b.")}
    _buy(c, clock, sb)
    after = {k: v for k, v in c.s.as_dict().items() if not k.startswith("strat_b.")}
    assert before == after
    assert c.db.one("SELECT COUNT(*) n FROM gmgn_orders")["n"] == 0
    assert c.db.one("SELECT COUNT(*) n FROM positions")["n"] == 0


# ---------- entry
def test_static_filters():
    c, _ = make_ctx()
    assert static_check(c.s, _audit()) is None
    assert "top 10" in static_check(c.s, _audit(top10=26))
    assert "holders" in static_check(c.s, _audit(holders=50))
    assert "bundled" in static_check(c.s, _audit(bundle_pct=8))
    assert static_check(c.s, {"layers": []}) == "no top-10 data"


def test_offer_skips_and_watches():
    c, clock, sb = _sb()
    w = _watch(c, clock, sb, top10=26)
    assert w["status"] == "skipped" and "top 10" in w["reason"]
    w2 = _watch(c, clock, sb, mint=M2)
    assert w2["status"] == "watching"
    sb.offer({"mint": M2, "name": "Cat"}, _audit())            # once per coin
    assert c.db.one("SELECT COUNT(*) n FROM strat_b_watch")["n"] == 2


def test_offer_never_raises():
    c, clock, sb = _sb()
    sb.offer(None, None)
    assert c.db.one("SELECT COUNT(*) n FROM errors WHERE agent='strat_b'")["n"] == 1


def test_entry_rules():
    c, clock = make_ctx()
    s, now = c.s, clock.t
    w = {"grad_at": now - 90 * 60, "high": 1.0, "low": 0.80}
    ok, why, _ = entry_check(s, w, _pair(price=0.85), now)
    assert ok, why
    assert not entry_check(s, dict(w, grad_at=now - 20 * 60), _pair(price=0.85), now)[0]          # first hour: wait
    assert "pullback" in entry_check(s, dict(w, low=0.95), _pair(price=0.95), now)[1]              # only -5%
    assert "too far" in entry_check(s, dict(w, low=0.5), _pair(price=0.55), now)[1]                # -45%
    assert "bouncing" in entry_check(s, dict(w, low=0.84), _pair(price=0.85), now)[1]
    assert "sellers" in entry_check(s, w, _pair(price=0.85, buys=3, sells=9), now)[1]
    assert "volume" in entry_check(s, w, _pair(price=0.85, vol24=50000), now)[1]
    assert "mc" in entry_check(s, w, _pair(price=0.85, mc=2_000_000), now)[1]
    assert "mc" in entry_check(s, w, _pair(price=0.85, mc=20_000), now)[1]
    assert "liquidity" in entry_check(s, w, _pair(price=0.85, liq=5000), now)[1]


def test_buy_waits_for_pullback_and_bounce_and_pings_labelled():
    c, clock, sb = _sb()
    o = _buy(c, clock, sb)
    assert o["size_usd"] == 50 and o["signal_price"] == 0.85
    assert o["entry_price"] > 0.85                              # paid slippage
    assert o["buy_fees_usd"] == pytest.approx(0.75)            # 1% + $0.25
    sent = c.notifier.sent
    assert len(sent) == 1 and sent[0]["text"].startswith("STRATEGY B · PAPER · Bought Cat")
    assert "r=savipww" in sent[0]["buttons"][0][0]["url"]
    assert c.db.one("SELECT status FROM strat_b_watch WHERE mint=?", (M,))["status"] == "bought"


def test_new_high_resets_pullback_low():
    c, clock, sb = _sb()
    _watch(c, clock, sb, age_min=70)
    _tick(sb, clock, 1.0)
    _tick(sb, clock, 0.9)
    _tick(sb, clock, 1.2)                                       # new high: low restarts
    w = c.db.one("SELECT * FROM strat_b_watch WHERE mint=?", (M,))
    assert w["high"] == 1.2 and w["low"] == 1.2


def test_one_buy_per_coin_and_max_open_and_daily_loss_stop():
    c, clock, sb = _sb(**{"strat_b.max_open": 1})
    _buy(c, clock, sb)
    _watch(c, clock, sb, mint=M2, age_min=90)
    clock.t += 10
    run(sb.tick(pairs=[_pair(M, price=0.86), _pair(M2, price=1.0)]))
    clock.t += 10
    run(sb.tick(pairs=[_pair(M, price=0.86), _pair(M2, price=0.80)]))
    clock.t += 10
    run(sb.tick(pairs=[_pair(M, price=0.86), _pair(M2, price=0.85)]))
    assert len(_orders(c)) == 1                                  # max open 1
    c.db.x("UPDATE strat_b_orders SET status='closed', pnl_usd=-101, closed_at=?", (clock.t,))   # -10.1% of $1,000 tonight
    assert "daily loss stop" in sb.can_buy()
    c.db.x("UPDATE strat_b_orders SET night='old'")
    assert sb.can_buy() is None


def test_disabled_does_not_watch_or_buy_but_manages_open():
    c, clock, sb = _sb()
    _buy(c, clock, sb)
    c.s.set("strat_b.enabled", False, source="test")
    _watch(c, clock, sb, mint=M2)
    assert c.db.one("SELECT * FROM strat_b_watch WHERE mint=?", (M2,)) is None
    _tick(sb, clock, 0.30)                                      # open position still gets its stop
    assert _orders(c)[0]["exit_reason"] == STOP


# ---------- fills
def test_fills_slippage_and_fees():
    c, _ = make_ctx()
    b = fill_buy(c.s, 1.0, 50, 50000)                           # 1% + 2*50/50000 = 1.2%
    assert b["price"] == pytest.approx(1.012) and b["fees"] == pytest.approx(0.75)
    assert b["tokens"] == pytest.approx(49.25 / 1.012)
    sl = fill_sell(c.s, 1.0, b["tokens"], 50000)
    assert sl["price"] < 1.0 and sl["proceeds"] < 49.25
    assert sl["proceeds"] - 50 < -2                             # a flat round trip costs ~4.5%


# ---------- exits
def _o(e=1.0, **kw):
    return dict({"entry_price": e, "peak": e, "trail_armed": 0, "opened_at": 0.0, "entry_liq": 50000}, **kw)


def test_exit_rules():
    c, _ = make_ctx()
    s = c.s
    assert exit_check(s, _o(), 0.64, 50000, [(100, 0.64)], 100)[0] == STOP
    assert exit_check(s, _o(), 0.70, 50000, [(100, 0.70)], 100)[0] is None          # -30%: holds (A would be out at -20%)
    assert exit_check(s, _o(), 2.01, 50000, [(100, 2.01)], 100)[0] == TP
    assert exit_check(s, _o(), 1.40, 50000, [(50, 1.05), (100, 1.40)], 100)[0] == SPIKE   # +33% in 50s
    assert exit_check(s, _o(), 1.40, 50000, [(100, 1.40)], 100)[0] is None          # slow climb: no spike
    r, upd = exit_check(s, _o(), 1.55, 50000, [(100, 1.55)], 100)
    assert r is None and upd["trail_armed"] == 1 and upd["peak"] == 1.55
    assert exit_check(s, _o(peak=1.55, trail_armed=1), 1.30, 50000, [(200, 1.30)], 200)[0] is None
    assert exit_check(s, _o(peak=1.55, trail_armed=1), 1.23, 50000, [(200, 1.23)], 200)[0] == TRAIL
    assert exit_check(s, _o(peak=1.3), 1.05, 50000, [(200, 1.05)], 200)[0] is None   # +30% peak: trail never armed
    assert exit_check(s, _o(), 1.0, 50000, [(86400, 1.0)], 86400)[0] == TIME
    assert exit_check(s, _o(), 1.0, 5000, [(100, 1.0)], 100)[0] == "liquidity pulled"


def test_sell_closes_once_with_pnl_and_ping():
    c, clock, sb = _sb()
    o = _buy(c, clock, sb)
    clock.t += 120
    _tick(sb, clock, o["entry_price"] * 2.05)                    # 2x: sell everything
    o = _orders(c)[0]
    assert o["status"] == "closed" and o["exit_reason"] == TP and o["pnl_usd"] > 40
    assert c.notifier.sent[-1]["text"].startswith("STRATEGY B · PAPER · Sold Cat (take profit 2x) · +$")
    _tick(sb, clock, 0.1)
    assert len(c.notifier.sent) == 2                             # buy + sell only
    assert sb.bankroll() == pytest.approx(1000 + o["pnl_usd"])


def test_restart_rebuilds_from_table():
    c, clock, sb = _sb()
    _buy(c, clock, sb)
    sb2 = StratB(c, log_files=False)                             # fresh process: state comes from strat_b_orders
    clock.t += 10
    run(sb2.tick(pairs=[_pair(M, price=0.4)]))
    assert _orders(c)[0]["exit_reason"] == STOP


# ---------- publisher / app
def test_scorecard_and_publisher_files():
    from publisher import Builder
    from tests.test_publisher_gmgn import _desk
    d, c, clock, t = _desk(mode="PAPER")
    sb = StratB(c, log_files=False)
    c.strat_b = sb
    _buy(c, clock, sb)
    _watch(c, clock, sb, mint=M2, top10=40)
    files = Builder(d).build()
    card = files["strat_b.json"]
    assert card["mode"] == "PAPER" and card["label"] == "STRATEGY B · PAPER"
    assert len(card["open"]) == 1 and card["open"][0]["pnlUsd"] < 0 and "r=savipww" in card["open"][0]["link"]
    assert card["skipped"] == 1 and card["bankroll"] == 1000 and card["rules"]
    st = files["state.json"]["stratB"]
    assert st["openCount"] == 1 and st["mode"] == "PAPER"
    tr = files["trades.json"]                                    # A's real/paper lists never include B
    assert not any(r.get("mint") == M for r in tr["real"]["open"] + tr["paper"]["open"])


def test_desk_wires_strat_b(tmp_path):
    import main
    d = main.Desk(db_path=str(tmp_path / "d.db"), log_files=False, with_telegram=False)
    assert d.ctx.strat_b is d.strat_b and d.strat_b.c is d.ctx
    assert main.HB_JOBS["strat_b"] == ("strat_b",)
    assert not hasattr(d.strat_b, "client")


def test_auditor_hands_passed_coins_to_b(monkeypatch):
    import asyncio
    from agents.auditor import Auditor
    c, clock, sb = _sb()
    seen = []
    sb.offer = lambda scan, audit: seen.append(scan["mint"])
    a = Auditor(c)

    async def fake_run(mint, scan):
        return {"passed": mint == M, "mint": mint}
    a.run = fake_run

    async def go():
        c.bus.auditor.put_nowait({"mint": M})
        c.bus.auditor.put_nowait({"mint": M2})
        task = asyncio.create_task(a.loop())
        for _ in range(20):
            await asyncio.sleep(0)
        task.cancel()
    run(go())
    assert seen == [M]
    assert c.bus.analyst.qsize() == 1                            # A's analyst still gets the passed coin


# ---------- DECISION 69: live daily-loss setting + watchlist hygiene
def test_live_daily_loss_usd_setting_not_protected():
    c, _ = make_ctx()
    assert c.s["strat_b.live_daily_loss_usd"] == 20.0
    assert "strat_b.live_daily_loss_usd" not in config.PROTECTED
    c.s.set("strat_b.live_daily_loss_usd", 25, source="test")
    assert c.s["strat_b.live_daily_loss_usd"] == 25.0
    paper = [r for r in strat_b.rules(c.s) if r["k"] == "Daily loss stop"][0]
    assert "10%" in paper["v"] and "bankroll" in paper["v"]
    c.s.set("strat_b.mode", "LIVE", source="test", force=True)
    live = [r for r in strat_b.rules(c.s) if r["k"] == "Daily loss stop"][0]
    assert "$25" in live["v"] and "real P&L" in live["v"]


def test_watch_expires_after_ten_under_floor_evals():
    c, clock, sb = _sb()
    _watch(c, clock, sb, age_min=90)
    for i in range(9):
        _tick(sb, clock, 1.0, mc=10_000)
        w = c.db.one("SELECT status, under_floor_evals FROM strat_b_watch WHERE mint=?", (M,))
        assert w["status"] == "watching" and w["under_floor_evals"] == i + 1
    _tick(sb, clock, 1.0, mc=10_000)
    w = c.db.one("SELECT * FROM strat_b_watch WHERE mint=?", (M,))
    ev = c.db.one("SELECT * FROM strat_b_evals ORDER BY id DESC LIMIT 1")
    assert w["status"] == "expired" and w["under_floor_evals"] == 10
    assert ev["kind"] == "expired" and "under $30,000 for 10 evals" in ev["reason"]


def test_watch_expires_after_15m_under_floor():
    c, clock, sb = _sb()
    _watch(c, clock, sb, age_min=90)
    _tick(sb, clock, 1.0, mc=10_000)
    assert c.db.one("SELECT status FROM strat_b_watch WHERE mint=?", (M,))["status"] == "watching"
    clock.t += 15 * 60 - 10
    _tick(sb, clock, 1.0, mc=10_000)                          # held == 15m, only 2 evals
    ev = c.db.one("SELECT * FROM strat_b_evals ORDER BY id DESC LIMIT 1")
    assert c.db.one("SELECT status FROM strat_b_watch WHERE mint=?", (M,))["status"] == "expired"
    assert ev["kind"] == "expired" and "for 15m" in ev["reason"]


def test_watch_under_floor_resets_when_mc_recovers():
    c, clock, sb = _sb()
    _watch(c, clock, sb, age_min=90)
    for _ in range(9):
        _tick(sb, clock, 1.0, mc=10_000)
    _tick(sb, clock, 1.0, mc=200_000)
    w = c.db.one("SELECT status, under_floor_evals, under_floor_since FROM strat_b_watch WHERE mint=?", (M,))
    assert w["status"] == "watching" and (w["under_floor_evals"] or 0) == 0 and w["under_floor_since"] is None
    _tick(sb, clock, 1.0, mc=10_000)
    w = c.db.one("SELECT status, under_floor_evals FROM strat_b_watch WHERE mint=?", (M,))
    assert w["status"] == "watching" and w["under_floor_evals"] == 1


def test_watch_expires_on_zero_liquidity_not_on_missing_price():
    c, clock, sb = _sb()
    _watch(c, clock, sb, age_min=90)
    clock.t += 10
    run(sb.tick(pairs=[]))                                    # no pair / no price: keep watching
    assert c.db.one("SELECT status FROM strat_b_watch WHERE mint=?", (M,))["status"] == "watching"
    _tick(sb, clock, 1.0, liq=0)
    ev = c.db.one("SELECT * FROM strat_b_evals ORDER BY id DESC LIMIT 1")
    assert c.db.one("SELECT status FROM strat_b_watch WHERE mint=?", (M,))["status"] == "expired"
    assert ev["kind"] == "expired" and "liquidity $0" in ev["reason"]
