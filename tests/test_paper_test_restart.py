"""DECISION 58: owner asked 2026-11-16 6:52 PM CT to restart the (original FOMO) paper test after many changes.
History stays; PAPER balance, Paper P&L and the LIVE gate's full-PAPER-night count start again from the restart."""
import json, time
import paper_test
from main import Desk
from publisher import Builder
from telegram import paper_nights_done


def _desk(tmp_path):
    d = Desk(db_path=str(tmp_path / "t.db"), log_files=False, with_telegram=False)
    d._allow_respawn = False
    return d


def _closed(db, pnl, ts, mode="PAPER", night="n1"):
    db.x("INSERT INTO positions(mint,name,mode,entry_price,size_usd,status,opened_at,closed_at,pnl_usd,pct,night,stop_pct,peak) "
         "VALUES(?,?,?,?,?,?,?,?,?,?,?,20,1.0)", ("M" + str(ts), "X", mode, 1.0, 10.0, "closed", ts - 60, ts, pnl, pnl * 10, night))


def test_restart_resets_balance_pnl_and_gate_but_keeps_history(tmp_path):
    d = _desk(tmp_path)
    c, db = d.ctx, d.db
    t0 = time.time()
    c.night = "n1"
    db.x("INSERT OR REPLACE INTO nights(id,started_at,ended_at,mode,start_balance) VALUES('old',?,?,?,200)", (t0 - 90000, t0 - 50000, "PAPER"))
    _closed(db, -50.0, t0 - 1000)
    _closed(db, -20.0, t0 - 500)
    mode_before, gmgn_before = c.s["desk.mode"], c.s["gmgn.mode"]
    assert c.balance() == 200 - 70
    assert paper_nights_done(db) == 1
    b = Builder(d)
    assert b.trades()["paper"]["pnlAllUsd"] == -70

    new = paper_test.restart(db, c.s, "chat: owner asked 2026-11-16 6:52 PM CT restart paper test after many changes", now=t0)
    assert new["paper_base"] == 200 and new["paper_test_since"] == t0
    assert c.balance() == 200                                   # simulated stack starts again
    assert paper_nights_done(db) == 0                           # the old full PAPER night no longer counts
    paper = b.trades()["paper"]
    assert paper["pnlAllUsd"] == 0 and paper["wins"] == 0 and paper["losses"] == 0
    assert len(paper["closed"]) == 2                            # history still listed
    assert db.one("SELECT COUNT(*) n FROM positions")["n"] == 2 and db.one("SELECT COUNT(*) n FROM nights WHERE id='old'")["n"] == 1
    log = db.one("SELECT * FROM change_log WHERE key='desk.paper_test'")
    assert log["source"].startswith("chat: owner asked 2026-11-16 6:52 PM CT") and json.loads(log["new"])["paper_test_since"] == t0
    assert c.s.sync() == []                                     # not a setting: nothing hot-loads
    assert c.s["desk.mode"] == mode_before and c.s["gmgn.mode"] == gmgn_before

    _closed(db, 5.0, t0 + 100)                                  # new paper trades count from the restart
    assert c.balance() == 205
    assert b.trades()["paper"]["pnlAllUsd"] == 5 and b.trades()["paper"]["wins"] == 1
    db.x("INSERT INTO nights(id,started_at,ended_at,mode,start_balance) VALUES('new',?,?,?,200)", (t0 + 10, t0 + 80000, "PAPER"))
    assert paper_nights_done(db) == 1
    pts = b.chart()["points"]
    assert all(p["t"] >= b.chart()["points"][0]["t"] for p in pts)


def test_live_side_untouched_by_paper_restart(tmp_path):
    d = _desk(tmp_path)
    c, db = d.ctx, d.db
    t0 = time.time()
    _closed(db, 7.0, t0 - 100, mode="LIVE")
    before = Builder(d).trades()["real"]["pnlAllUsd"]
    paper_test.restart(db, c.s, "test", now=t0)
    assert Builder(d).trades()["real"]["pnlAllUsd"] == before == 7
