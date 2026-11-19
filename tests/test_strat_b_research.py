"""DECISION 91: Strategy B research profile (PAPER) from the 'How profitable crypto bot users set up their bots' Page."""
import pytest
import config, strat_b, strat_b_research as R
from strat_b import StratB
from tests.helpers import run
from tests.test_strat_b import _audit, _pair, _sb, _orders, M, M2


class FakeJup:
    def __init__(self, buy=None, sell=None):
        self.buy = buy or {"ok": True, "out": 1_000_000, "impact_pct": 0.1, "route": "PumpSwap"}
        self.sell = sell or {"ok": True, "out": 100_000_000, "impact_pct": 0.2, "route": "PumpSwap"}
        self.calls = []

    async def __call__(self, inp, out, amount, slippage_bps=2500):
        self.calls.append((inp, out, amount))
        return dict(self.buy if inp == R.USDC else self.sell)


@pytest.fixture
def jup(monkeypatch):
    j = FakeJup()
    monkeypatch.setattr(R, "quote", j)
    return j


def _rsb(**kw):
    s = {"strat_b.profile": "research", "strat_b.enabled": True, "scanner.min_market_cap_usd": 15000}
    s.update(kw)
    c, clock, sb = _sb(**s)

    async def no_live(*a, **k):
        raise AssertionError("research profile must never reach B LIVE")
    sb.live.buy = no_live
    return c, clock, sb


def _offer(c, clock, sb, mint=M, src=None, **audit):
    c.db.x("INSERT INTO coins(mint,name,symbol,migrated_at,night) VALUES(?,?,?,?,?)", (mint, "Cat", "CAT", clock.t - 600, c.night))
    scan = {"mint": mint, "name": "Cat", "symbol": "CAT", "ageMin": 10}
    if src:
        scan["b_source"] = src
    a = _audit(**{k: v for k, v in audit.items() if k in ("top10", "holders", "bundle_pct")})
    a.update({k: v for k, v in audit.items() if k not in ("top10", "holders", "bundle_pct")})
    sb.offer(scan, a)
    return c.db.one("SELECT * FROM strat_b_watch WHERE mint=?", (mint,))


def _tick(sb, clock, price, dt=30, mints=(M,), **kw):
    clock.t += dt
    return run(sb.tick(pairs=[_pair(m, price=price, **kw) for m in mints]))


def _bought(c, clock, sb, jup, mint=M):
    _offer(c, clock, sb, mint=mint)
    _tick(sb, clock, 1.0, mints=(mint,))
    o = [r for r in _orders(c) if r["mint"] == mint]
    assert len(o) == 1 and o[0]["status"] == "open"
    return o[0]


# ---------- settings
def test_profile_and_settings():
    c, clock, sb = _rsb()
    assert "research" in config.BY_KEY["strat_b.profile"].choices
    assert c.s["strat_b.rs_trade_usd"] == 20.0 and c.s["strat_b.rs_max_open"] == 1
    assert (c.s["strat_b.rs_trail_start_pct"], c.s["strat_b.rs_trail_pct"], c.s["strat_b.rs_trail_floor_pct"]) == (15.0, 8.0, 8.0)
    assert (c.s["strat_b.rs_fee_check_min"], c.s["strat_b.rs_hard_close_min"], c.s["strat_b.rs_stop_pct"]) == (5.0, 10.0, 50.0)
    assert (c.s["strat_b.rs_book_small_usd"], c.s["strat_b.rs_book_large_usd"]) == (34.0, 500.0)
    assert config.BY_KEY["strat_b.rs_trade_usd"].lo == 20                # never shrinks below $20
    assert not any(k.startswith("strat_b.rs_") for k in config.PROTECTED)
    assert c.s["strat_b.mode"] == "PAPER"


def test_research_module_never_signs_or_reaches_gmgn():
    src = open(R.__file__).read().split('"""', 2)[2]
    for bad in ("import gmgn", "strat_b_live", "solana", "secrets_store", "keypair", "private", "swap(", "notifier"):
        assert bad not in src, bad


# ---------- intake filters
def test_offer_filters(jup):
    c, clock, sb = _rsb()
    assert _offer(c, clock, sb, mint=M, src="gecko trending")["status"] == "skipped"
    w = _offer(c, clock, sb, mint=M2, bundle_pct=12.0)
    assert w["status"] == "skipped" and "bundled" in w["reason"]
    w = _offer(c, clock, sb, mint="Tok22222222222222222222222222222222222pump", token_ext=["transferHook", "metadataPointer"])
    assert w["status"] == "skipped" and "transfer hook" in w["reason"]
    w = _offer(c, clock, sb, mint="Ok333333333333333333333333333333333333pump", top10=60.0, holders=5)
    assert w["status"] == "watching"                    # standard B's holder filters are not part of this profile


# ---------- entry
def test_buys_fixed_20_paper_after_sell_check(jup):
    c, clock, sb = _rsb(**{"strat_b.trade_usd": 50.0})
    o = _bought(c, clock, sb, jup)
    assert o["size_usd"] == 20.0 and o["mode"] == "PAPER" and o["profile"] == "research"
    assert o["exits_phase"] == "normal" and o["early_until"] is None
    assert [x[0] for x in jup.calls] == [R.USDC, M] and jup.calls[0][2] == 20_000_000 and jup.calls[1][2] == 1_000_000
    # fees: 1% + $0.25 + 1.25% protocol on the buy
    assert o["buy_fees_usd"] == pytest.approx(20 * 0.01 + 0.25 + 20 * 0.0125)
    assert "Jupiter sell route ok" in o["why"]


def test_no_sell_route_skips_coin(jup):
    c, clock, sb = _rsb()
    jup.sell = {"ok": False, "no_route": True, "error": "COULD_NOT_FIND_ANY_ROUTE"}
    _offer(c, clock, sb)
    _tick(sb, clock, 1.0)
    assert not _orders(c)
    w = c.db.one("SELECT * FROM strat_b_watch WHERE mint=?", (M,))
    assert w["status"] == "skipped" and "unsellable" in w["reason"]
    _tick(sb, clock, 1.0)
    assert not _orders(c)


def test_jupiter_error_waits_then_buys(jup):
    c, clock, sb = _rsb()
    jup.buy = {"ok": False, "no_route": False, "error": "jupiter 500"}
    _offer(c, clock, sb)
    _tick(sb, clock, 1.0)
    assert not _orders(c)
    assert c.db.one("SELECT status FROM strat_b_watch WHERE mint=?", (M,))["status"] == "watching"
    jup.buy = {"ok": True, "out": 1_000_000}
    _tick(sb, clock, 1.0)
    assert len(_orders(c)) == 1


def test_entry_window_expires(jup):
    c, clock, sb = _rsb()
    _offer(c, clock, sb)
    _tick(sb, clock, 1.0, dt=121)
    assert not _orders(c)
    assert c.db.one("SELECT status FROM strat_b_watch WHERE mint=?", (M,))["status"] == "expired"


def test_min_mc_kept(jup):
    c, clock, sb = _rsb()
    _offer(c, clock, sb)
    _tick(sb, clock, 1.0, mc=10000)
    assert not _orders(c)


def test_one_position_at_a_time(jup):
    c, clock, sb = _rsb()
    _bought(c, clock, sb, jup)
    _offer(c, clock, sb, mint=M2)
    _tick(sb, clock, 1.0, dt=10, mints=(M, M2))
    assert [o["mint"] for o in _orders(c)] == [M]


def test_live_mode_never_buys(jup):
    c, clock, sb = _rsb(**{"strat_b.mode": "LIVE"})
    _offer(c, clock, sb)
    _tick(sb, clock, 1.0)
    assert not _orders(c)


# ---------- exits
def _open(c, clock, entry=1.0, size=20.0, liq=50000):
    s = c.s
    f = R.fill_buy(s, entry, size, liq)
    c.db.x("INSERT INTO strat_b_orders(ts,night,mint,name,symbol,status,size_usd,signal_price,entry_price,tokens,buy_fees_usd,peak,"
           "last_price,last_liq,entry_liq,mc_entry,trail_armed,opened_at,why,last_seen,mode,exits_phase,early_until,profile) "
           "VALUES(?,?,?,?,?,'open',?,?,?,?,?,?,?,?,?,?,0,?,?,?,'PAPER','normal',NULL,'research')",
           (clock.t, c.night, M, "Cat", "CAT", size, entry, f["price"], f["tokens"], f["fees"], f["price"], entry, liq, liq, 200000,
            clock.t, '{"reason":"t"}', clock.t))
    return c.db.one("SELECT * FROM strat_b_orders WHERE mint=?", (M,))


def _last(c):
    return c.db.one("SELECT * FROM strat_b_orders WHERE mint=?", (M,))


def test_fee_check_at_5_minutes(jup):
    c, clock, sb = _rsb()
    _open(c, clock)
    _tick(sb, clock, 1.02, dt=240)                     # 4 min, under fees: still held
    assert _last(c)["status"] == "open"
    _tick(sb, clock, 1.02, dt=61)                      # 5 min: doesn't cover fees -> sell
    o = _last(c)
    assert o["status"] == "closed" and o["exit_reason"] == R.FEE_CHECK


def test_covers_fees_holds_until_10_min_hard_close(jup):
    c, clock, sb = _rsb()
    _open(c, clock)
    _tick(sb, clock, 1.12, dt=301)                     # +12%: covers fees, trail not armed (+15%)
    assert _last(c)["status"] == "open"
    _tick(sb, clock, 1.12, dt=300)                     # 10 min, trail never armed
    o = _last(c)
    assert o["status"] == "closed" and o["exit_reason"] == R.HARD_CLOSE


def test_trail_sells_only_above_net_floor(jup):
    c, clock, sb = _rsb()
    o = _open(c, clock)
    e = o["entry_price"]
    _tick(sb, clock, e * 1.40, dt=30)                  # armed, peak +40%
    assert _last(c)["trail_armed"] == 1
    _tick(sb, clock, e * 1.40 * 0.95, dt=30)           # 5% off the high: no
    assert _last(c)["status"] == "open"
    _tick(sb, clock, e * 1.40 * 0.91, dt=700)          # 9% off the high, past 10 min (armed: no hard close), net >= +8%
    o = _last(c)
    assert o["status"] == "closed" and o["exit_reason"] == R.RS_TRAIL and o["pnl_usd"] > 0
    assert o["pct"] >= 8


def test_armed_below_floor_holds_then_stop(jup):
    c, clock, sb = _rsb()
    o = _open(c, clock)
    e = o["entry_price"]
    _tick(sb, clock, e * 1.16, dt=30)                  # armed at +16%
    _tick(sb, clock, e * 1.02, dt=30)                  # under the +8% net floor: hold
    assert _last(c)["status"] == "open"
    _tick(sb, clock, e * 0.49, dt=30)
    o = _last(c)
    assert o["status"] == "closed" and o["exit_reason"] == R.RS_STOP


def test_stop_50(jup):
    c, clock, sb = _rsb()
    o = _open(c, clock)
    _tick(sb, clock, o["entry_price"] * 0.55, dt=30)
    assert _last(c)["status"] == "open"
    _tick(sb, clock, o["entry_price"] * 0.49, dt=30)
    assert _last(c)["exit_reason"] == R.RS_STOP


def test_standard_profile_orders_keep_standard_exits():
    c, clock, sb = _sb(**{"strat_b.profile": "default"})
    o = {"profile": "default", "entry_price": 1.0, "peak": 1.0, "trail_armed": 0, "opened_at": 0, "entry_liq": 50000,
         "exits_phase": "normal", "size_usd": 50, "tokens": 50}
    assert strat_b.exit_check(c.s, o, 1.0, 50000, [], 11 * 60)[0] is None   # no research time stop on standard rows


# ---------- books
def _closed(c, clock, pnl, i):
    c.db.x("INSERT INTO strat_b_orders(ts,night,mint,name,symbol,status,size_usd,entry_price,tokens,opened_at,closed_at,pnl_usd,mode,profile) "
           "VALUES(?,?,?,?,?,'closed',20,1,20,?,?,?,'PAPER','research')", (clock.t + i, c.night, f"m{i}", "x", "X", clock.t + i, clock.t + i + 1, pnl))


def test_books_34_and_500():
    c, clock, sb = _rsb()
    for i, p in enumerate((-10.0, -10.0, 5.0)):
        _closed(c, clock, p, i)
    small, large = R.books(c)
    assert large["startUsd"] == 500 and large["trades"] == 3 and large["pnlUsd"] == -15.0 and large["balanceUsd"] == 485.0
    # $34: -10 -> $24, -10 -> $14 (< $20), so the +$5 trade is skipped
    assert small["startUsd"] == 34 and small["trades"] == 2 and small["skippedNoCash"] == 1
    assert small["balanceUsd"] == 14.0 and small["canTrade"] is False and large["canTrade"] is True


def test_scorecard_research_card(jup):
    c, clock, sb = _rsb()
    _bought(c, clock, sb, jup)
    sc = strat_b.scorecard(c)
    assert sc["profile"] == "research" and sc["profileLabel"] == "Research" and sc["mode"] == "PAPER"
    rc = sc["research"]
    assert rc["active"] and rc["label"].startswith("B PAPER") and len(rc["open"]) == 1
    assert rc["open"][0]["label"] == "B PAPER" and rc["open"][0]["feeCheckAt"] and rc["open"][0]["tpLine"] is None
    assert [b["startUsd"] for b in rc["books"]] == [34.0, 500.0]
    assert any(r["k"] == "Can it sell" for r in sc["rules"])


def test_feed_idle_on_research():
    import strat_b_feed
    c, clock, sb = _rsb()
    f = strat_b_feed.BFeed(sb)
    assert "research" in run(f.poll_once())


def test_a_settings_untouched(jup):
    c, clock, sb = _rsb()
    before = {k: v for k, v in c.s.as_dict().items() if not k.startswith("strat_b.")}
    _bought(c, clock, sb, jup)
    _tick(sb, clock, 0.4, dt=30)
    assert {k: v for k, v in c.s.as_dict().items() if not k.startswith("strat_b.")} == before


def test_app_shows_research_paper_trades():
    from pathlib import Path
    js = (Path(__file__).resolve().parents[1] / "web" / "app.js").read_text()
    assert "function bResearchBody" in js and "bResearchBody(b) + bLiveBody(b)" in js
    assert "Strategy B · research · PAPER" in js and 'r.mode === "PAPER" ? `<span class="badge">B PAPER</span>`' in js
    assert "book</div>" in js
