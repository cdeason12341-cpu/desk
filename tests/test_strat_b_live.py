"""DECISION 60: Strategy B live view (strat_b_live.json): every B check with pass/fail + values, eval history, watchlist."""
import json
import strat_b
from strat_b import checks, away, live_feed
from tests.test_strat_b import M, M2, _sb, _watch, _tick, _pair, _buy
from tests.helpers import run

KEYS = ["top10", "holders", "bundle", "wait", "mc", "volmc", "liq", "dip", "bounce", "buys"]


def test_checks_cover_every_rule_with_values():
    c, clock, sb = _sb()
    now = clock.t
    w = {"top10": 15.0, "holders": 150, "bundle_pct": 0.0, "grad_at": now - 90 * 60, "high": 1.0, "low": 0.80}
    ck = checks(c.s, w, _pair(price=0.85), now)
    assert [x["k"] for x in ck] == KEYS
    assert all(x["pass"] is True for x in ck), ck
    d = {x["k"]: x for x in ck}
    assert d["top10"]["value"] == "15%" and d["top10"]["bar"] == "≤ 20%"
    assert d["holders"]["value"] == "150" and d["wait"]["value"] == "90m"
    assert d["mc"]["value"] == "$200.0K" and d["volmc"]["value"] == "1.50x" and d["liq"]["value"] == "$50.0K"
    assert d["dip"]["value"] == "-15.0%" and d["bounce"]["value"] == "+6.2%" and d["buys"]["value"] == "10/5"
    # display agrees with the decision
    assert strat_b.entry_check(c.s, w, _pair(price=0.85), now)[0] is True


def test_checks_fail_values_and_away():
    c, clock, sb = _sb()
    now = clock.t
    w = {"top10": 26.0, "holders": 50, "bundle_pct": 8.0, "grad_at": now - 20 * 60, "high": 1.0, "low": 0.95}
    ck = checks(c.s, w, _pair(price=0.95, mc=20000, vol24=5000, liq=5000, buys=2, sells=9), now)
    d = {x["k"]: x for x in ck}
    assert all(d[k]["pass"] is False for k in KEYS if k != "bounce"), ck
    a = away(c.s, w, ck, now)
    assert "40m left of the wait" in a and any("needs 10.0% more dip" in x for x in a)
    assert any(x.startswith("Market cap $20.0K") for x in a)
    nop = checks(c.s, w, None, now)
    assert [x["pass"] for x in nop[4:]] == [None] * 6 and nop[4]["value"] == "no price yet"
    assert "waiting for a price" in away(c.s, w, nop, now)


def test_eval_history_and_watchlist_in_live_feed():
    c, clock, sb = _sb()
    _watch(c, clock, sb, mint=M2, top10=30)                     # skipped at offer
    _buy(c, clock, sb)                                           # M: offer -> waits -> bought
    f = live_feed(c)
    items = f["items"]
    assert items[0]["mint"] == M and items[0]["status"] == "bought"
    assert [i["id"] for i in items] == sorted((i["id"] for i in items), reverse=True)      # newest first
    sk = [i for i in items if i["mint"] == M2]
    assert sk and sk[0]["status"] == "skipped" and "top 10" in sk[0]["reason"]
    assert [x["k"] for x in items[0]["checks"]] == ["source"] + KEYS and "r=savipww" in items[0]["link"]
    assert items[0]["source"] == "desk scanner" and items[0]["reason"].startswith("via desk scanner · ")
    assert any(i["status"] == "watching" and "too fresh" in i["reason"] for i in items)
    assert f["counts"]["boughtTonight"] == 1 and f["counts"]["skippedTonight"] == 1 and f["mode"] == "PAPER"
    assert f["watchlist"] == []                                  # nothing left watching


def test_watchlist_time_left_and_distance():
    c, clock, sb = _sb()
    _watch(c, clock, sb, age_min=70)
    _tick(sb, clock, 1.0)
    _tick(sb, clock, 0.95)
    f = live_feed(c)
    w = f["watchlist"][0]
    assert w["mint"] == M and w["minLeft"] == 180 and w["ageMin"] == 70
    assert any("more dip" in x for x in w["away"]) and w["passCount"] < w["checkCount"]
    assert "r=savipww" in w["link"]


def test_eval_rows_only_on_change_and_expiry_recorded():
    c, clock, sb = _sb()
    _watch(c, clock, sb, age_min=70)
    for p in (1.0, 0.99, 0.98, 0.97):
        _tick(sb, clock, p)
    n = c.db.one("SELECT COUNT(*) n FROM strat_b_evals")["n"]
    assert n == 2                                                # offer + one "no pullback" row, not one per tick
    assert c.db.one("SELECT evals FROM strat_b_watch")["evals"] == 5
    clock.t += 181 * 60
    run(sb.tick(pairs=[_pair(price=0.97)]))
    last = c.db.one("SELECT * FROM strat_b_evals ORDER BY id DESC LIMIT 1")
    assert last["status"] == "expired" and "no entry in 180m" in last["reason"]


def test_live_feed_only_tonight_and_limit():
    c, clock, sb = _sb()
    for i in range(5):
        _watch(c, clock, sb, mint=f"Lim{i}" + "1" * 36 + "pump")
    c.db.x("UPDATE strat_b_evals SET night='old' WHERE id=1")
    f = live_feed(c, limit=3)
    assert len(f["items"]) == 3 and all(i["id"] != 1 for i in f["items"])
    assert f["counts"]["evaluationsTonight"] == 4


def test_publisher_writes_strat_b_live():
    from publisher import Builder
    from tests.test_publisher_gmgn import _desk
    from strat_b import StratB
    d, c, clock, t = _desk(mode="PAPER")
    sb = StratB(c, log_files=False)
    c.strat_b = sb
    _watch(c, clock, sb)
    files = Builder(d).build()
    live = files["strat_b_live.json"]
    assert live["label"] == "STRATEGY B · PAPER" and live["watchlist"][0]["mint"] == M
    json.dumps(live)
