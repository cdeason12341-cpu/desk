"""Scanner re-check: fixable kills (thin pool / top 10 / already pumped) are re-scanned at set ages."""
import config
from agents.scanner import Scanner, fixable, recheck_offsets, next_recheck_at
from tests.helpers import make_ctx, pair, run

NOW = 1_800_000_000.0
M = "R" * 40 + "pump"


def _sc(top10=12.0):
    # schedule mechanics only: near-miss gates off here (tested in test_scanner_near_miss.py)
    c, clock = make_ctx(**{"scanner.min_liquidity_usd": 10000, "scanner.recheck_min_liq_usd": 0,
                           "scanner.recheck_min_mc_usd": 0, "scanner.recheck_max_top10_pct": 0,
                           "scanner.recheck_max_h1_pct": 0})
    clock.t = NOW
    sc = Scanner(c)

    async def fake_top10(mint, pool):
        return sc._top10 if hasattr(sc, "_top10") else top10
    sc.top10 = fake_top10
    return c, clock, sc


def _add(c, mint, age_min, status="room", reason=None):
    ts = NOW - age_min * 60.0
    c.db.x("INSERT INTO coins(mint,migrated_at,first_seen,status,reason,night) VALUES(?,?,?,?,?,?)",
           (mint, ts, ts, status, reason, c.night))


def test_setting_defaults_and_validation():
    c, _ = make_ctx()
    assert c.s["scanner.recheck_minutes"] == "5,10,20,30,45,60"
    assert recheck_offsets(c.s) == [5, 10, 20, 30, 45, 60]
    assert config.resolve("recheck").key == "scanner.recheck_minutes"
    c.s.set("recheck", "5, 15,30", source="test")
    assert c.s["scanner.recheck_minutes"] == "5,15,30"
    for bad in ("abc", "10,5", "0.5", ""):
        try:
            c.s.set("recheck", bad, source="test")
            raise AssertionError(f"accepted {bad!r}")
        except config.SettingError:
            pass
    c.s.set("recheck", "off", source="test")
    assert recheck_offsets(c.s) == []
    c.s.set("recheck", "60,400", source="test")
    assert recheck_offsets(c.s) == [60]               # clipped to the 5h watch window


def test_fixable_reasons_only():
    for r in ("liq $3.2K", "top 10 hold 31%", "+120% in 1h", "recheck 2: liq $5.0K · next at 20m"):
        assert fixable(r), r
    for r in ("not a pump mint", "not SOL-quoted (USDC)", "copycat of X", "never qualified", None, ""):
        assert not fixable(r), r


def test_next_recheck_schedule():
    c, _ = make_ctx()
    mig = NOW - 60
    assert next_recheck_at(c.s, mig, NOW) == mig + 5 * 60
    assert next_recheck_at(c.s, mig, mig + 7 * 60) == mig + 10 * 60
    assert next_recheck_at(c.s, mig, mig + 60 * 60) is None


def test_thin_coin_rechecked_then_passes_to_auditor():
    c, clock, sc = _sc()
    _add(c, M, 1)
    c.net.pairs[M] = pair(M, liq=3000)
    run(sc.poll_once())
    row = c.coin(M)
    assert row["status"] == "recheck"
    s = c.db.rows("SELECT verdict, reason, json FROM scans WHERE mint=?", (M,))
    assert len(s) == 1 and s[0]["verdict"] == "kill" and s[0]["reason"].startswith("liq ")
    assert '"recheckAgeMin": 5' in s[0]["json"]
    # not due yet: nothing happens
    clock.t = NOW + 3 * 60
    run(sc.poll_once())
    assert len(c.db.rows("SELECT id FROM scans WHERE mint=?", (M,))) == 1
    # at 5m still thin -> recheck 1, next at 10m
    clock.t = NOW + 4 * 60
    run(sc.poll_once())
    last = c.db.one("SELECT verdict, reason FROM scans WHERE mint=? ORDER BY id DESC", (M,))
    assert last["verdict"] == "recheck" and last["reason"].startswith("recheck 1: liq ") and "next at 10m" in last["reason"]
    assert c.coin(M)["status"] == "recheck"
    # at 10m liquidity has grown -> pass (age >= min pool age 10m)
    c.net.pairs[M] = pair(M, liq=30000)
    clock.t = NOW + 9 * 60 + 1
    run(sc.poll_once())
    last = c.db.one("SELECT verdict, reason FROM scans WHERE mint=? ORDER BY id DESC", (M,))
    assert last["verdict"] == "pass" and last["reason"] == "recheck 2: passed"
    assert c.coin(M)["status"] == "passed" and c.coin(M)["sent"] == 1
    msg = c.bus.auditor.get_nowait()
    assert msg["mint"] == M and msg["recheck"] == 2
    # kill counter only counts the first kill
    assert c.db.one("SELECT COUNT(*) n FROM scans WHERE verdict='kill'")["n"] == 1


def test_recheck_that_clears_filters_early_goes_back_to_room():
    c, clock, sc = _sc()
    _add(c, M, 1)
    sc._top10 = 40.0
    c.net.pairs[M] = pair(M, liq=30000)
    run(sc.poll_once())
    assert c.coin(M)["status"] == "recheck"
    assert c.db.one("SELECT reason FROM scans WHERE mint=?", (M,))["reason"] == "top 10 hold 40%"
    sc._top10 = 20.0
    clock.t = NOW + 4 * 60 + 1                     # age 5m: clears filters but under 10m pool age
    run(sc.poll_once())
    assert c.coin(M)["status"] == "room"
    last = c.db.one("SELECT verdict, reason FROM scans WHERE mint=? ORDER BY id DESC", (M,))
    assert last["verdict"] == "recheck" and "back in waiting room" in last["reason"]
    clock.t = NOW + 9 * 60 + 1
    run(sc.poll_once())
    assert c.coin(M)["status"] == "passed"


def test_schedule_runs_out_and_permanent_kills_never_recheck():
    c, clock, sc = _sc()
    usdc = "U" * 40 + "pump"
    _add(c, M, 50)
    _add(c, usdc, 1)
    c.net.pairs[M] = pair(M, liq=3000)
    p = pair(usdc)
    p["quoteToken"] = {"address": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v", "symbol": "USDC"}
    c.net.pairs[usdc] = p
    run(sc.poll_once())
    assert c.coin(usdc)["status"] == "killed"
    assert c.coin(M)["status"] == "recheck"         # 60m slot left
    clock.t = NOW + 10 * 60
    run(sc.poll_once())
    assert c.coin(M)["status"] == "killed"
    last = c.db.one("SELECT verdict, reason FROM scans WHERE mint=? ORDER BY id DESC", (M,))
    assert last["reason"].startswith("recheck 1: liq ") and last["reason"].endswith("final")
    clock.t = NOW + 30 * 60
    n = len(c.db.rows("SELECT id FROM scans"))
    run(sc.poll_once())
    assert len(c.db.rows("SELECT id FROM scans")) == n


def test_queue_bound_and_per_poll_cap():
    c, clock, sc = _sc()
    c.s.set("scanner.recheck_max_queue", 2, source="test")
    mints = [chr(65 + i) * 40 + "pump" for i in range(4)]
    for m in mints:
        _add(c, m, 1)
        c.net.pairs[m] = pair(m, liq=1000)
    run(sc.poll_once())
    st = sorted(c.coin(m)["status"] for m in mints)
    assert st == ["killed", "killed", "recheck", "recheck"]
    c.s.set("scanner.recheck_per_poll", 1, source="test")
    clock.t = NOW + 5 * 60
    assert len(sc.due_rechecks(clock.t)) == 1


def test_backfill_schedules_recent_fixable_kills_only():
    c, clock, sc = _sc()
    _add(c, "A" * 40 + "pump", 3, "killed", "liq $0.2K")
    _add(c, "B" * 40 + "pump", 20, "killed", "top 10 hold 28%")
    _add(c, "C" * 40 + "pump", 90, "killed", "liq $0.2K")          # past last slot
    _add(c, "D" * 40 + "pump", 3, "killed", "not SOL-quoted (USDC)")
    _add(c, "E" * 40 + "pump", 3, "killed", "copycat of X")
    assert sc.backfill_rechecks() == 2
    st = {r["mint"][0]: r["status"] for r in c.db.rows("SELECT mint,status FROM coins")}
    assert st == {"A": "recheck", "B": "recheck", "C": "killed", "D": "killed", "E": "killed"}
    assert sc.backfill_rechecks() == 0


def test_never_past_max_age():
    c, clock, sc = _sc()
    c.s.set("recheck", "5,10,20,30,45,60", source="test")
    _add(c, M, 2, "killed", "liq $1.0K")
    sc.backfill_rechecks()
    clock.t = NOW + 6 * 3600                          # box paused for hours
    assert sc.due_rechecks(clock.t) == []
    assert c.coin(M)["status"] == "killed"
