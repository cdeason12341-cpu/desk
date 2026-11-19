"""DECISION 72: per-strategy profit reset (gmgn.pnl_since / strat_b.pnl_since) is display-only, history is kept,
and the publisher emits strat_feed.json for the Wallet / Strat A / Strat B tabs."""
import json
import pytest
import config, pnl_since, strat_b
from publisher import Builder
from tests.test_publisher_gmgn import _desk, _order, LEAK_FIELDS

M2 = "Pnl2111111111111111111111111111111111pump"
M3 = "Pnl3111111111111111111111111111111111pump"
M4 = "Pnl4111111111111111111111111111111111pump"


def _b(c, mint, pnl, closed_at, opened_at, status="closed", mode="LIVE", **kw):
    cols = dict(ts=opened_at, night=kw.pop("night", c.night), mode=mode, mint=mint, name=kw.pop("name", mint[:4]), status=status, size_usd=50.0,
                pnl_usd=pnl, pct=(pnl or 0) * 2, opened_at=opened_at, closed_at=closed_at, entry_price=1.0, last_price=1.0,
                signal_price=1.0, peak=1.0, **kw)
    return c.db.x(f"INSERT INTO strat_b_orders({','.join(cols)}) VALUES({','.join('?' * len(cols))})", tuple(cols.values())).lastrowid


def test_parse_iso_and_label():
    ts = pnl_since.parse("2026-11-18T00:20:00Z")
    assert ts == 1794961200
    assert pnl_since.label(ts) == "Nov 17 6:20 PM CT"
    assert pnl_since.iso(ts) == "2026-11-18T00:20:00Z"
    assert pnl_since.parse("") is None and pnl_since.parse("nope") is None


def test_settings_validate_and_not_protected():
    d, c, clock, t = _desk()
    assert c.s["gmgn.pnl_since"] == "" and c.s["strat_b.pnl_since"] == ""
    assert "gmgn.pnl_since" not in config.PROTECTED and "strat_b.pnl_since" not in config.PROTECTED
    with pytest.raises(config.SettingError):
        c.s.set("gmgn.pnl_since", "yesterday", source="test")
    c.s.set("gmgn.pnl_since", "2026-11-18T00:20:00Z", source="chat: owner asked 2026-11-17 6:20 PM CT")
    c.s.set("strat_b.pnl_since", "2026-11-18T00:20:00Z", source="chat: owner asked 2026-11-17 6:20 PM CT")
    log = c.db.rows("SELECT key, source FROM change_log WHERE key LIKE '%pnl_since'")
    assert {r["key"] for r in log} == {"gmgn.pnl_since", "strat_b.pnl_since"}


def test_a_reset_counts_only_closes_after_and_ignores_old_seed():
    d, c, clock, t = _desk(mode="LIVE")
    base = clock.t + 100
    _order(c, mint=M2, status="closed", pnl_usd=-9.75, pct=-15.0, closed_at=clock.t + 50, exit_reason="stop", name="Old")
    # opened before the reset, closes after it: counts
    _order(c, mint=M3, status="closed", pnl_usd=7.30, pct=19.2, closed_at=clock.t + 150, exit_reason="surge_top", name="Straddle")
    _order(c, mint=M4)                                                     # still open: not realized
    c.db.set_meta("gmgn_pnl_baseline_ts", clock.t - 1000)                  # old seed baseline is replaced by the reset
    c.db.set_meta("gmgn_pnl_manual_usd", 64.54)
    c.db.set_meta("gmgn_pnl_manual_wins", 2)
    c.s.set("gmgn.pnl_since", pnl_since.iso(base), source="test")
    files = Builder(d).build()
    real = files["trades.json"]["real"]
    assert real["pnlAllUsd"] == 7.30 and real["wins"] == 1 and real["losses"] == 0 and real["closedTonight"] == 1
    assert real["since"] == pnl_since.iso(base)
    assert {r["name"] for r in real["closed"]} >= {"Old", "Straddle"}          # history kept
    assert files["state.json"]["realPnl"]["pnlAllUsd"] == 7.30
    pa = files["strat_feed.json"]["pnl"]["A"]
    assert pa["pnlUsd"] == 7.30 and pa["closed"] == 1 and pa["wins"] == 1 and pa["since"] == pnl_since.iso(base)
    assert c.db.one("SELECT COUNT(*) n FROM gmgn_orders")["n"] == 3            # nothing deleted
    line = t.status_line()
    assert "A LIVE profit since" in line and "$+7.30" in line and "1W 0L" in line


def test_b_reset_live_card_and_telegram():
    d, c, clock, t = _desk(mode="LIVE")
    base = clock.t + 100
    _b(c, M2, -20.46, clock.t + 10, clock.t - 500, name="JVOLT")
    _b(c, M3, 0.21, clock.t + 200, clock.t + 50, name="GM")                   # opened before, closed after: counts
    _b(c, M4, 99.0, clock.t + 200, clock.t + 50, mode="PAPER", name="Paper")  # paper never counts
    before = strat_b.live_card(c)
    assert before["pnlAllUsd"] == -20.25 and before["closedAll"] == 2 and before["since"] is None
    c.s.set("strat_b.pnl_since", pnl_since.iso(base), source="test")
    lc = strat_b.live_card(c)
    assert lc["pnlAllUsd"] == 0.21 and lc["closedAll"] == 1 and lc["wins"] == 1 and lc["losses"] == 0
    assert lc["earlierClosed"] == 1 and lc["earlierPnlUsd"] == -20.46 and lc["earlier"][0]["name"] == "JVOLT"
    assert [o["name"] for o in lc["closed"]] == ["GM"]
    pb = Builder(d).build()["strat_feed.json"]["pnl"]["B"]
    assert pb["pnlUsd"] == 0.21 and pb["closed"] == 1
    assert c.db.one("SELECT COUNT(*) n FROM strat_b_orders")["n"] == 3


def test_reset_never_touches_risk_numbers():
    """B's daily-loss stop reads night P&L, not the display baseline."""
    d, c, clock, t = _desk(mode="LIVE")
    _b(c, M2, -15.0, clock.t + 10, clock.t - 5)
    sb = strat_b.StratB(c, log_files=False)
    before = sb.night_pnl("LIVE")
    c.s.set("strat_b.pnl_since", pnl_since.iso(clock.t + 100), source="test")
    c.s.set("gmgn.pnl_since", pnl_since.iso(clock.t + 100), source="test")
    assert sb.night_pnl("LIVE") == before == -15.0


def test_strat_feed_per_strategy_events_and_no_leaks():
    d, c, clock, t = _desk(mode="LIVE")
    _order(c, mint=M2, status="closed", pnl_usd=3.0, pct=6.0, closed_at=clock.t + 5, exit_reason="trail", name="AWin")
    _order(c, mint=M3, status="refused", reason="A's share can't cover $50", name="ASkip")
    _b(c, M4, -2.0, clock.t + 5, clock.t, name="BLoss", exit_reason="stop")
    sf = Builder(d).build()["strat_feed.json"]
    a_titles = [e["title"] for e in sf["A"]]
    b_titles = [e["title"] for e in sf["B"]]
    assert "Bought AWin" in a_titles and "Sold AWin" in a_titles and "Skipped ASkip" in a_titles
    assert "Bought BLoss" in b_titles and "Stopped BLoss" in b_titles
    assert not any("BLoss" in x for x in a_titles) and not any("AWin" in x for x in b_titles)
    sold = next(e for e in sf["A"] if e["title"] == "Sold AWin")
    assert sold["pnlUsd"] == 3.0 and sold["kind"] == "sell"
    blob = json.dumps(sf)
    assert "strat_ok" not in blob and "ord_secretish" not in blob and "SHOULD_NOT_PUBLISH" not in blob
    for k in LEAK_FIELDS:
        assert f'"{k}"' not in blob, k


def test_tonight_resets_with_night_total_stays_since_baseline():
    """DECISION 83: main figure is the current 8 AM CT night; Total is LIVE since pnl_since."""
    d, c, clock, t = _desk(mode="LIVE")
    base = clock.t - 1000
    c.s.set("gmgn.pnl_since", pnl_since.iso(base), source="test")
    c.s.set("strat_b.pnl_since", pnl_since.iso(base), source="test")
    last = "2026-11-17-0800"
    _order(c, mint=M2, status="closed", pnl_usd=10.00, pct=20.0, closed_at=clock.t - 100, night=last, name="YestA")
    _order(c, mint=M3, status="closed", pnl_usd=5.25, pct=10.5, closed_at=clock.t + 10, name="TodayA")
    _order(c, mint=M4, status="closed", pnl_usd=99.0, pct=99.0, closed_at=clock.t + 20, mode="PAPER", name="PaperA")
    _b(c, M2, -4.00, clock.t - 100, clock.t - 200, name="YestB", night=last)
    _b(c, M3, 2.50, clock.t + 10, clock.t, name="TodayB")
    _b(c, M4, 50.0, clock.t + 20, clock.t, mode="PAPER", name="PaperB")
    files = Builder(d).build()
    pa, pb = files["strat_feed.json"]["pnl"]["A"], files["strat_feed.json"]["pnl"]["B"]
    assert pa["pnlTonightUsd"] == 5.25 and pa["pnlUsd"] == 15.25
    assert pa["closedTonight"] == 1 and pa["closed"] == 2
    assert pa["winsTonight"] == 1 and pa["lossesTonight"] == 0 and pa["winRateTonightPct"] == 100.0
    assert pa["wins"] == 2 and pa["losses"] == 0
    assert pb["pnlTonightUsd"] == 2.50 and pb["pnlUsd"] == -1.50
    assert pb["closedTonight"] == 1 and pb["closed"] == 2
    assert pb["winsTonight"] == 1 and pb["lossesTonight"] == 0
    assert pb["wins"] == 1 and pb["losses"] == 1
    assert files["trades.json"]["real"]["pnlTonightUsd"] == 5.25
    assert files["trades.json"]["real"]["pnlAllUsd"] == 15.25
    # after the 8 AM CT rollover, tonight is $0 and the total is unchanged
    c.night = "2026-11-19-0800"
    rolled = Builder(d).build()
    ra, rb = rolled["strat_feed.json"]["pnl"]["A"], rolled["strat_feed.json"]["pnl"]["B"]
    assert ra["pnlTonightUsd"] == 0 and ra["pnlUsd"] == 15.25 and ra["closedTonight"] == 0
    assert ra["winsTonight"] == 0 and ra["lossesTonight"] == 0 and ra["winRateTonightPct"] is None
    assert rb["pnlTonightUsd"] == 0 and rb["pnlUsd"] == -1.50 and rb["closedTonight"] == 0


def test_app_shows_tonight_main_and_total_aside():
    """Shell: big number is pnlTonightUsd; muted Total $X sits beside it on A, B, and Wallet."""
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    js = (root / "web" / "app.js").read_text()
    css = (root / "web" / "app.css").read_text()
    html = (root / "web" / "index.html").read_text()
    sw = (root / "web" / "sw.js").read_text()
    assert 'const V = "desk-shell-v40"' in sw
    assert "app.js?v=38" in html and "app.css?v=38" in html
    assert "desk-shell-v35" not in sw and "v=35" not in html
    assert "function profitCard" in js and "function stratCard" in js
    assert "const t = p?.pnlTonightUsd ?? 0, tot = p?.pnlUsd ?? 0" in js
    assert "Total ${money(tot)}" in js
    assert js.count("Total ${money(tot)}") == 2          # Strat overview card and Wallet summary card
    assert "<div class=\"k2\">Tonight</div>" in js
    assert "Profit ${esc(sinceText(p))}" not in js        # old main label is gone
    assert "tonight ${money(p?.pnlTonightUsd ?? 0)}" not in js
    assert ".pnltotal" in css and "pnltotal" in js and "pnlrow" in js


def test_app_v36_header_and_size_ladder():
    """Shell v36: A+B LIVE header when both on, size ladder on A and B, 50/50 split when B is live."""
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    js = (root / "web" / "app.js").read_text()
    html = (root / "web" / "index.html").read_text()
    sw = (root / "web" / "sw.js").read_text()
    assert 'const V = "desk-shell-v40"' in sw
    assert "app.js?v=38" in html
    assert "function bIsLive" in js and "function sizeLadder" in js and "function aIsLive" in js
    assert "A+B LIVE (GTA)" in js and "bLiveLabel" in js
    assert 'bOn ? bLab : "A LIVE"' in js
    assert "bBadge(\"LIVE\")" not in js
    assert "Strategy B is off" in js and "B is paused and places no real orders" in js
    assert "A ${bg.A.sharePct}% of wallet" in js
    assert "sizeLabel" in js
    assert "function shareLine(bu, who, size)" in js
    assert "sizeLadder(gm)" in js
    assert "b.sizeLabel" in js
    assert "function bLiveLabel" in js and "function bProfile" in js
    assert "LIVE (GTA)" in js and "GTA profile is LIVE" in js
    assert "A+B LIVE (GTA)" in js
    assert "startsWith(\"LIVE\")" in js


def test_a_paper_pnl_and_feed_separate_from_live():
    """DECISION 90: paper A P&L and events are published separately and never mixed into live A."""
    d, c, clock, t = _desk(mode="LIVE")
    c.s.set("gmgn.mode", "PAPER", source="test", force=True)
    base = clock.t - 1000
    c.db.set_meta("gmgn_paper_bankroll_since", base)
    last = "2026-11-17-0800"
    _order(c, mint=M2, status="closed", pnl_usd=18.64, pct=12.0, closed_at=clock.t + 10, mode="PAPER", name="JACK")
    _order(c, mint=M3, status="closed", pnl_usd=-31.11, pct=-20.0, closed_at=clock.t + 20, mode="PAPER", name="Peanut")
    _order(c, mint=M4, status="open", size_usd=200.0, mode="PAPER", name="Rockstar", pnl_usd=5.0, pct=2.5)
    _order(c, mint="Pnl5111111111111111111111111111111111pump", status="closed", pnl_usd=50.0, pct=10.0,
           closed_at=clock.t - 2000, mode="PAPER", name="OldPaper", night=last)
    _order(c, mint="Pnl6111111111111111111111111111111111pump", status="closed", pnl_usd=9.00, pct=10.0,
           closed_at=clock.t + 30, mode="LIVE", name="LiveA")
    files = Builder(d).build()
    pa, pap = files["strat_feed.json"]["pnl"]["A"], files["strat_feed.json"]["pnl"]["A_paper"]
    assert pa["pnlTonightUsd"] == 9.00 and pa["pnlUsd"] == 9.00 and pa["mode"] == "LIVE"
    assert pap["mode"] == "PAPER"
    assert pap["pnlTonightUsd"] == round(18.64 - 31.11, 2) == -12.47
    assert pap["pnlUsd"] == -12.47
    assert pap["closedTonight"] == 2 and pap["closed"] == 2
    assert pap["winsTonight"] == 1 and pap["lossesTonight"] == 1
    assert pap["since"] == pnl_since.iso(base)
    titles = [e["title"] for e in files["strat_feed.json"]["A_paper"]]
    assert "Bought JACK" in titles and "Sold JACK" in titles
    assert "Bought Peanut" in titles and "Sold Peanut" in titles
    assert "Bought Rockstar" in titles
    assert all(e.get("mode") == "PAPER" for e in files["strat_feed.json"]["A_paper"] if e["kind"] != "approval")
    assert "LiveA" not in titles
    assert all("JACK" not in e["title"] and "Peanut" not in e["title"] for e in files["strat_feed.json"]["A"])
    paper = files["trades.json"]["paper"]
    assert any(r["name"] == "JACK" and r["source"] == "GMGN" for r in paper["closed"])
    assert any(r["name"] == "Rockstar" and r["open"] for r in paper["open"])
    # FOMO paper must not be required for A_paper totals
    blob = json.dumps(files["strat_feed.json"])
    for k in LEAK_FIELDS:
        assert f'"{k}"' not in blob, k


def test_app_v36_shows_paper_a_profit():
    """Shell v36: when A is PAPER, the app shows paper P&L and trades, clearly labeled, not as off/hidden."""
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    js = (root / "web" / "app.js").read_text()
    html = (root / "web" / "index.html").read_text()
    sw = (root / "web" / "sw.js").read_text()
    css = (root / "web" / "app.css").read_text()
    assert 'const V = "desk-shell-v40"' in sw
    assert "app.js?v=38" in html and "app.css?v=38" in html
    assert "function aIsPaper" in js and "function aPaperBody" in js
    assert "Strategy A · PAPER · not real money" in js
    assert "A PAPER" in js
    assert "pnl?.A_paper" in js
    assert 'A_paper' in js
    assert "isPaperTrade" not in js
    assert "isFomoPaperNoise" in js
    assert "These numbers are PAPER — not real money, and not the wallet." in js
    assert "Strategy A is paper trading — its profit is not real money" in js
    assert "PAPER · not real money" in js
    assert ".card.paper" in css
    # paper is not treated as off
    assert 'el.textContent = aOn ? "LIVE" : aPaper ? "PAPER" : "OFF"' in js
    assert 'mode === "PAPER"' in js


def test_account_changelog_skips_bad_json(tmp_path):
    """A bad change_log row must not break account.json / publish."""
    d, c, clock, t = _desk()
    c.db.x("INSERT INTO change_log(ts,key,old,new,source) VALUES(?,?,?,?,?)",
           (clock.t, "gmgn.broken_test_key", "not-json", "also-bad", "test-bad-json"))
    c.db.x("INSERT INTO change_log(ts,key,old,new,source) VALUES(?,?,?,?,?)",
           (clock.t + 1, "gmgn.enabled", json.dumps(True), json.dumps(True), "test-good-json"))
    acc = Builder(d).account()
    assert all(e["key"] != "gmgn.broken_test_key" for e in acc["changeLog"])
    assert any(e["key"] == "gmgn.enabled" and e["source"] == "test-good-json" for e in acc["changeLog"])
    json.dumps(acc)   # must stay valid JSON
