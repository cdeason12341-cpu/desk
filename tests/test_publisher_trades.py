"""trades.json: Real (GMGN LIVE only) | Paper (all PAPER, bypass badged) split for the app. No secrets."""
import json
from agents.executor import Executor
from publisher import Builder
from tests.helpers import pair, run
from tests.test_publisher_gmgn import _desk, _order, LEAK_FIELDS

B = "Bypt111111111111111111111111111111111pump"


def test_real_paper_split_and_bypass_badge():
    d, c, clock, t = _desk(mode="LIVE")
    _order(c)                                                             # LIVE open
    _order(c, mint=B[:-5] + "Cpump", status="closed", pnl_usd=-9.75, pct=-15.0, closed_at=clock.t, exit_reason="stop")
    _order(c, mint=B[:-5] + "Gpump", mode="PAPER", name="GPaper")         # GMGN paper
    _order(c, mint=B[:-5] + "Rpump", status="refused", reason="cap")      # refused rows are not trades
    c.s.set("analyst.paper_bypass", True, source="test")
    c.net.pairs[B] = pair(B, price=1.0, liq=60000, name="BYT", symbol="BYT")
    c.db.x("INSERT INTO analyses(ts,mint,passed,failed,score,model,input_tokens,output_tokens,json,night) VALUES(?,?,?,?,?,?,?,?,?,?)",
           (clock.t, B, 0, '["score 38"]', 38, "rules", 0, 0, "{}", c.night))
    run(Executor(c).run({"scan": {"mint": B, "name": "BYT", "liquidityUsd": 60000},
                         "analyst": {"score": 38, "passed": False}, "bypass": True}))
    tr = Builder(d).build()["trades.json"]
    real, paper = tr["real"], tr["paper"]
    assert all(r["mode"] == "LIVE" for r in real["open"] + real["closed"])
    assert len(real["open"]) == 1 and len(real["closed"]) == 1 and real["pnlAllUsd"] == -9.75
    assert all(r["mode"] == "PAPER" for r in paper["open"] + paper["closed"])
    byp = [r for r in paper["open"] if r["bypass"]]
    assert len(byp) == 1 and byp[0]["score"] == 38 and byp[0]["name"] == "BYT" and byp[0]["source"] == "FOMO"
    assert "r=savipww" in byp[0]["link"]
    assert any(r["source"] == "GMGN" and r["name"] == "GPaper" for r in paper["open"])
    assert paper["bypassOn"] is True and paper["bypassCount"] == 1
    blob = json.dumps(tr)
    assert "strat_ok" not in blob and "ord_secretish" not in blob and "SHOULD_NOT_PUBLISH" not in blob
    for k in LEAK_FIELDS:
        assert f'"{k}"' not in blob, k


def test_real_pnl_baseline_seeds_home_totals_and_keeps_history():
    """Home Real P&L can be reset at a timestamp and seeded with manual profits; history stays in the closed list."""
    d, c, clock, t = _desk(mode="LIVE")
    # Times sit inside this test night (clock starts at 1_800_000_000).
    old_close, baseline, new_close = clock.t + 50, clock.t + 100, clock.t + 200
    # a loss closed before the baseline — stays in history, drops out of the totals
    _order(c, mint="Old1111111111111111111111111111111111pump", status="closed",
           pnl_usd=-9.75, pct=-15.0, closed_at=old_close, exit_reason="stop", name="Old")
    _order(c, mint="New1111111111111111111111111111111111pump", status="closed",
           pnl_usd=3.00, pct=6.0, closed_at=new_close, exit_reason="trail", name="New")
    _order(c, mint="Pap1111111111111111111111111111111111pump", mode="PAPER", status="closed",
           pnl_usd=8.00, pct=10.0, closed_at=new_close, exit_reason="trail", name="PaperWin")
    # no baseline: all-time includes the old LIVE loss
    tr = Builder(d).build()["trades.json"]
    assert tr["real"]["pnlAllUsd"] == -6.75 and tr["real"]["pnlTonightUsd"] == -6.75
    assert tr["paper"]["pnlAllUsd"] == 8.00
    names = {r["name"] for r in tr["real"]["closed"]}
    assert "Old" in names and "New" in names

    c.db.set_meta("gmgn_pnl_baseline_ts", baseline)
    c.db.set_meta("gmgn_pnl_manual_usd", 64.54)
    c.db.set_meta("gmgn_pnl_manual_wins", 2)
    tr = Builder(d).build()["trades.json"]
    real, paper = tr["real"], tr["paper"]
    # 64.54 seed + the $3 trade after the baseline; the -$9.75 trade is excluded from totals
    assert real["pnlAllUsd"] == 67.54 and real["pnlTonightUsd"] == 67.54
    assert real["closedTonight"] == 1 and real["wins"] == 3 and real["losses"] == 0
    assert {r["name"] for r in real["closed"]} == names          # full history still listed
    assert paper["pnlAllUsd"] == 8.00 and paper["pnlTonightUsd"] == 8.00  # paper unchanged
    st = Builder(d).build()["state.json"]
    assert st["realPnl"]["pnlAllUsd"] == 67.54 and st["realPnl"]["pnlTonightUsd"] == 67.54

