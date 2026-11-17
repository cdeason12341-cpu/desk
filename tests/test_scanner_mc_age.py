"""2026-11-15 owner test request: scanner.max_age_h off (0) and scanner.min_market_cap_usd floor."""
import json
import config
from agents.scanner import (Scanner, decide, fixable, recheck_offsets, next_recheck_at, watch_window_min,
                            market_cap, age_limit_off, NO_AGE_LIMIT_HORIZON_MIN)
from tests.helpers import make_ctx, pair, run

NOW = 1_800_000_000.0
M = "Q" * 40 + "pump"


def _sc(**kw):
    c, clock = make_ctx(**{"scanner.min_liquidity_usd": 10000, "scanner.recheck_min_liq_usd": 0,
                           "scanner.recheck_min_mc_usd": 0, "scanner.recheck_max_top10_pct": 0,
                           "scanner.recheck_max_h1_pct": 0, **kw})
    clock.t = NOW
    sc = Scanner(c)

    async def fake_top10(mint, pool):
        return getattr(sc, "_top10", 12.0)
    sc.top10 = fake_top10
    return c, clock, sc


def _add(c, mint, age_min, status="room", reason=None):
    ts = NOW - age_min * 60.0
    c.db.x("INSERT INTO coins(mint,migrated_at,first_seen,status,reason,night) VALUES(?,?,?,?,?,?)",
           (mint, ts, ts, status, reason, c.night))


def _p(mc=200000, fdv=None, liq=50000):
    p = pair(M, liq=liq)
    p.pop("marketCap", None)
    if mc is not None:
        p["marketCap"] = mc
    if fdv is not None:
        p["fdv"] = fdv
    return p


def test_settings_defaults_aliases_and_off():
    c, _ = make_ctx()
    assert c.s["scanner.min_market_cap_usd"] == 30000
    for a in ("min mc", "market cap", "mc", "min market cap"):
        assert config.resolve(a).key == "scanner.min_market_cap_usd"
    assert config.resolve("age limit").key == "scanner.max_age_h"
    msg = c.s.set("min mc", "45k", source="test")
    assert c.s["scanner.min_market_cap_usd"] == 45000 and "$45,000" in msg
    c.s.set("max age", "off", source="test")
    assert c.s["scanner.max_age_h"] == 0 and age_limit_off(c.s)
    assert config.display(config.BY_KEY["scanner.max_age_h"], 0) == "off"
    c.s.set("market cap", "off", source="test")
    assert c.s["scanner.min_market_cap_usd"] == 0
    try:
        c.s.set("min mc", -5, source="test")
        raise AssertionError("accepted negative")
    except config.SettingError:
        pass


def test_market_cap_falls_back_to_fdv():
    assert market_cap(_p(mc=50000)) == 50000
    assert market_cap(_p(mc=None, fdv=41000)) == 41000
    assert market_cap(_p(mc=0, fdv=41000)) == 41000
    assert market_cap(_p(mc=None)) is None
    assert market_cap(None) is None


def test_decide_mc_floor():
    c, _ = make_ctx()
    coin = {"mint": M, "migrated_at": NOW - 20 * 60}
    assert decide(c.s, coin, _p(mc=29000), 10.0, [], NOW) == ("kill", "mc $29.0K")
    assert decide(c.s, coin, _p(mc=30000), 10.0, [], NOW) == ("pass", None)        # exactly 30K passes
    assert decide(c.s, coin, _p(mc=None, fdv=31000), 10.0, [], NOW) == ("pass", None)
    assert decide(c.s, coin, _p(mc=None), 10.0, [], NOW) == ("kill", "mc unknown")
    assert decide(c.s, coin, _p(mc=1000, liq=2000), 10.0, [], NOW)[1].startswith("liq ")   # liq first
    c.s.set("min mc", 0, source="test")
    assert decide(c.s, coin, _p(mc=None), 10.0, [], NOW) == ("pass", None)


def test_mc_kills_are_recheckable():
    for r in ("mc $12.0K", "mc unknown", "recheck 2: mc $20.0K · next at 20m"):
        assert fixable(r), r


def test_mc_kill_rechecked_then_passes_and_scan_json_has_mc():
    c, clock, sc = _sc()
    _add(c, M, 1)
    c.net.pairs[M] = _p(mc=12000)
    run(sc.poll_once())
    assert c.coin(M)["status"] == "recheck"
    s = c.db.one("SELECT verdict, reason, json FROM scans WHERE mint=?", (M,))
    assert s["verdict"] == "kill" and s["reason"] == "mc $12.0K"
    assert json.loads(s["json"])["marketCapUsd"] == 12000
    c.net.pairs[M] = _p(mc=35000)
    clock.t = NOW + 9 * 60 + 1
    run(sc.poll_once())
    last = c.db.one("SELECT verdict, reason, json FROM scans WHERE mint=? ORDER BY id DESC", (M,))
    assert last["verdict"] == "pass" and last["reason"].endswith("passed")
    assert json.loads(last["json"])["marketCapUsd"] == 35000


def test_age_off_rechecks_keep_schedule_and_horizon_is_finite():
    c, clock, sc = _sc(**{"scanner.max_age_h": 0})
    assert watch_window_min(c.s) == NO_AGE_LIMIT_HORIZON_MIN
    assert recheck_offsets(c.s) == [5, 10, 20, 30, 45, 60]
    assert next_recheck_at(c.s, NOW - 61 * 60, NOW) is None          # schedule still ends at 60m
    c.s.set("recheck", "60,400", source="test")
    assert recheck_offsets(c.s) == [60, 400]                         # no 5h clip when off
    c.s.set("recheck", "5,10,20,30,45,60", source="test")
    # a coin still in the waiting room past 5h is NOT dropped with the limit off
    old = "O" * 40 + "pump"
    _add(c, old, 6 * 60)
    c.net.pairs[old] = pair(old, liq=50000)
    run(sc.poll_once())
    assert c.coin(old)["status"] == "passed"
    # but a parked recheck is still cleaned up after the 24h horizon (box paused for a day)
    _add(c, M, 2, "killed", "mc $5.0K")
    sc.backfill_rechecks()
    assert c.coin(M)["status"] == "recheck"
    clock.t = NOW + 25 * 3600
    assert sc.due_rechecks(clock.t) == []
    assert c.coin(M)["status"] == "killed"


def test_age_on_still_drops_past_limit():
    c, clock, sc = _sc()
    old = "O" * 40 + "pump"
    _add(c, old, 6 * 60)
    c.net.pairs[old] = pair(old, liq=50000)
    run(sc.poll_once())
    assert c.coin(old)["status"] == "dropped"
