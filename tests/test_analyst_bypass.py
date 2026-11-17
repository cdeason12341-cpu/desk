"""DECISION 35: analyst.paper_bypass. Failed coins paper-fill on the FOMO desk for testing; never GMGN, never LIVE."""
import config
from agents.analyst import Analyst
from agents.executor import Executor
from tests.helpers import make_ctx, pair, run

M = "Bypa111111111111111111111111111111111pump"


class SpyGmgn:
    def __init__(self, mode):
        self.mode = mode
        self.calls = []

    async def on_pass(self, mint, name, symbol, price, liq, score=None, **kw):
        self.calls.append((mint, score))


def _setup(desk_mode="PAPER", gmgn_mode="LIVE", bypass=True):
    c, clock = make_ctx()
    c.s.set("desk.mode", desk_mode, source="test", force=True)
    c.s.set("analyst.paper_bypass", bypass, source="test")
    c.gmgn = SpyGmgn(gmgn_mode)
    c.net.pairs[M] = pair(M, price=2.0, liq=60000, name="BYP", symbol="BYP")
    return c


FAILED = {"agent": "analyst", "mint": M, "score": 41, "passed": False, "failed": ["score 41"], "_priceUsd": 2.0}
PASSED = {"agent": "analyst", "mint": M, "score": 77, "passed": True, "failed": [], "_priceUsd": 2.0}
ITEM = {"scan": {"mint": M, "name": "BYP", "symbol": "BYP", "liquidityUsd": 60000}, "audit": {}}


def _flow(c, res):
    """analyst forward -> executor run; lets GMGN tasks run."""
    fwd = Analyst(c).forward(ITEM, res)
    if fwd is None:
        return None, None
    ex = Executor(c)

    async def go():
        out = await ex.run(fwd)
        for t in list(ex.gmgn_tasks):
            await t
        return out
    return fwd, run(go())


def test_setting_defined_default_off():
    c, _ = make_ctx()
    assert config.BY_KEY["analyst.paper_bypass"].kind == "bool" and c.s["analyst.paper_bypass"] is False
    c.s.set("paper bypass", "on")
    assert c.s["analyst.paper_bypass"] is True


def test_a_bypass_never_reaches_gmgn_live_or_paper():
    for gm in ("LIVE", "PAPER"):
        c = _setup(gmgn_mode=gm)
        fwd, out = _flow(c, dict(FAILED))
        assert fwd["bypass"] is True
        assert c.gmgn.calls == [], gm
        assert out["status"] == "bought" and out["bypass"] is True and out["reason"] == "analyst bypass"
        assert "r=savipww" in out["link"]
        push = c.notifier.sent[-1]["text"]
        assert push.startswith("PAPER · BYPASS · score 41 · Buy BYP"), push
        pos = c.db.one("SELECT * FROM positions WHERE mint=?", (M,))
        assert pos["mode"] == "PAPER" and pos["name"] == "BYPASS · BYP" and pos["stop_pct"] == c.s["risk.stop_pct"]
        assert c.db.one("SELECT reason FROM signals WHERE mint=?", (M,))["reason"] == "analyst bypass"
        # executor also refuses to hand a bypass item to GMGN even if fed directly
        c2 = _setup(gmgn_mode=gm)
        run(Executor(c2).run(dict(ITEM, analyst=dict(FAILED), bypass=True)))
        assert c2.gmgn.calls == []


def test_b_bypass_off_unchanged():
    c = _setup(bypass=False)
    fwd, out = _flow(c, dict(FAILED))
    assert fwd is None and out is None
    assert c.db.one("SELECT id FROM positions") is None and c.gmgn.calls == []
    c = _setup(bypass=False)
    fwd, out = _flow(c, dict(PASSED))
    assert "bypass" not in fwd and "bypass" not in out
    assert c.notifier.sent[-1]["text"].startswith("PAPER · Buy BYP")
    assert c.db.one("SELECT name FROM positions")["name"] == "BYP"
    assert c.db.one("SELECT reason FROM signals")["reason"] is None
    assert c.gmgn.calls == [(M, 77)]


def test_c_desk_live_ignores_bypass():
    c = _setup(desk_mode="LIVE")
    fwd, out = _flow(c, dict(FAILED))
    assert fwd is None and out is None
    # even a hand-made bypass item is refused by the executor in LIVE
    out = run(Executor(c).run(dict(ITEM, analyst=dict(FAILED), bypass=True)))
    assert out is None and c.notifier.sent == [] and c.gmgn.calls == []
    assert c.db.one("SELECT id FROM signals") is None and c.db.one("SELECT id FROM positions") is None


def test_d_passed_coins_still_reach_gmgn():
    for gm in ("LIVE", "PAPER"):
        c = _setup(gmgn_mode=gm, bypass=True)
        fwd, out = _flow(c, dict(PASSED))
        assert "bypass" not in fwd and c.gmgn.calls == [(M, 77)]
        assert out["status"] == "bought" and c.notifier.sent[-1]["text"].startswith("PAPER · Buy BYP")
