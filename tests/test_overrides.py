"""DECISION 36: owner override of the Analyst score for one coin (Telegram deep links from the app)."""
import asyncio, json
import pytest
from agents.executor import Executor
from agents.gmgn_trader import GmgnTrader
from overrides import Overrides
from publisher import Builder
from telegram import Telegram
from tests.helpers import make_ctx, pair, run
from tests.test_gmgn import FakeClient

M = "Uvrd111111111111111111111111111111111pump"
OWNER = 9127354951


async def _nosleep(*a, **k):
    return None


class SpyGmgn:
    """Stands in for the GMGN trader: records any call that could buy."""
    client = object()

    def __init__(self, mode="LIVE"):
        self._mode, self.calls = mode, []

    def mode(self):
        return self._mode

    def halted(self):
        return False

    def ticket_usd(self, liq):
        return 47.5, None

    async def on_pass(self, *a, **k):
        self.calls.append(a)
        return {"status": "open"}


class DeskStub:
    def __init__(self, c, gm):
        self.ctx, self.gmgn = c, gm
        c.gmgn = gm
        self.executor = Executor(c)
        self.overrides = Overrides(self)
        self.auditor = type("A", (), {"live": {}, "gpa_down_alerted": False})()
        self.analyst = type("A", (), {"live": {}})()


def _desk(real_gmgn=False, gmgn_mode="LIVE", client=None, **settings):
    base = {"risk.stop_pct": 15, "risk.trail_pct": 20, "scanner.min_liquidity_usd": 10000, "gmgn.cap_trade_usd": 65.0}
    base.update(settings)
    c, clock = make_ctx(**base)
    if real_gmgn:
        gm = GmgnTrader(c, client or FakeClient())
        c.s.set("gmgn.mode", gmgn_mode, source="test", force=True)
    else:
        gm = SpyGmgn(gmgn_mode)
    d = DeskStub(c, gm)
    c.net.pairs[M] = pair(M, price=1.0, liq=60000, name="OVR", symbol="OVR")
    return d, c, clock


def _reject(c, mint=M, score=41, price=1.0, passed=0, ts=None):
    c.db.x("INSERT INTO analyses(ts,mint,passed,failed,score,model,input_tokens,output_tokens,json,night) VALUES(?,?,?,?,?,?,?,?,?,?)",
           (ts or c.clock(), mint, passed, json.dumps([] if passed else [f"score {score}"]), score, "rules", 0, 0,
            json.dumps({"score": score, "passed": bool(passed), "priceUsd": price}), c.night))


def _tg(d):
    tg = Telegram.__new__(Telegram)                 # no token, no network: sends are captured
    tg.d, tg.c, tg.owner, tg.log = d, d.ctx, OWNER, d.ctx.logs["telegram"]
    tg.from_owner, tg.awaiting_live, tg.pending_cap, tg.gmgn_live_asked = False, False, None, 0.0
    tg.sent = []

    async def send(text, buttons=None, chat_id=None, mentions=None):
        tg.sent.append({"text": text, "buttons": buttons})

    async def api(method, **payload):
        return {}
    tg.send, tg.api = send, api
    return tg


def msg(chat, text):
    return {"update_id": 1, "message": {"chat": {"id": chat}, "text": text}}


def cb(user, data):
    return {"update_id": 2, "callback_query": {"id": "q", "from": {"id": user}, "data": data}}


# ---------- owner lock
def test_non_owner_refused():
    d, c, _ = _desk()
    _reject(c)
    tg = _tg(d)
    for txt in (f"/start ovp_{M}", f"/start ovr_{M}"):
        run(tg.handle(msg(12345, txt)))
    run(tg.handle(cb(12345, f"ovrc:{M}")))
    assert tg.sent == [] and c.db.one("SELECT id FROM overrides") is None
    assert c.db.one("SELECT id FROM positions") is None and d.gmgn.calls == []
    out = run(tg.command(f"/start ovp_{M}"))         # command path without the owner flag (e.g. not from Telegram)
    assert out[0].startswith("Refused") and c.db.one("SELECT id FROM positions") is None


# ---------- paper override
def test_ovp_paper_fill_never_reaches_gmgn():
    d, c, _ = _desk(gmgn_mode="LIVE")
    _reject(c, score=41)
    tg = _tg(d)
    run(tg.handle(msg(OWNER, f"/start ovp_{M}")))
    assert d.gmgn.calls == []
    push = c.notifier.sent[-1]["text"]
    assert push.startswith("PAPER · OVERRIDE · score 41 · Buy OVR"), push
    assert "r=savipww" in c.notifier.sent[-1]["buttons"][0][0]["url"]
    pos = c.db.one("SELECT * FROM positions WHERE mint=?", (M,))
    assert pos["mode"] == "PAPER" and pos["name"] == "OVERRIDE · OVR" and pos["stop_pct"] == 15
    assert c.db.one("SELECT reason FROM signals WHERE mint=?", (M,))["reason"] == "owner override"
    assert c.db.one("SELECT status FROM overrides WHERE mint=? AND kind='paper'", (M,))["status"] == "done"
    run(tg.handle(msg(OWNER, f"/start ovp_{M}")))      # once per mint
    assert "already overridden" in tg.sent[-1]["text"] and d.gmgn.calls == []
    assert c.db.one("SELECT COUNT(*) n FROM positions")["n"] == 1


def test_ovp_only_on_analyst_rejects_and_paper_desk():
    d, c, clock = _desk()
    tg = _tg(d)
    run(tg.handle(msg(OWNER, f"/start ovp_{M}")))       # no analyst decision (scanner/auditor kill): refused
    assert "didn't reject" in tg.sent[-1]["text"]
    _reject(c, passed=1, score=80)
    run(tg.handle(msg(OWNER, f"/start ovp_{M}")))       # passed coin: nothing to override
    assert "didn't reject" in tg.sent[-1]["text"]
    m2 = M[:-5] + "Lpump"
    c.net.pairs[m2] = pair(m2)
    _reject(c, mint=m2)
    c.s.set("desk.mode", "LIVE", source="test", force=True)
    run(tg.handle(msg(OWNER, f"/start ovp_{m2}")))
    assert "LIVE" in tg.sent[-1]["text"] and c.db.one("SELECT id FROM positions") is None


# ---------- real override
def test_ovr_does_nothing_without_confirm(monkeypatch):
    monkeypatch.setattr(asyncio, "sleep", _nosleep)
    d, c, _ = _desk(real_gmgn=True)
    _reject(c, score=44)
    tg = _tg(d)
    run(tg.handle(msg(OWNER, f"/start ovr_{M}")))
    ask = tg.sent[-1]
    assert ask["text"].startswith("REAL MONEY · OVERRIDE · score 44")
    for s in ("Liquidity $60,000", "MC $200,000", "Size $65.00 (less if free SOL is short: $20.00, then what it covers down to $1.00)", "0/4 open", "stop -30% for first 5 min, then -15%"):
        assert s in ask["text"], s
    assert ask["buttons"] == [[{"text": "Confirm real buy", "callback_data": f"ovrc:{M}"}, {"text": "Cancel", "callback_data": f"ovrx:{M}"}]]
    assert "swap" not in d.gmgn.client.kinds() and c.db.one("SELECT id FROM gmgn_orders") is None
    run(tg.handle(cb(OWNER, f"ovrx:{M}")))
    assert "cancelled" in tg.sent[-1]["text"]
    run(tg.handle(cb(OWNER, f"ovrc:{M}")))              # confirm after cancel: nothing pending
    assert "no pending confirm" in tg.sent[-1]["text"]
    assert "swap" not in d.gmgn.client.kinds() and c.db.one("SELECT id FROM gmgn_orders") is None


def test_ovr_confirm_buys_once_with_exits(monkeypatch):
    monkeypatch.setattr(asyncio, "sleep", _nosleep)
    d, c, _ = _desk(real_gmgn=True)
    _reject(c, score=44)
    tg = _tg(d)
    run(tg.handle(msg(OWNER, f"/start ovr_{M}")))
    run(tg.handle(cb(OWNER, f"ovrc:{M}")))
    swaps = [b for k, b in d.gmgn.client.calls if k == "swap"]
    assert len(swaps) == 1
    conds = {x["order_type"]: x for x in swaps[0]["condition_orders"]}
    assert conds["loss_stop"]["price_scale"] == "30" and "loss_stop_trace" not in conds
    assert conds["profit_stop_trace"]["drawdown_rate"] == "20"
    o = c.db.one("SELECT * FROM gmgn_orders WHERE mint=?", (M,))
    assert o["mode"] == "LIVE" and o["reason"] == "owner override" and o["size_usd"] == 65.0     # top size: wallet covers it
    assert "OVERRIDE · score 44" in tg.sent[-1]["text"]
    run(tg.handle(cb(OWNER, f"ovrc:{M}")))              # second Confirm: spent
    run(tg.handle(msg(OWNER, f"/start ovr_{M}")))       # and no new ask either
    assert len([1 for k, _ in d.gmgn.client.calls if k == "swap"]) == 1
    assert "already" in tg.sent[-1]["text"]


def test_ovr_expiry_and_freshness(monkeypatch):
    monkeypatch.setattr(asyncio, "sleep", _nosleep)
    d, c, clock = _desk(real_gmgn=True)
    _reject(c)
    tg = _tg(d)
    run(tg.handle(msg(OWNER, f"/start ovr_{M}")))
    clock.t += 301                                       # confirm expires after 5 min
    run(tg.handle(cb(OWNER, f"ovrc:{M}")))
    assert "expired" in tg.sent[-1]["text"]
    run(tg.handle(msg(OWNER, f"/start ovr_{M}")))       # ask again (expired asks aren't spent)
    c.net.pairs[M] = pair(M, price=0.79, liq=60000)     # -21% since the reject
    run(tg.handle(cb(OWNER, f"ovrc:{M}")))
    assert "down 21%" in tg.sent[-1]["text"]
    for mint, p, why in ((M[:-5] + "Qpump", pair(M[:-5] + "Qpump", liq=9000), "scanner floor"),
                         (M[:-5] + "Apump", pair(M[:-5] + "Apump"), "min old")):
        _reject(c, mint=mint, ts=clock.t - (29 * 60 if why == "min old" else 0))
        c.net.pairs[mint] = p
        run(tg.handle(msg(OWNER, f"/start ovr_{mint}")))
        if why == "min old":
            clock.t += 2 * 60                            # reject now 31 min old, confirm still inside 5 min
        run(tg.handle(cb(OWNER, f"ovrc:{mint}")))
        assert why in tg.sent[-1]["text"], tg.sent[-1]["text"]
    assert "swap" not in d.gmgn.client.kinds() and c.db.one("SELECT id FROM gmgn_orders WHERE status!='refused'") is None


def test_ovr_respects_caps_and_reserve(monkeypatch):
    monkeypatch.setattr(asyncio, "sleep", _nosleep)
    # night ticket cap
    d, c, _ = _desk(real_gmgn=True)
    for i in range(4):
        d.gmgn._row(ts=1, night=c.night, mode="LIVE", mint=f"m{i}", status="closed", size_usd=47.5)
    _reject(c)
    tg = _tg(d)
    run(tg.handle(msg(OWNER, f"/start ovr_{M}")))
    run(tg.handle(cb(OWNER, f"ovrc:{M}")))
    assert "ticket cap reached" in tg.sent[-1]["text"] and "swap" not in d.gmgn.client.kinds()
    # wallet + 0.02 SOL reserve
    d, c, _ = _desk(real_gmgn=True, client=FakeClient(wallet_sol=0.022))      # even $1 + fees + rent + reserve doesn't fit
    _reject(c)
    tg = _tg(d)
    run(tg.handle(msg(OWNER, f"/start ovr_{M}")))
    run(tg.handle(cb(OWNER, f"ovrc:{M}")))
    assert "reserve" in tg.sent[-1]["text"] and "swap" not in d.gmgn.client.kinds()
    assert c.db.one("SELECT reason FROM gmgn_orders WHERE status='refused'")["reason"].startswith("owner override: ")
    # GMGN in PAPER: refused with a plain reason, no paper fill either
    d, c, _ = _desk(real_gmgn=True, gmgn_mode="PAPER")
    _reject(c)
    tg = _tg(d)
    run(tg.handle(msg(OWNER, f"/start ovr_{M}")))
    assert "PAPER mode" in tg.sent[-1]["text"] and tg.sent[-1]["buttons"] is None
    assert c.db.one("SELECT id FROM gmgn_orders") is None


# ---------- app buttons only on analyst rejects
def test_feed_overridable_only_on_fresh_analyst_rejects():
    d, c, clock = _desk()
    mp, mk = M[:-5] + "Ppump", M[:-5] + "Kpump"
    _reject(c, mint=M, score=41)
    _reject(c, mint=mp, score=77, passed=1)
    c.db.x("INSERT INTO scans(ts,mint,verdict,reason,json,night) VALUES(?,?,?,?,?,?)", (clock.t, mk, "kill", "liq", "{}", c.night))
    c.db.x("INSERT INTO audits(ts,mint,passed,reason,layer,json,night) VALUES(?,?,?,?,?,?,?)", (clock.t, mk, 0, "dev sold", 3, "{}", c.night))
    b = Builder(d)
    feed = b.feed()
    assert feed["tgBot"] and all(ch.isalnum() or ch == "_" for ch in feed["tgBot"])
    by = {(i["mint"], i["stage"]): i for i in feed["items"]}
    assert by[(M, "analyst")]["overridable"] is True
    assert by[(mp, "analyst")]["overridable"] is False
    assert all("overridable" not in i for i in feed["items"] if i["stage"] in ("scanner", "auditor"))
    sc = {i["mint"]: i for i in b.scores()["items"]}
    assert sc[M]["overridable"] is True and sc[mp]["overridable"] is False
    clock.t += 31 * 60                                    # reject older than 30 min: hidden
    assert next(i for i in b.feed()["items"] if i["mint"] == M and i["stage"] == "analyst")["overridable"] is False
    clock.t -= 31 * 60
    c.db.x("INSERT INTO overrides(ts,mint,kind,status,night) VALUES(?,?,?,?,?)", (clock.t, M, "paper", "done", c.night))
    assert next(i for i in b.feed()["items"] if i["mint"] == M and i["stage"] == "analyst")["overridable"] is False
    js = open("web/app.js").read()
    assert "it.overridablePaper === true" in js and "it.overridableReal === true" in js and "?start=${k}_${it.mint}" in js


# ---------- DECISION 37: analyst paper bypass must not hide the override buttons
def _bypass_buy(d, c, mint=M, score=41):
    """The real bypass path: analyst reject -> executor PAPER fill flagged bypass (reason 'analyst bypass')."""
    c.s.set("analyst.paper_bypass", True, source="test")
    _reject(c, mint=mint, score=score)
    out = run(d.executor.run({"scan": {"mint": mint, "name": "OVR"}, "analyst": {"score": score, "passed": False}, "bypass": True}))
    assert out["status"] == "bought" and out["reason"] == "analyst bypass"


def _flags(b, mint):
    it = next(i for i in b.feed()["items"] if i["mint"] == mint and i["stage"] == "analyst")
    sc = next(i for i in b.scores()["items"] if i["mint"] == mint)
    keys = ("overridable", "overridablePaper", "overridableReal")
    assert {k: it[k] for k in keys} == {k: sc[k] for k in keys}
    return it["overridablePaper"], it["overridableReal"]


def test_bypass_position_hides_only_paper_button():
    d, c, clock = _desk()
    _bypass_buy(d, c)
    b = Builder(d)
    assert _flags(b, M) == (False, True)
    clock.t += 31 * 60                                   # same 30-min rule for Real $
    assert _flags(b, M) == (False, False)
    clock.t -= 31 * 60
    c.db.x("INSERT INTO overrides(ts,mint,kind,status,night) VALUES(?,?,?,?,?)", (clock.t, M, "real", "done", c.night))
    assert _flags(b, M) == (False, False)                # override used: both hide


def test_plain_reject_shows_both_and_gmgn_order_hides_both():
    d, c, clock = _desk()
    _reject(c)
    b = Builder(d)
    assert _flags(b, M) == (True, True)
    for st, hidden in (("refused", False), ("skipped", False), ("open", True)):
        c.db.x("DELETE FROM gmgn_orders")
        c.db.x("INSERT INTO gmgn_orders(ts,night,mode,mint,status,size_usd) VALUES(?,?,?,?,?,?)", (clock.t, c.night, "LIVE", M, st, 47.5))
        assert _flags(b, M) == ((False, False) if hidden else (True, True)), st
    c.db.x("DELETE FROM gmgn_orders")
    # a non-bypass FOMO buy (e.g. owner paper override) hides both
    m2 = M[:-5] + "Fpump"
    _reject(c, mint=m2)
    c.db.x("INSERT INTO positions(mint,name,mode,entry_price,size_usd,peak,stop_pct,opened_at,status,night) VALUES(?,?,?,?,?,?,?,?,?,?)",
           (m2, "OVERRIDE · X", "PAPER", 1, 47.5, 1, 15, clock.t, "open", c.night))
    assert _flags(b, m2) == (False, False)


def test_ovr_works_on_bypass_paper_coin_and_ovp_says_already_on_paper(monkeypatch):
    monkeypatch.setattr(asyncio, "sleep", _nosleep)
    d, c, _ = _desk(real_gmgn=True)
    _bypass_buy(d, c, score=44)
    tg = _tg(d)
    run(tg.handle(msg(OWNER, f"/start ovp_{M}")))
    assert "already on paper" in tg.sent[-1]["text"]
    assert c.db.one("SELECT COUNT(*) n FROM positions")["n"] == 1 and "swap" not in d.gmgn.client.kinds()
    run(tg.handle(msg(OWNER, f"/start ovr_{M}")))
    assert tg.sent[-1]["text"].startswith("REAL MONEY · OVERRIDE · score 44")
    assert "swap" not in d.gmgn.client.kinds()            # still nothing without Confirm
    run(tg.handle(cb(OWNER, f"ovrc:{M}")))
    swaps = [b for k, b in d.gmgn.client.calls if k == "swap"]
    assert len(swaps) == 1 and swaps[0]["condition_orders"][0]["price_scale"] == "30"
    assert c.db.one("SELECT reason FROM gmgn_orders WHERE mint=?", (M,))["reason"] == "owner override"
    run(tg.handle(cb(OWNER, f"ovrc:{M}")))                # once
    assert len([1 for k, _ in d.gmgn.client.calls if k == "swap"]) == 1


def test_ovr_on_bypass_coin_keeps_freshness_checks(monkeypatch):
    monkeypatch.setattr(asyncio, "sleep", _nosleep)
    d, c, _ = _desk(real_gmgn=True)
    _bypass_buy(d, c)
    tg = _tg(d)
    run(tg.handle(msg(OWNER, f"/start ovr_{M}")))
    c.net.pairs[M] = pair(M, price=0.75, liq=60000)
    run(tg.handle(cb(OWNER, f"ovrc:{M}")))
    assert "down 25%" in tg.sent[-1]["text"] and "swap" not in d.gmgn.client.kinds()
