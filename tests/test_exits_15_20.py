"""Owner 2026-11-15 12:36 CT: -15% stop, 20% trail. GMGN condition orders and texts must follow settings.
DECISION 48: new LIVE buys use a 5 min grace hard stop at -30% (no drawdown trail) unless grace is off."""
from agents import gmgn_trader
from agents.executor import buy_text
from agents.risk import exit_text
from tests.helpers import make_ctx


def test_gmgn_conditions_and_texts_follow_settings():
    c, _ = make_ctx()
    c.s.set("risk.stop_pct", 15, source="test")
    c.s.set("risk.trail_pct", 20, source="test")
    c.s.set("risk.grace_min", 0, source="test")
    conds = {x["order_type"]: x for x in gmgn_trader.conditions(c.s)}
    assert conds["loss_stop"]["price_scale"] == "15"
    assert conds["loss_stop_trace"]["drawdown_rate"] == "20"
    assert "profit_stop_trace" not in conds
    body = gmgn_trader.buy_body(c.s, "M" * 40 + "pump", 1000)
    assert body["condition_orders"] == gmgn_trader.conditions(c.s)
    rep = gmgn_trader.stop_strategy_body(c.s, "M" * 40 + "pump", 1.0, 100.0)
    assert abs(float(rep["check_price"]) - 0.85) < 1e-12          # USD: 1.0 * 0.85 (DECISION 52)
    assert buy_text("", "X", 65, 10, 70, c.s["risk.stop_pct"], c.s["risk.trail_pct"]).endswith("stop -15% · trail 20%")
    assert exit_text("", "stop", "X", {"stop_pct": 15}, -9.75, -15.0, c.s) == "X hit the -15% stop · -$9.75"
    assert c.s.set("stop", "-15%", source="test") == "stop -15% -> -15%"


def test_grace_conditions_skip_drawdown_trail():
    c, _ = make_ctx()
    c.s.set("risk.stop_pct", 15, source="test")
    c.s.set("risk.trail_pct", 10, source="test")
    assert c.s["risk.grace_min"] == 5 and c.s["risk.grace_stop_pct"] == 30
    grace = {x["order_type"]: x for x in gmgn_trader.conditions(c.s, "grace")}
    assert grace["loss_stop"]["price_scale"] == "30"
    assert "loss_stop_trace" not in grace
    assert "profit_stop_trace" not in grace
    assert gmgn_trader.buy_body(c.s, "M" * 40 + "pump", 1000)["condition_orders"] == gmgn_trader.conditions(c.s, "grace")
    assert gmgn_trader.exits_label(c.s) == "stop -30% for first 5 min, then -15%"
    assert "stop -30% for first 5 min, then -15%" in gmgn_trader.exits_text(c.s)
    normal = {x["order_type"]: x for x in gmgn_trader.conditions(c.s, "normal")}
    assert normal["loss_stop"]["price_scale"] == "15" and normal["loss_stop_trace"]["drawdown_rate"] == "10"
    c.s.set("risk.grace_min", 0, source="test")
    assert gmgn_trader.exits_label(c.s) == "stop -15% · trail 10%"
    assert "loss_stop_trace" in {x["order_type"] for x in gmgn_trader.buy_body(c.s, "M" * 40 + "pump", 1000)["condition_orders"]}
