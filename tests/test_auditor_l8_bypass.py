"""DECISION 74: bypass layer 8 when getProgramAccounts is down; L4 stays strict; GPA 429 retries."""
import asyncio, json
import config, notif_label, stage_pings
from agents.auditor import (
    Auditor, L8_SKIP_NOTE, L8_SKIP_VALUE, layer8_verdict, tag_l8_skip, mint_l8_skipped, short,
)
from net import Net, RateLimited, SourceError
from publisher import Builder
from tests.helpers import make_ctx, run
from tests.test_publisher_gmgn import DeskStub

NOW = 1_800_000_000.0
MINT = "L8by" + "1" * 36 + "pump"
DEV = "Dev1111111111111111111111111111111111111111"
POOL = "Pool" + MINT[:8]
SUPPLY = 1_000_000_000_000
SOURCE = "chat: owner asked 2026-11-17 7:15 PM CT"


def _acct(owner, amount, delegate=None, pubkey=None):
    info = {"owner": owner, "tokenAmount": {"amount": str(amount)}}
    if delegate:
        info["delegate"] = delegate
    return {"pubkey": pubkey or ("Acc" + owner[:20]), "account": {"data": {"parsed": {"info": info}}}}


def _holders(n=40, amount=1_000_000_000, delegate=None, delegated=0):
    out = []
    for i in range(n):
        d = delegate if i < delegated else None
        out.append(_acct(f"Own{i:04d}" + "x" * 28, amount, d))
    return out


def _ctx(**settings):
    settings.setdefault("auditor.retries", 0)
    c, clock = make_ctx(**settings)
    clock.t = NOW
    c.db.x("INSERT INTO coins(mint,name,symbol,creator,creator_balance_raw,migrated_at,first_seen,status,night,pool) "
           "VALUES(?,?,?,?,?,?,?,?,?,?)",
           (MINT, "SkipCoin", "SKIP", DEV, "0", NOW - 1800, NOW - 1800, "passed", c.night, POOL))
    report = {"creator": DEV, "knownAccounts": {}, "insiderNetworks": [],
              "token": {"supply": str(SUPPLY)},
              "markets": [{"pubkey": POOL, "lp": {"lpLockedPct": 100}}]}

    async def rug_report(mint):
        return report
    c.net.rug_report = rug_report
    c.net.rpc_map = {
        "getAccountInfo": {"value": {"owner": "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
                                     "data": {"parsed": {"info": {
                                         "mintAuthority": None, "freezeAuthority": None, "supply": str(SUPPLY)}}}}},
        "getTokenAccountsByOwner": {"value": [{"account": {"data": {"parsed": {"info": {
            "tokenAmount": {"amount": "0"}}}}}}]},
    }

    async def tafs(mint, program, timeout=60):
        return _holders() if str(program).startswith("Tokenkeg") else []
    c.net.token_accounts_for_mint = tafs
    return c


def _layers(r):
    return {l["n"]: l for l in r["layers"]}


def test_setting_default_true_not_protected_and_source():
    c, _ = make_ctx()
    assert c.s["auditor.l8_bypass_on_down"] is True
    assert "auditor.l8_bypass_on_down" not in config.PROTECTED
    assert config.resolve("l8 bypass").key == "auditor.l8_bypass_on_down"
    assert config.resolve("layer 8 bypass").key == "auditor.l8_bypass_on_down"
    assert config.resolve("bypass layer 8").key == "auditor.l8_bypass_on_down"
    c.s.record_default_source("auditor.l8_bypass_on_down", SOURCE)
    row = c.db.one("SELECT key, old, new, source FROM change_log WHERE key='auditor.l8_bypass_on_down' ORDER BY id DESC")
    assert row["source"] == SOURCE and json.loads(row["new"]) is True
    c.s.set("l8 bypass", False, source="test")
    assert c.s["auditor.l8_bypass_on_down"] is False
    c.s.record_default_source("auditor.l8_bypass_on_down", SOURCE)   # already logged: no overwrite
    assert c.s["auditor.l8_bypass_on_down"] is False


def test_layer8_verdict_no_data_bypass_and_real_finding():
    assert layer8_verdict(None, 10, error=RateLimited("rpc 429"), bypass=True) == (True, L8_SKIP_VALUE, True)
    assert layer8_verdict(None, 10, error=RateLimited("rpc 429"), bypass=False) == (False, "no data: delegations", False)
    acc = [{"owner": "o", "amount": 1, "delegate": "Delg11111111111111111111111111111111111111", "pubkey": f"p{i}"}
           for i in range(12)]
    ok, value, skipped = layer8_verdict(acc, 10, bypass=True)
    assert ok is False and skipped is False and "12 wallets" in value and short(acc[0]["delegate"]) in value
    clean = [{"owner": "o", "amount": 1, "delegate": None, "pubkey": "p1"}]
    assert layer8_verdict(clean, 10, bypass=True) == (True, "None", False)


def test_gpa_down_fails_layer4_even_when_bypass_on():
    c = _ctx()
    assert c.s["desk.mode"] == "PAPER" and c.s["auditor.l8_bypass_on_down"] is True

    async def boom(mint, program, timeout=60):
        raise RateLimited("rpc 429 getProgramAccounts")
    c.net.token_accounts_for_mint = boom
    r = run(Auditor(c).run(MINT, {"mint": MINT, "pool": POOL, "name": "SkipCoin"}))
    L = _layers(r)
    assert r["passed"] is False and r["reason"] == "no data: top 10" and r["layersChecked"] == 4
    assert L[4]["pass"] is False and L[8]["pass"] is None and L[8]["value"] == "not checked"
    assert r["l8_skipped"] is False
    assert int(c.db.meta("layer8_kills_" + c.night, 0) or 0) == 0
    alerts = [m for m in c.notifier.sent if m["kind"] == "alert"]
    assert len(alerts) == 1
    assert alerts[0]["text"] == "layer 8 is down and being skipped"
    assert not alerts[0]["text"].startswith("PAPER")
    title = notif_label.headline("alert", alerts[0]["text"])[1]
    assert title == "Desk · layer 8 is down and being skipped"


def test_gpa_down_alert_every_coin_red_when_bypass_off():
    c = _ctx(**{"auditor.l8_bypass_on_down": False})

    async def boom(mint, program, timeout=60):
        raise RateLimited("rpc 429 getProgramAccounts")
    c.net.token_accounts_for_mint = boom
    r = run(Auditor(c).run(MINT, {"mint": MINT, "pool": POOL, "name": "SkipCoin"}))
    assert r["passed"] is False and r["reason"] == "no data: top 10"
    alerts = [m for m in c.notifier.sent if m["kind"] == "alert"]
    assert alerts[0]["text"] == "layer 8 down, every coin is red until rpc_url changes"
    assert not alerts[0]["text"].startswith("PAPER")


def test_l8_skip_when_accounts_fail_only_at_layer_8():
    c = _ctx()
    au = Auditor(c)
    calls = {"n": 0}

    async def flaky(mint, program, timeout=60):
        calls["n"] += 1
        if calls["n"] <= 2:          # Token + Token-2022 for layer 4
            return _holders() if str(program).startswith("Tokenkeg") else []
        raise RateLimited("rpc 429 getProgramAccounts")
    c.net.token_accounts_for_mint = flaky

    async def on_layer(L):
        if L["n"] == 4:
            au._acct_cache.pop("accts", None)

    async def go():
        return await au.run(MINT, {"mint": MINT, "pool": POOL, "name": "SkipCoin"}, on_layer=on_layer)
    r = run(go())
    L = _layers(r)
    assert r["passed"] is True and r["l8_skipped"] is True and r["layersChecked"] == 8
    assert L[4]["pass"] is True and L[8]["pass"] is True and L[8]["value"] == L8_SKIP_VALUE
    assert int(c.db.meta("layer8_kills_" + c.night, 0) or 0) == 0
    row = c.db.one("SELECT passed, reason, json FROM audits WHERE mint=?", (MINT,))
    body = json.loads(row["json"])
    assert row["passed"] == 1 and body["l8_skipped"] is True
    assert any(l["n"] == 8 and l["value"] == L8_SKIP_VALUE for l in body["layers"])
    pings = [m for m in c.notifier.sent if m["kind"] == "stage_auditor"]
    assert len(pings) == 1
    assert L8_SKIP_NOTE in pings[0]["text"]
    assert pings[0]["text"].startswith("🛡️ Audit pass · SkipCoin")
    title = notif_label.headline("stage_auditor", pings[0]["text"])[1]
    assert title == "Strat A · SkipCoin passed the audit · ⚠️ L8 skipped (rpc down)"
    assert mint_l8_skipped(c, MINT) is True


def test_l8_no_data_still_kills_when_bypass_off():
    c = _ctx(**{"auditor.l8_bypass_on_down": False})
    au = Auditor(c)

    async def flaky(mint, program, timeout=60):
        if getattr(flaky, "opened", False):
            raise RateLimited("rpc 429 getProgramAccounts")
        return _holders() if str(program).startswith("Tokenkeg") else []
    c.net.token_accounts_for_mint = flaky

    async def on_layer(L):
        if L["n"] == 4:
            au._acct_cache.pop("accts", None)
            flaky.opened = True

    r = run(au.run(MINT, {"mint": MINT, "pool": POOL, "name": "SkipCoin"}, on_layer=on_layer))
    assert r["passed"] is False and r["reason"] == "no data: delegations" and r["l8_skipped"] is False
    assert int(c.db.meta("layer8_kills_" + c.night, 0) or 0) == 1
    assert not any(m["kind"] == "stage_auditor" for m in c.notifier.sent)


def test_real_delegation_still_rejects_with_bypass_on():
    c = _ctx()
    dele = "Delg11111111111111111111111111111111111111"

    async def tafs(mint, program, timeout=60):
        return _holders(delegated=12, delegate=dele) if str(program).startswith("Tokenkeg") else []
    c.net.token_accounts_for_mint = tafs
    r = run(Auditor(c).run(MINT, {"mint": MINT, "pool": POOL, "name": "SkipCoin"}))
    L = _layers(r)
    assert r["passed"] is False and r["l8_skipped"] is False and r["layersChecked"] == 8
    assert L[4]["pass"] is True and L[8]["pass"] is False
    assert "12 wallets" in r["reason"] and short(dele) in r["reason"]
    assert int(c.db.meta("layer8_kills_" + c.night, 0) or 0) == 0


def test_clean_pass_unchanged():
    c = _ctx()
    r = run(Auditor(c).run(MINT, {"mint": MINT, "pool": POOL, "name": "SkipCoin"}))
    L = _layers(r)
    assert r["passed"] is True and r["l8_skipped"] is False and L[8]["value"] == "None"
    pings = [m for m in c.notifier.sent if m["kind"] == "stage_auditor"]
    assert pings[0]["text"].startswith("🛡️ Audit pass · SkipCoin · top10 1.0%") and L8_SKIP_NOTE not in pings[0]["text"]
    assert L8_SKIP_NOTE not in pings[0]["text"]


def test_feed_and_buy_notification_tag():
    c, clock = make_ctx()
    clock.t = NOW
    c.db.x("INSERT INTO coins(mint,name,symbol,migrated_at,first_seen,status,night) VALUES(?,?,?,?,?,?,?)",
           (MINT, "SkipCoin", "SKIP", NOW - 1800, NOW - 1800, "passed", c.night))
    body = {"agent": "auditor", "mint": MINT, "passed": True, "layersChecked": 8, "l8_skipped": True,
            "layers": [{"n": i, "name": n, "pass": True,
                        "value": (L8_SKIP_VALUE if i == 8 else "ok")}
                       for i, n in enumerate(
                           ["Mint", "Freeze", "Dev", "Top10", "Liq", "Age", "Bundles", "Delegations"], 1)]}
    c.db.x("INSERT INTO audits(ts,mint,passed,reason,layer,json,night) VALUES(?,?,?,?,?,?,?)",
           (NOW, MINT, 1, None, 8, json.dumps(body), c.night))
    fd = Builder(DeskStub(c)).feed()
    item = next(i for i in fd["items"] if i["mint"] == MINT and i["stage"] == "auditor")
    assert item["outcome"] == "pass" and item["l8Skipped"] is True
    assert item["reason"] == L8_SKIP_NOTE
    assert item["layerName"] == "8/8 · L8 skipped (rpc down)"

    from agents.executor import Executor
    au_text = stage_pings.auditor_text("SkipCoin", body)
    assert au_text.endswith(L8_SKIP_NOTE)
    tagged = tag_l8_skip("GMGN · Bought SkipCoin · $11.67 · 1/4 open", True)
    assert tagged.endswith(" · " + L8_SKIP_NOTE)
    assert tag_l8_skip(tagged, True) == tagged
    title = notif_label.headline("gmgn", tagged)[1]
    assert title == "Strat A bought SkipCoin $11.67 · ⚠️ L8 skipped (rpc down)"

    sent = []

    async def fake_push(text, buttons=None, kind="info", mint=None):
        sent.append(text)
        return True
    c.notifier.push = fake_push
    run(Executor(c).push("Bought SkipCoin · $11.67 · 23.75% of stack", None, MINT))
    assert L8_SKIP_NOTE in sent[0]


def test_gpa_429_retries_then_succeeds(monkeypatch):
    c, _ = make_ctx()
    n = Net(c.s)
    calls, slept = [], []

    async def rpc(method, params, timeout=30):
        calls.append(method)
        if len(calls) < 3:
            raise RateLimited("rpc 429 getProgramAccounts")
        return [{"ok": True}]

    async def sleep(d):
        slept.append(d)

    n.rpc = rpc
    monkeypatch.setattr(asyncio, "sleep", sleep)
    try:
        assert run(n.token_accounts_for_mint(MINT, "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA")) == [{"ok": True}]
    finally:
        run(n.close())
    assert calls == ["getProgramAccounts"] * 3 and slept == [1.0, 3.0]


def test_gpa_429_retries_exhausted(monkeypatch):
    c, _ = make_ctx()
    n = Net(c.s)
    slept = []

    async def rpc(method, params, timeout=30):
        raise RateLimited("rpc 429 getProgramAccounts")

    async def sleep(d):
        slept.append(d)

    n.rpc = rpc
    monkeypatch.setattr(asyncio, "sleep", sleep)
    try:
        try:
            run(n.token_accounts_for_mint(MINT, "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"))
            raise AssertionError("should have raised")
        except RateLimited as e:
            assert "429" in str(e)
    finally:
        run(n.close())
    assert slept == [1.0, 3.0]


def test_gpa_non_429_does_not_retry(monkeypatch):
    c, _ = make_ctx()
    n = Net(c.s)
    slept, calls = [], []

    async def rpc(method, params, timeout=30):
        calls.append(1)
        raise SourceError("rpc error getProgramAccounts: timeout")

    async def sleep(d):
        slept.append(d)

    n.rpc = rpc
    monkeypatch.setattr(asyncio, "sleep", sleep)
    try:
        try:
            run(n.token_accounts_for_mint(MINT, "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"))
            raise AssertionError("should have raised")
        except SourceError:
            pass
    finally:
        run(n.close())
    assert calls == [1] and slept == []
