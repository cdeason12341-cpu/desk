"""Analyst scores feed for the phone app: last 50 tonight, no secrets, FOMO links from links.py."""
import json
import links, secrets_store
from publisher import Builder
from tests.helpers import make_ctx
from tests.test_publisher_gmgn import DeskStub, SECRET_NAMES

M1 = "Score11111111111111111111111111111111pump"
M2 = "Score22222222222222222222222222222222pump"
NOW = 1_800_000_000.0


def _analysis(c, mint, ts, score, passed, answers, failed, name="Nyajira", symbol="NYA"):
    if not c.coin(mint):
        c.db.x("INSERT INTO coins(mint,name,symbol,status,migrated_at,first_seen,night) VALUES(?,?,?,?,?,?,?)",
               (mint, name, symbol, "passed", ts, ts, c.night))
    body = {"agent": "analyst", "mint": mint, "answers": answers, "score": score,
            "passed": bool(passed), "failed": failed, "model": "jev-1.14.0"}
    c.db.x("INSERT INTO analyses(ts,mint,passed,failed,score,model,input_tokens,output_tokens,json,night) VALUES(?,?,?,?,?,?,?,?,?,?)",
           (ts, mint, int(passed), json.dumps(failed), score, "jev-1.14.0", 10, 10, json.dumps(body), c.night))


def test_scores_shape_order_bought_and_min():
    c, clock = make_ctx(**{"analyst.min_score": 60})
    clock.t = NOW
    _analysis(c, M1, NOW - 120, 59, False,
              {"alive_6h": 0.54, "organic": 0.53, "flush": 0.27, "chase": 0.68},
              ["alive_6h 0.54", "organic 0.53", "chase 0.68", "score 59"], name="Alpha", symbol="ALP")
    _analysis(c, M2, NOW - 30, 72, True,
              {"alive_6h": 0.80, "organic": 0.70, "flush": 0.60, "chase": 0.20},
              [], name="Beta", symbol="BET")
    c.db.x("INSERT INTO gmgn_orders(ts,night,mode,mint,name,symbol,status,size_usd) VALUES(?,?,?,?,?,?,?,?)",
           (NOW, c.night, "LIVE", M2, "Beta", "BET", "open", 65))
    files = Builder(DeskStub(c)).build()
    sc = files["scores.json"]
    assert sc["v"] == 1 and sc["minScore"] == 60 and sc["night"] == c.night
    assert [i["mint"] for i in sc["items"]] == [M2, M1]  # newest first
    a, b = sc["items"][0], sc["items"][1]
    assert a["name"] == "Beta" and a["symbol"] == "BET" and a["score"] == 72
    assert a["passed"] is True and a["bought"] is True and a["failed"] == []
    assert a["answers"]["chase"] == 0.20 and a["mintShort"] == M2[:4] + "…" + M2[-4:]
    assert a["link"] == links.coin_url(M2)
    assert b["name"] == "Alpha" and b["passed"] is False and b["bought"] is False
    assert "score 59" in b["failed"] and b["link"] == links.coin_url(M1)
    # agents/analyst.json also carries minScore and recent
    ag = files["agents/analyst.json"]
    assert ag["minScore"] == 60 and ag["recent"][0]["mint"] == M2


def test_scores_capped_at_50_and_contains_no_secrets():
    c, clock = make_ctx()
    clock.t = NOW
    for i in range(52):
        mint = f"{i:039d}pump"
        _analysis(c, mint, NOW - i, 40, False,
                  {"alive_6h": 0.4, "organic": 0.4, "flush": 0.4, "chase": 0.4},
                  ["score 40"], name=f"C{i}", symbol=f"C{i}")
    files = Builder(DeskStub(c)).build()
    items = files["scores.json"]["items"]
    assert len(items) == 50
    assert items[0]["name"] == "C0" and items[-1]["name"] == "C49"
    blob = json.dumps(files["scores.json"])
    for n in SECRET_NAMES:
        v = secrets_store.get(n)
        if v:
            assert v not in blob
    # no broker/swap fields
    for k in ("quote", "body", "conditions", "tx", "order_id", "strategy_order_id"):
        assert f'"{k}"' not in blob
