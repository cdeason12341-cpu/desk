"""Owner rule 2026-11-15 12:26 CT: only re-check first-scan kills that came close to passing."""
import json
import config
from agents.scanner import Scanner, near_miss, fixable, TOO_FAR
from tests.helpers import make_ctx, pair, run

NOW = 1_800_000_000.0
M = "N" * 40 + "pump"


def _sc(top10=12.0):
    c, clock = make_ctx(**{"scanner.min_liquidity_usd": 10000})
    clock.t = NOW
    sc = Scanner(c)

    async def fake_top10(mint, pool):
        return getattr(sc, "_top10", top10)
    sc.top10 = fake_top10
    return c, clock, sc


def _add(c, mint, age_min, status="room", reason=None, liq=None, top10=None, h1=None):
    ts = NOW - age_min * 60.0
    c.db.x("INSERT INTO coins(mint,migrated_at,first_seen,status,reason,night,liq_at_scan,top10,h1) VALUES(?,?,?,?,?,?,?,?,?)",
           (mint, ts, ts, status, reason, c.night, liq, top10, h1))


def test_defaults_aliases_off():
    c, _ = make_ctx()
    assert c.s["scanner.recheck_min_liq_usd"] == 5000
    assert c.s["scanner.recheck_min_mc_usd"] == 20000
    assert c.s["scanner.recheck_max_top10_pct"] == 40
    assert c.s["scanner.recheck_max_h1_pct"] == 150
    assert config.resolve("near miss liq").key == "scanner.recheck_min_liq_usd"
    assert config.resolve("recheck min mc").key == "scanner.recheck_min_mc_usd"
    assert config.resolve("recheck top10").key == "scanner.recheck_max_top10_pct"
    assert config.resolve("recheck pump").key == "scanner.recheck_max_h1_pct"
    assert config.resolve("recheck liq").key == "executor.recheck_liq_drop_pct"      # unchanged
    c.s.set("near miss liq", "7k", source="test")
    assert c.s["scanner.recheck_min_liq_usd"] == 7000
    for k in ("recheck min liq", "recheck min mc", "recheck top10", "recheck h1"):
        c.s.set(k, "off", source="test")
    assert near_miss(c.s, liq=1, mc=1, top10=99, h1=9999) == (True, [])


def test_gate_values():
    c, _ = make_ctx()
    assert near_miss(c.s, liq=5000, mc=40000)[0]              # exactly at gate: recheck
    assert not near_miss(c.s, liq=4999, mc=40000)[0]
    assert near_miss(c.s, liq=20000, mc=20000)[0]
    assert not near_miss(c.s, liq=20000, mc=19999)[0]
    assert near_miss(c.s, liq=20000, mc=None)[0]               # mc unknown keeps re-checking
    assert near_miss(c.s, liq=20000, mc=40000, top10=40)[0]
    assert not near_miss(c.s, liq=20000, mc=40000, top10=41)[0]
    assert near_miss(c.s, liq=20000, mc=40000, h1=150)[0]
    assert not near_miss(c.s, liq=20000, mc=40000, h1=151)[0]
    # several failing checks: every one must be within its margin
    assert near_miss(c.s, liq=6000, mc=25000, top10=35, h1=120)[0]
    ok, far = near_miss(c.s, liq=6000, mc=25000, top10=55, h1=120)
    assert not ok and far == ["top 10 55% > 40%"]
    # passing checks never block, even if beyond a gate value
    assert near_miss(c.s, liq=50000, mc=1e6, top10=10, h1=50)[0]


def test_too_far_reason_is_final():
    assert not fixable(f"liq $1.0K · {TOO_FAR}")


def test_near_miss_is_rechecked_far_is_final():
    c, clock, sc = _sc()
    near, far = "A" * 40 + "pump", "B" * 40 + "pump"
    _add(c, near, 1)
    _add(c, far, 1)
    c.net.pairs[near] = pair(near, liq=6000)
    c.net.pairs[far] = pair(far, liq=3000)
    run(sc.poll_once())
    assert c.coin(near)["status"] == "recheck"
    assert c.coin(far)["status"] == "killed"
    r = c.db.one("SELECT verdict, reason, json FROM scans WHERE mint=?", (far,))
    assert r["verdict"] == "kill" and r["reason"] == f"liq $3.0K · {TOO_FAR}"
    assert json.loads(r["json"])["tooFar"] == ["liq $3.0K < $5.0K"]
    assert c.coin(far)["reason"].endswith(TOO_FAR)
    assert c.db.one("SELECT COUNT(*) n FROM rechecks WHERE mint=?", (far,))["n"] == 0


def test_mc_and_multi_fail_gates():
    c, clock, sc = _sc()
    a, b, u = "C" * 40 + "pump", "D" * 40 + "pump", "E" * 40 + "pump"
    for m in (a, b, u):
        _add(c, m, 1)
    pa = pair(a, liq=15000); pa["marketCap"] = 25000      # mc kill, near
    pb = pair(b, liq=15000); pb["marketCap"] = 12000      # mc kill, far
    pu = pair(u, liq=15000); pu.pop("marketCap")          # mc unknown keeps re-checking
    c.net.pairs.update({a: pa, b: pb, u: pu})
    run(sc.poll_once())
    assert c.coin(a)["status"] == "recheck"
    assert c.coin(b)["status"] == "killed" and c.coin(b)["reason"] == f"mc $12.0K · {TOO_FAR}"
    assert c.coin(u)["status"] == "recheck"
    # liq kill is near, but the pump (also failing) is far -> final
    m = "F" * 40 + "pump"
    _add(c, m, 1)
    c.net.pairs[m] = pair(m, liq=7000, h1=400)
    run(sc.poll_once())
    assert c.coin(m)["status"] == "killed" and TOO_FAR in c.coin(m)["reason"]


def test_later_rechecks_keep_schedule_without_gate():
    c, clock, sc = _sc()
    _add(c, M, 1)
    c.net.pairs[M] = pair(M, liq=6000)
    run(sc.poll_once())
    assert c.coin(M)["status"] == "recheck"
    c.net.pairs[M] = pair(M, liq=1000)                  # got worse: still keeps its schedule
    clock.t = NOW + 4 * 60
    run(sc.poll_once())
    last = c.db.one("SELECT verdict, reason FROM scans WHERE mint=? ORDER BY id DESC", (M,))
    assert last["verdict"] == "recheck" and "next at 10m" in last["reason"] and TOO_FAR not in last["reason"]
    assert c.coin(M)["status"] == "recheck"


def test_backfill_applies_gate():
    c, clock, sc = _sc()
    _add(c, "G" * 40 + "pump", 3, "killed", "liq $6.0K", liq=6000)
    _add(c, "H" * 40 + "pump", 3, "killed", "liq $1.0K", liq=1000)
    _add(c, "I" * 40 + "pump", 3, "killed", "mc $12.0K", liq=15000)
    _add(c, "J" * 40 + "pump", 3, "killed", f"liq $1.0K · {TOO_FAR}", liq=1000)
    assert sc.backfill_rechecks() == 1
    assert c.coin("G" * 40 + "pump")["status"] == "recheck"
