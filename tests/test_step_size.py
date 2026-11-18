"""DECISION 84 (owner 2026-11-18 5:21 PM CT): LIVE A size steps $75 -> $50 -> $20 -> shrink to the $1 floor."""
from agents.gmgn_trader import pick_size, buy_cost_lamports, size_ladder_text
from tests.helpers import make_ctx, run
from tests.test_gmgn import FakeClient, trader, go_live
import config, gmgn
import pytest

L = gmgn.LAMPORTS
PX = 100.0


def _s(step=50.0, cap=75.0):
    c, _ = make_ctx()
    c.s.set("gmgn.cap_trade_usd", cap, source="test", force=True)
    c.s.set("gmgn.step_trade_usd", step, source="test", force=True)
    return c.s


def _need(s, usd):
    return buy_cost_lamports(s, int(usd / PX * L))


def test_top_when_affordable():
    s = _s(); assert pick_size(s, _need(s, 75) + 10, PX)[0] == 75.0


def test_step_50_when_75_does_not_fit():
    s = _s(); assert pick_size(s, _need(s, 75) - 10, PX)[0] == 50.0


def test_then_20():
    s = _s(); assert pick_size(s, _need(s, 50) - 10, PX)[0] == 20.0


def test_step_off_skips_to_20():
    s = _s(step=0); assert pick_size(s, _need(s, 75) - 10, PX)[0] == 20.0


def test_shrink_below_20_then_floor():
    s = _s()
    size, lam, why = pick_size(s, _need(s, 20) - 10, PX)
    assert why is None and 1.0 <= size < 20.0 and buy_cost_lamports(s, lam) <= _need(s, 20) - 10
    need1 = _need(s, 1)
    assert pick_size(s, need1, PX)[0] == 1.0
    size, _, why = pick_size(s, need1 - 1, PX)
    assert size is None and "can't cover even $1.00" in why


def test_step_ignored_unless_between_fallback_and_top():
    s = _s(step=75); assert pick_size(s, _need(s, 75) - 10, PX)[0] == 20.0
    s = _s(step=20); assert pick_size(s, _need(s, 75) - 10, PX)[0] == 20.0
    s = _s(step=10); assert pick_size(s, _need(s, 75) - 10, PX)[0] == 20.0


def test_size_ladder_text_and_status_line():
    s = _s()
    assert size_ladder_text(s) == "$75, then $50, then $20"
    s0 = _s(step=0)
    assert size_ladder_text(s0) == "$75, then $20"
    c, _, t = trader(FakeClient(wallet_sol=2.0))
    go_live(c)
    c.s.set("gmgn.cap_trade_usd", 75, source="test", force=True)
    c.s.set("gmgn.step_trade_usd", 50, source="test", force=True)
    run(t.wallet_lamports(fresh=True)); run(t.sol_usd())
    line = t.status_line()
    assert "size $75, then $50, then $20, then whatever free SOL covers down to $1.00" in line


def test_step_is_protected():
    c, _ = make_ctx()
    assert "gmgn.step_trade_usd" in config.CAP_KEYS and "gmgn.step_trade_usd" in config.PROTECTED
    with pytest.raises(config.SettingError):
        c.s.set("gmgn.step_trade_usd", 50)
    c.s.set("gmgn.step_trade_usd", 50, source="test", force=True)
    assert c.s["gmgn.step_trade_usd"] == 50.0
