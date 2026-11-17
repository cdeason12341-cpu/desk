"""Live evaluation feed for the phone app: last 100 tonight, no secrets, FOMO links from links.py."""
import inspect, json
import links, main, secrets_store
from publisher import Builder
from tests.helpers import make_ctx
from tests.test_publisher_gmgn import DeskStub, SECRET_NAMES, LEAK_FIELDS

M_KILL = "FeedKill1111111111111111111111111111pump"
M_RE = "FeedRecheck11111111111111111111111111pump"
M_ROOM = "FeedRoom1111111111111111111111111111pump"
M_AUD = "FeedAudit1111111111111111111111111111pump"
M_AN = "FeedAnal11111111111111111111111111111pump"
NOW = 1_800_000_000.0


def _coin(c, mint, ts, name, symbol, status="room"):
    c.db.x("INSERT INTO coins(mint,name,symbol,status,migrated_at,first_seen,night) VALUES(?,?,?,?,?,?,?)",
           (mint, name, symbol, status, ts, ts, c.night))


def _scan(c, mint, ts, verdict, reason, **extra):
    body = {"agent": "scanner", "mint": mint, "verdict": verdict, "reason": reason, **extra}
    c.db.x("INSERT INTO scans(ts,mint,verdict,reason,json,night) VALUES(?,?,?,?,?,?)",
           (ts, mint, verdict, reason, json.dumps(body), c.night))


def test_events_loop_publishes_on_scan():
    src = inspect.getsource(main.Desk.events_loop)
    assert '"scan"' in src


def test_feed_shape_stages_order_bars_and_links():
    c, clock = make_ctx(**{"analyst.min_score": 60, "analyst.min_alive_6h": 0.70,
                           "analyst.min_organic": 0.65, "analyst.max_chase": 0.30})
    clock.t = NOW
    _coin(c, M_KILL, NOW - 500, "DeadCoin", "DEAD", "killed")
    _scan(c, M_KILL, NOW - 400, "kill", "liq $0.0K")
    _coin(c, M_RE, NOW - 350, "Retry", "RTY", "recheck")
    _scan(c, M_RE, NOW - 300, "recheck", "recheck 2: liq $5.4K · next at 30m", recheck=2)
    _coin(c, M_ROOM, NOW - 250, "Waiter", "WAIT", "room")
    _scan(c, M_ROOM, NOW - 200, "recheck", "recheck 1: clears filters, back in waiting room", recheck=1)
    _coin(c, M_AUD, NOW - 180, "AudFail", "AUD", "passed")
    c.db.x("INSERT INTO audits(ts,mint,passed,reason,layer,json,night) VALUES(?,?,?,?,?,?,?)",
           (NOW - 150, M_AUD, 0, "dev sold", 3, json.dumps({
               "agent": "auditor", "mint": M_AUD, "passed": False, "reason": "dev sold",
               "layersChecked": 3,
               "layers": [{"n": 1, "name": "Mint authority", "pass": True, "value": "Revoked"},
                          {"n": 2, "name": "Freeze authority", "pass": True, "value": "Revoked"},
                          {"n": 3, "name": "Dev wallet", "pass": False, "value": "dev sold"}],
           }), c.night))
    _coin(c, M_AN, NOW - 120, "Nyajira", "NYA", "passed")
    c.db.x("INSERT INTO audits(ts,mint,passed,reason,layer,json,night) VALUES(?,?,?,?,?,?,?)",
           (NOW - 90, M_AN, 1, None, 8, json.dumps({
               "agent": "auditor", "mint": M_AN, "passed": True, "layersChecked": 8,
               "layers": [{"n": i, "name": n, "pass": True, "value": "ok"} for i, n in
                          enumerate(["Mint", "Freeze", "Dev", "Top10", "Liq", "Age", "Bundles", "Delegations"], 1)],
           }), c.night))
    body = {"agent": "analyst", "mint": M_AN, "answers": {"alive_6h": 0.54, "organic": 0.53, "flush": 0.27, "chase": 0.68},
            "score": 59, "passed": False, "failed": ["alive_6h 0.54", "organic 0.53", "chase 0.68", "score 59"]}
    c.db.x("INSERT INTO analyses(ts,mint,passed,failed,score,model,input_tokens,output_tokens,json,night) VALUES(?,?,?,?,?,?,?,?,?,?)",
           (NOW - 30, M_AN, 0, json.dumps(body["failed"]), 59, "jev-1.14.0", 10, 10, json.dumps(body), c.night))

    fd = Builder(DeskStub(c)).build()["feed.json"]
    assert fd["v"] == 1 and fd["night"] == c.night and fd["minScore"] == 60
    assert fd["bars"] == {"alive_6h": 0.70, "organic": 0.65, "chase": 0.30}
    items = fd["items"]
    assert items, "feed should not be empty"
    ts = [i["t"] for i in items]
    assert ts == sorted(ts, reverse=True)

    by = {(i["stage"], i["outcome"], i["mint"]): i for i in items}

    kill = by[("scanner", "kill", M_KILL)]
    assert kill["name"] == "DeadCoin" and kill["symbol"] == "DEAD" and kill["reason"] == "liq $0.0K"
    assert kill["link"] == links.coin_url(M_KILL) and "r=savipww" in kill["link"]
    assert kill["mintShort"] == M_KILL[:4] + "…" + M_KILL[-4:]

    rec = by[("recheck", "recheck", M_RE)]
    assert rec["recheckN"] == 2 and "liq $5.4K" in rec["reason"]

    rooms = [i for i in items if i["mint"] == M_ROOM and i["stage"] == "room" and i["outcome"] == "waiting"]
    assert any(i["recheckN"] == 1 and "waiting room" in (i["reason"] or "") for i in rooms)
    assert any(i["recheckN"] is None and i["reason"] == "waiting room" for i in rooms)

    aud = by[("auditor", "fail", M_AUD)]
    assert aud["layer"] == 3 and aud["layerName"] == "Dev wallet" and aud["reason"] == "dev sold" and aud["passed"] is False

    aud_ok = by[("auditor", "pass", M_AN)]
    assert aud_ok["layerName"] == "8/8 clean" and aud_ok["passed"] is True

    an = by[("analyst", "no_buy", M_AN)]
    assert an["score"] == 59 and an["passed"] is False
    assert an["answers"]["alive_6h"] == 0.54 and an["answers"]["chase"] == 0.68
    assert "score 59" in an["failed"] and an["link"] == links.coin_url(M_AN)

    # a passing analyst would be buy
    M_BUY = "FeedBuy11111111111111111111111111111pump"
    _coin(c, M_BUY, NOW - 20, "Winner", "WIN", "passed")
    buy_body = {"agent": "analyst", "mint": M_BUY,
                "answers": {"alive_6h": 0.80, "organic": 0.70, "flush": 0.60, "chase": 0.20},
                "score": 72, "passed": True, "failed": []}
    c.db.x("INSERT INTO analyses(ts,mint,passed,failed,score,model,input_tokens,output_tokens,json,night) VALUES(?,?,?,?,?,?,?,?,?,?)",
           (NOW - 5, M_BUY, 1, "[]", 72, "jev-1.14.0", 10, 10, json.dumps(buy_body), c.night))
    fd2 = Builder(DeskStub(c)).build()["feed.json"]
    buy = next(i for i in fd2["items"] if i["mint"] == M_BUY and i["stage"] == "analyst")
    assert buy["outcome"] == "buy" and buy["score"] == 72 and buy["passed"] is True
    assert buy["t"] == fd2["items"][0]["t"]  # newest first


def test_feed_capped_at_100_and_contains_no_secrets():
    c, clock = make_ctx()
    clock.t = NOW
    for i in range(80):
        mint = f"{i:039d}pump"
        _coin(c, mint, NOW - 1000 + i, f"C{i}", f"C{i}", "killed")
        _scan(c, mint, NOW - 800 + i, "kill", f"liq ${i}.0K")
    files = Builder(DeskStub(c)).build()
    items = files["feed.json"]["items"]
    assert len(items) == 100
    # newest scan is C79; first_seen of later coins and scans interleave, but cap holds
    blob = json.dumps(files["feed.json"])
    for n in SECRET_NAMES:
        v = secrets_store.get(n)
        if v:
            assert v not in blob
    for k in LEAK_FIELDS:
        assert f'"{k}"' not in blob
    assert "r=savipww" in blob
