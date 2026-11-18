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
    cols = dict(ts=opened_at, night=c.night, mode=mode, mint=mint, name=kw.pop("name", mint[:4]), status=status, size_usd=50.0,
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
