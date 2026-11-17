"""DECISION 38: owner pings on scanner / audit / analyst pass (and analyst reject with override buttons)."""
import asyncio, json
import config, links, stage_pings
from agents.analyst import Analyst
from agents.auditor import Auditor
from agents.scanner import Scanner
from tests.helpers import make_ctx, pair

M = "Stge111111111111111111111111111111111pump"


async def _drain(c):
    for _ in range(3):
        ts = list(getattr(c, "_ping_tasks", ()))
        if ts:
            await asyncio.gather(*ts)
        await asyncio.sleep(0)


def _kinds(c, kind):
    return [m for m in c.notifier.sent if m["kind"] == kind]


def _audit_out(top10="23.4%"):
    return {"layers": [{"n": 4, "name": "Top 10", "pass": True, "value": top10}], "passed": True}


def test_setting_default_on_and_toggle():
    c, _ = make_ctx()
    assert config.BY_KEY["alerts.stage_pings"].kind == "bool" and c.s["alerts.stage_pings"] is True
    c.s.set("stage pings", "off")
    assert c.s["alerts.stage_pings"] is False


def test_scanner_pass_ping_once_with_fomo_link():
    c, clock = make_ctx()
    c.db.x("INSERT INTO coins(mint,name,symbol,migrated_at,first_seen,status,night) VALUES(?,?,?,?,?,?,?)",
           (M, "Stage", "STG", clock.t - 600, clock.t - 600, "room", c.night))
    sc = Scanner(c)
    p = pair(M, liq=12345, name="Stage", symbol="STG")

    async def go():
        sc.emit({"mint": M, "migrated_at": clock.t - 600}, p, 22.0, "pass", None, clock.t)
        await _drain(c)
        c.db.x("UPDATE coins SET sent=0 WHERE mint=?", (M,))       # even a second pass the same night (recheck) won't repeat it
        sc.emit({"mint": M, "migrated_at": clock.t - 600}, p, 22.0, "pass", None, clock.t + 60)
        await _drain(c)
    asyncio.run(go())
    msgs = _kinds(c, "stage_scanner")
    assert len(msgs) == 1
    assert msgs[0]["text"] == "✅ Scanner pass · Stage · liq $12.3K · MC $200.0K · via pump.fun"
    url = msgs[0]["buttons"][0][0]["url"]
    assert url == links.coin_url(M, "solana") and "r=savipww" in url and msgs[0]["mint"] == M


def test_scanner_kill_and_setting_off_send_nothing():
    c, clock = make_ctx()
    sc = Scanner(c)

    async def go():
        sc.emit({"mint": M, "migrated_at": clock.t - 600}, pair(M), 22.0, "kill", "liq $1K", clock.t)
        c.s.set("stage pings", "off")
        sc.emit({"mint": M[:-5] + "Bpump", "migrated_at": clock.t - 600}, pair(M[:-5] + "Bpump"), 22.0, "pass", None, clock.t)
        await _drain(c)
    asyncio.run(go())
    assert not [m for m in c.notifier.sent if m["kind"].startswith("stage_")]


def test_audit_pass_ping_once():
    c, _ = make_ctx()
    au = Auditor(c)

    async def go():
        au.ping_pass(M, {"name": "Stage"}, _audit_out())
        au.ping_pass(M, {"name": "Stage"}, _audit_out())
        await _drain(c)
    asyncio.run(go())
    msgs = _kinds(c, "stage_auditor")
    assert [m["text"] for m in msgs] == ["🛡️ Audit pass · Stage · top10 23.4%"]
    src = open("agents/auditor.py").read()
    assert "if passed:\n                self.ping_pass(mint, scan, out, reentry=reentry)" in src      # only on a pass, inside the recorded run


def _analyst(c, mint, **pk):
    c.net.pairs[mint] = pair(mint, **pk)
    c.s.set("analyst.mode", "rules", source="test")
    return Analyst(c)


def test_analyst_pass_ping_before_return_and_once():
    c, _ = make_ctx(**{"analyst.min_score": 50})
    an = _analyst(c, M, vol_h1=60000, buys_h1=400, h1=5.0, m5=-6.0, buys_m5=20, sells_m5=5, name="Stage")

    async def go():
        r = await an.run({"mint": M, "name": "Stage"}, {"layers": []})
        sent_at_return = list(_kinds(c, "stage_analyst"))
        await _drain(c)
        return r, sent_at_return
    r, at_return = asyncio.run(go())
    assert r["passed"] is True
    assert len(at_return) == 1                                       # already sent when the buy flow starts
    assert at_return[0]["text"].startswith(f"🎯 Analyst pass · score {r['score']} · Stage")
    assert stage_pings.ping(c, "analyst", M, "x") is None            # no loop / duplicate: nothing


def test_analyst_reject_ping_with_override_buttons():
    c, _ = make_ctx(**{"analyst.min_score": 50})
    an = _analyst(c, M, vol_h1=1000, buys_h1=10, h1=200.0, name="Stage")

    async def go():
        r = await an.run({"mint": M, "name": "Stage"}, {"layers": []})
        await _drain(c)
        return r
    r = asyncio.run(go())
    assert r["passed"] is False
    msgs = _kinds(c, "stage_analyst")
    assert len(msgs) == 1
    t = msgs[0]["text"]
    assert t.startswith(f"Analyst · score {r['score']} (min 50) · Stage") and "in the app" in t
    b = msgs[0]["buttons"]
    assert b[0] == [{"text": "Override · Paper", "url": f"https://t.me/Denarpobot?start=ovp_{M}"},
                    {"text": "Override · Real $", "url": f"https://t.me/Denarpobot?start=ovr_{M}"}]
    assert "r=savipww" in b[1][0]["url"]
    assert all(len(u["url"].split("start=")[1]) <= 64 for u in b[0])


def test_analyst_reject_with_bypass_on_offers_real_only_and_no_trading_change():
    c, _ = make_ctx(**{"analyst.min_score": 50, "analyst.paper_bypass": True})
    c.db.set_meta("tg_username", "TestDeskBot")
    an = _analyst(c, M, vol_h1=1000, buys_h1=10, h1=200.0, name="Stage")

    async def go():
        r = await an.run({"mint": M, "name": "Stage"}, {"layers": []})
        await _drain(c)
        return r
    r = asyncio.run(go())
    msgs = _kinds(c, "stage_analyst")
    assert msgs[0]["buttons"][0] == [{"text": "Override · Real $", "url": f"https://t.me/TestDeskBot?start=ovr_{M}"}]
    assert "Paper bypass is on" in msgs[0]["text"]
    # pings never create trades or signals
    for tbl in ("positions", "signals", "executions", "gmgn_orders"):
        assert c.db.one(f"SELECT COUNT(*) n FROM {tbl}")["n"] == 0, tbl
