"""STEP 13.4 test_executor."""
import config
from agents.executor import Executor, size_ticket
from tests.helpers import make_ctx, pair, run

M = "Exec111111111111111111111111111111111pump"


def test_size_200_bump_and_skip():
    c, _ = make_ctx()
    r = size_ticket(c.s, 200, 200, 1_000_000)
    assert r["raw"] == 16.0 and r["size"] == 47.5 and r["status"] == "ok"
    c.s.set("below min", "skip")
    r = size_ticket(c.s, 200, 200, 1_000_000)
    assert r["status"] == "skipped" and r["size"] == 0


def test_pool_cap():
    c, _ = make_ctx()
    r = size_ticket(c.s, 1_000_000, 1_000_000, 40000)
    assert r["size"] == 800.0 and r["cap"] == "max_pool_pct"


def test_ticket_check_messages():
    assert config.ticket_check(200, 8, 47.5, "bump")["msg"] == "tickets bumped to $47.50 = 23.75% of stack"
    t = config.ticket_check(200, 8, 47.5, "skip")
    assert not t["ok"] and t["msg"].startswith("every coin will be skipped, stack under $593.75")
    assert config.ticket_check(593.75, 8, 47.5, "skip")["ok"]


def test_paper_start_skip_warns(tmp_path):
    import main
    d = main.Desk(db_path=str(tmp_path / "e.db"), log_files=False, with_telegram=False)
    d.settings.set("below min", "skip")
    run(d.night_start())
    msgs = [m["text"] for m in d.ctx.notifier.sent]
    assert any(m.startswith("PAPER · every coin will be skipped, stack under $593.75") for m in msgs), msgs
    d.settings.set("below min", "bump")
    d.ctx.notifier.sent.clear()
    run(d.night_start())
    assert d.ctx.notifier.sent[0]["text"] == "PAPER · tickets bumped to $47.50 = 23.75% of stack"


def test_executor_paper_fill_and_skip():
    c, clock = make_ctx()
    ex = Executor(c)
    c.net.pairs[M] = pair(M, price=2.0, liq=60000, name="EXC", symbol="EXC")
    out = run(ex.run({"scan": {"mint": M, "name": "EXC", "liquidityUsd": 60000}, "analyst": {"score": 77}, "ref_price": 2.0}))
    assert out["status"] == "bought" and out["sizeUsd"] == 47.5 and out["stackPct"] == 23.75 and "r=savipww" in out["link"]
    push = c.notifier.sent[-1]
    assert push["text"] == "PAPER · Buy EXC · $47.50 · 23.75% of stack · score 77"
    assert push["buttons"][0][0]["text"] == "Buy on FOMO"
    act = c.db.one("SELECT * FROM activity WHERE kind='buy'")
    assert act["title"] == "Bought EXC" and act["right1"] == "$47.50" and act["right2"] == "23.75% of stack"
    assert run(ex.run({"scan": {"mint": M, "name": "EXC", "liquidityUsd": 60000}, "analyst": {"score": 77}})) is None  # 1 push per night
    c.s.set("below min", "skip")
    m2 = M[:-5] + "Spump"
    c.net.pairs[m2] = pair(m2, price=1.0, liq=60000)
    out = run(ex.run({"scan": {"mint": m2, "name": "SK", "liquidityUsd": 60000}, "analyst": {"score": 77}}))
    assert out["status"] == "skipped" and out["reason"].startswith("below min")
    m3 = M[:-5] + "Ppump"
    out = run(ex.run({"scan": {"mint": m3, "name": "NP", "liquidityUsd": 60000}, "analyst": {"score": 77}}))
    assert out["status"] == "skipped" and out["reason"] == "no price"
