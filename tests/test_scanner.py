"""Scanner watch window: max_age_h (default 5h) instead of the old ~60 min drop."""
import json
import config
from agents.analyst import build_state
from agents.scanner import Scanner, decide, watch_window_min
from tests.helpers import make_ctx, pair, run

PUMP = "Scan1111111111111111111111111111111111pump"
NOW = 1_800_000_000.0


def _coin(age_min, mint=PUMP, **extra):
    row = {"mint": mint, "migrated_at": NOW - age_min * 60.0}
    row.update(extra)
    return row


def _ok_pair(mint=PUMP, **kw):
    return pair(mint, liq=kw.pop("liq", 50000), h1=kw.pop("h1", 10.0), **kw)


def test_watch_window_default_is_5h():
    c, _ = make_ctx()
    assert c.s["scanner.max_age_h"] == 5
    assert watch_window_min(c.s) == 300
    assert config.resolve("max age").key == "scanner.max_age_h"


def test_max_age_set_validates_and_logs():
    c, _ = make_ctx()
    msg = c.s.set("max age", 8, source="telegram")
    assert "5h" in msg and "8h" in msg
    assert c.s["scanner.max_age_h"] == 8
    row = c.db.one("SELECT key, old, new, source FROM change_log WHERE key='scanner.max_age_h' ORDER BY id DESC")
    assert row["source"] == "telegram"
    assert json.loads(row["old"]) == 5 and json.loads(row["new"]) == 8
    for bad in (0, 25, 4.5, "abc"):
        try:
            c.s.set("max age", bad, source="test")
            raise AssertionError(f"accepted {bad}")
        except config.SettingError:
            pass
    assert c.s["scanner.max_age_h"] == 8


def test_decide_passes_older_coin_inside_5h():
    c, _ = make_ctx()
    # previously dropped at 60 min; a 2.5h coin that clears every filter must now pass
    v, r = decide(c.s, _coin(150), _ok_pair(), 10.0, [], NOW)
    assert (v, r) == ("pass", None)
    v, r = decide(c.s, _coin(299), _ok_pair(), 10.0, [], NOW)
    assert v == "pass"
    v, r = decide(c.s, _coin(300), _ok_pair(), 10.0, [], NOW)
    assert (v, r) == ("drop", "never qualified")
    v, r = decide(c.s, _coin(9), _ok_pair(), 10.0, [], NOW)
    assert (v, r) == ("wait", None)          # still below min_pool_age_min
    v, r = decide(c.s, _coin(10), _ok_pair(), 10.0, [], NOW)
    assert v == "pass"
    v, r = decide(c.s, _coin(180), None, None, [], NOW)
    assert (v, r) == ("wait", None)          # no pair yet, but under 5h
    v, r = decide(c.s, _coin(301), None, None, [], NOW)
    assert (v, r) == ("drop", "never qualified")


def test_existing_filters_still_kill():
    c, _ = make_ctx()
    age = 150
    v, r = decide(c.s, _coin(age, mint="x" * 40 + "xxxx"), _ok_pair(), 10.0, [], NOW)
    assert v == "kill" and "pump" in r
    usdc = _ok_pair()
    usdc["quoteToken"] = {"address": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v", "symbol": "USDC"}
    v, r = decide(c.s, _coin(age), usdc, 10.0, [], NOW)
    assert v == "kill" and "SOL-quoted" in r
    v, r = decide(c.s, _coin(age), _ok_pair(liq=9999), 10.0, [], NOW)
    assert v == "kill" and r.startswith("liq ")
    v, r = decide(c.s, _coin(age), _ok_pair(), 25.1, [], NOW)
    assert v == "kill" and "top 10" in r
    v, r = decide(c.s, _coin(age), _ok_pair(), 25.0, [], NOW)
    assert v == "pass"                       # top10 ≤ 25% still allowed
    v, r = decide(c.s, _coin(age), _ok_pair(h1=81), 10.0, [], NOW)
    assert v == "kill" and r.startswith("+81%")
    v, r = decide(c.s, _coin(age), _ok_pair(name="CopyCat", symbol="CC"), 10.0,
                  [{"mint": "other", "name": "CopyCatCoin", "symbol": "CCC"}], NOW)
    assert v == "kill" and "copycat" in r
    v, r = decide(c.s, _coin(age), _ok_pair(), None, [], NOW)
    assert (v, r) == ("wait", None)          # never pass without top10


def test_backfill_reopens_dropped_under_5h_only():
    c, clock = make_ctx()
    clock.t = NOW
    sc = Scanner(c)

    def add(mint, age_min, status, reason=None, sent=0):
        ts = NOW - age_min * 60.0
        c.db.x(
            "INSERT INTO coins(mint,migrated_at,first_seen,status,reason,sent,night) VALUES(?,?,?,?,?,?,?)",
            (mint, ts, ts, status, reason, sent, c.night),
        )

    add("A" * 40 + "pump", 120, "dropped", "never qualified")   # 2h, reopen
    add("B" * 40 + "pump", 299, "dropped", "never qualified")   # just inside, reopen
    add("C" * 40 + "pump", 301, "dropped", "never qualified")   # past 5h, stay dropped
    add("D" * 40 + "pump", 90, "killed", "liq $0.0K")           # hard kill, stay killed
    add("E" * 40 + "pump", 80, "passed", None, sent=1)          # already passed
    add("F" * 40 + "pump", 15, "room", None)                   # already in room
    n = sc.backfill_room()
    assert n == 2
    st = {r["mint"][0]: r["status"] for r in c.db.rows("SELECT mint,status,reason FROM coins")}
    assert st == {"A": "room", "B": "room", "C": "dropped", "D": "killed", "E": "passed", "F": "room"}
    assert sc.backfill_room() == 0            # idempotent
    # raising the window reopens the 301-min coin; lowering does not un-kill
    c.s.set("max age", 6, source="test")
    assert sc.backfill_room() == 1
    assert c.coin("C" * 40 + "pump")["status"] == "room"


def test_poll_passes_backfilled_2h_coin_and_leaves_killed():
    c, clock = make_ctx()
    clock.t = NOW
    good = "G" * 40 + "pump"
    bad = "K" * 40 + "pump"
    old = "O" * 40 + "pump"
    for mint, age, status, reason in (
        (good, 150, "dropped", "never qualified"),
        (bad, 90, "killed", "liq $0.0K"),
        (old, 400, "dropped", "never qualified"),
    ):
        ts = NOW - age * 60.0
        c.db.x("INSERT INTO coins(mint,migrated_at,first_seen,status,reason,night) VALUES(?,?,?,?,?,?)",
               (mint, ts, ts, status, reason, c.night))
    c.net.pairs[good] = _ok_pair(good)
    c.net.pairs[bad] = _ok_pair(bad)
    c.net.pairs[old] = _ok_pair(old)
    sc = Scanner(c)

    async def fake_top10(mint, pool):
        return 12.0
    sc.top10 = fake_top10
    run(sc.poll_once())
    assert c.coin(good)["status"] == "passed"
    assert c.coin(bad)["status"] == "killed"
    assert c.coin(old)["status"] == "dropped"
    scan = c.db.one("SELECT verdict, reason FROM scans WHERE mint=?", (good,))
    assert scan["verdict"] == "pass"


def test_analyst_state_formats_older_age_as_hours():
    p = _ok_pair()
    young = build_state({"name": "Young", "ageMin": 45}, {"layers": []}, p)
    assert "graduated 45 min ago" in young
    old = build_state({"name": "Older", "ageMin": 180}, {"layers": []}, p)
    assert "graduated 3.0h ago" in old
    assert "180 min ago" not in old
