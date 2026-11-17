"""scanner.json: live Scanner view feed (owner request 2026-11-15 6:25 PM CT). Bars from settings, no secrets."""
import json
import links, secrets_store
from publisher import Builder
from tests.helpers import make_ctx
from tests.test_publisher_gmgn import DeskStub, SECRET_NAMES

P = "Scan111111111111111111111111111111111pump"
K = "Kill111111111111111111111111111111111pump"
R = "Rchk111111111111111111111111111111111pump"
Q = "QuoT111111111111111111111111111111111pump"


def _scan(c, ts, mint, verdict, reason, **b):
    body = {"agent": "scanner", "mint": mint, "name": mint[:4], "verdict": verdict, "reason": reason, **b}
    c.db.x("INSERT INTO scans(ts,mint,verdict,reason,json,night) VALUES(?,?,?,?,?,?)", (ts, mint, verdict, reason, json.dumps(body), c.night))


def test_scanner_feed_checks_bars_counts_and_limits():
    c, clock = make_ctx(**{"scanner.min_liquidity_usd": 2500, "scanner.max_top10_pct": 30, "scanner.min_market_cap_usd": 30000})
    t = clock.t
    _scan(c, t - 400, P, "pass", None, liquidityUsd=12000, marketCapUsd=45000, top10Pct=22.0, h1ChangePct=12.0, pool="x", ageMin=11)
    _scan(c, t - 300, K, "kill", "top 10 hold 80% · too far for recheck", liquidityUsd=9000, marketCapUsd=40000, top10Pct=80.0, h1ChangePct=5.0, pool="y")
    _scan(c, t - 200, R, "recheck", "recheck 1: mc $26.6K · next at 10m", liquidityUsd=10000, marketCapUsd=26600, top10Pct=None,
          h1ChangePct=-3.0, pool="z", recheck=1, recheckAt="2026-01-01T00:10:00Z")
    _scan(c, t - 100, Q, "kill", "not SOL-quoted (MIL)", liquidityUsd=129, marketCapUsd=223, h1ChangePct=-41.0, pool="w")
    _scan(c, t - 7200, "Old11111111111111111111111111111111111pump", "kill", "liq $1K", liquidityUsd=1000)
    c.db.x("INSERT INTO coins(mint,status) VALUES(?,?)", (R, "recheck"))
    f = Builder(DeskStub(c)).build()["scanner.json"]
    assert [i["mint"] for i in f["items"][:4]] == [Q, R, K, P]                     # newest first
    by = {i["mint"]: i for i in f["items"]}
    ck = lambda m: {x["key"]: x for x in by[m]["checks"]}
    assert by[P]["pill"] == "pass" and by[P]["reason"] is None and all(x["pass"] for x in ck(P).values())
    assert ck(P)["liq"]["bar"] == 2500 and ck(P)["mc"]["bar"] == 30000 and ck(P)["top10"]["bar"] == 30 and ck(P)["h1"]["bar"] == 80
    assert by[K]["pill"] == "kill" and ck(K)["top10"]["pass"] is False and ck(K)["liq"]["pass"] is True
    assert by[K]["reason"].startswith("top 10 hold 80%")
    assert by[R]["pill"] == "recheck" and by[R]["recheckN"] == 1 and by[R]["nextRecheckN"] == 2 and by[R]["nextAt"]
    assert ck(R)["mc"]["pass"] is False and ck(R)["top10"]["pass"] is None
    assert ck(Q)["sol"]["pass"] is False and ck(Q)["pump"]["pass"] is True
    assert all("r=savipww" in i["link"] and i["link"] == links.coin_url(i["mint"]) for i in f["items"])
    assert f["counts1h"] == {"seen": 4, "passed": 1, "killed": 2, "rechecking": 1, "surges": 0}
    # limits follow settings, not constants
    c.s.set("scanner.min_liquidity_usd", 4000, source="test")
    f2 = Builder(DeskStub(c)).scanner_feed()
    assert f2["limits"]["minLiquidityUsd"] == 4000 and {x["key"]: x for x in f2["items"][-1]["checks"]}["liq"]["bar"] == 4000
    assert f2["limits"]["maxTop10Pct"] == 30 and f2["limits"]["recheckMinutes"] == c.s["scanner.recheck_minutes"]
    blob = json.dumps(f)
    for n in SECRET_NAMES:
        v = secrets_store.get(n)
        if v:
            assert v not in blob


def test_app_has_scanner_segment():
    js = open("web/app.js").read()
    assert 'tab("scanner", "scanner", "Scanner")' in js and '"scanner.json"' in js and "function scannerView()" in js
