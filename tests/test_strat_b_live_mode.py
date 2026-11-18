"""DECISION 66: Strategy B LIVE. Same entry logic; real GMGN fills/exits through Strategy A's client, wallet safety and caps.
Sizing: strat_b.trade_usd when free SOL covers it, else Strategy A's rule (fallback $20, then whatever free SOL covers, $1 floor)."""
import pytest
import config, gmgn, strat_b, strat_b_live
from agents.gmgn_trader import GmgnTrader, LAMPORTS, pick_size, open_count, committed_lamports, night_usage
from strat_b import StratB
from tests.chainsim import ChainSim
from tests.helpers import make_ctx, run
from tests.test_gmgn import FakeClient
from tests.test_strat_b import M, M2, _watch, _tick, _orders, _pair

SOL_USD = 70.0


class Client(FakeClient):
    def __init__(self, **kw):
        super().__init__(sol=SOL_USD, **kw)
        self.open_strats = []

    async def strategy_orders(self, base_token=None, kind="open", group_tag="STMix"):
        return {"list": list(self.open_strats) if kind == "open" else []}


def _live(wallet_sol=5.0, **settings):
    settings.setdefault("strat_b.mode", "LIVE")
    c, clock = make_ctx(**settings)
    cl = Client(wallet_sol=wallet_sol)
    t = GmgnTrader(c, cl)
    c.gmgn = t
    sb = StratB(c, log_files=False)
    c.strat_b = sb
    sb.live.ATTACH_GAP_S = 0
    sb.live.SETTLE_GAP_S = 0
    chain = ChainSim(c.net, M)
    return c, clock, sb, cl, chain


def _signal(c, clock, sb, chain=None, sol_paid=0.75, tokens=None):
    """Same entry path as the paper tests: watch, high 1.0, wait, dip 0.80, bounce 0.85 -> buy signal."""
    _watch(c, clock, sb)
    _tick(sb, clock, 1.0)
    clock.t += 50 * 60
    _tick(sb, clock, 0.80)
    if chain is not None:                                    # the buy tx "h" lands on chain with real SOL / tokens
        tok = tokens or int(sol_paid * SOL_USD / 0.85 * 1e6)
        chain.add("h", clock.t + 10, -int(sol_paid * LAMPORTS), tok)
        chain.balance = tok
    return _tick(sb, clock, 0.85)


def _swaps(cl):
    return [b for k, b in cl.calls if k == "swap"]


# ---------- sizing: $50 when covered, else A's rule
def test_pick_size_top_usd_follows_a_rule():
    c, _ = make_ctx()
    s = c.s
    big = 10 * LAMPORTS
    assert pick_size(s, big, SOL_USD, None, top_usd=50)[0] == 50
    assert pick_size(s, big, SOL_USD, None, top_usd=500)[0] == 50          # never above A's per-trade cap
    free30 = int(30 / SOL_USD * LAMPORTS)
    assert pick_size(s, free30, SOL_USD, None, top_usd=50)[0] == 20        # A's fallback size
    free5 = int(5 / SOL_USD * LAMPORTS)
    size, lam, why = pick_size(s, free5, SOL_USD, None, top_usd=50)
    assert size is not None and 1 <= size < 5 and why is None              # whatever free SOL covers, $1 floor
    assert pick_size(s, 0, SOL_USD, None, top_usd=50)[0] is None
    assert pick_size(s, big, SOL_USD) == pick_size(s, big, SOL_USD, None, top_usd=None)   # A unchanged


def test_live_buy_50_with_gmgn_stop_and_real_entry():
    c, clock, sb, cl, chain = _live(wallet_sol=5.0)
    _signal(c, clock, sb, chain)
    o = _orders(c)[0]
    assert o["mode"] == "LIVE" and o["status"] == "open" and o["size_usd"] == 50
    sw = _swaps(cl)
    assert len(sw) == 1 and sw[0]["from_address"] == gmgn.MAIN_WALLET and sw[0]["input_token"] == gmgn.SOL
    assert int(sw[0]["input_amount"]) == int(50 / SOL_USD * LAMPORTS)
    assert sw[0]["condition_orders"] == [{"order_type": "loss_stop", "side": "sell", "price_scale": "35", "sell_ratio": "100"}]
    assert o["entry_src"] == "chain" and o["entry_price"] == pytest.approx(0.75 * SOL_USD / (o["tokens"]), rel=1e-6)
    st = [b for k, b in cl.calls if k == "strategy_create"]               # GMGN showed no strategy -> stop created
    assert st and st[0]["sub_order_type"] == "stop_loss"
    assert float(st[0]["check_price"]) == pytest.approx(o["entry_price"] * 0.65, rel=1e-6)
    assert o["strategy_order_id"] == "s1"
    assert c.db.one("SELECT COUNT(*) n FROM gmgn_orders")["n"] == 0        # A's table untouched
    assert c.db.one("SELECT status FROM strat_b_watch WHERE mint=?", (M,))["status"] == "bought"
    texts = [m["text"] for m in c.notifier.sent]
    assert any(t.startswith("B LIVE · Buying Cat · $50.00") for t in texts)
    assert not any("PAPER" in t for t in texts)
    # global caps: B LIVE counts toward A's LIVE open / night counts
    assert open_count(c.db, "LIVE") == 1 and night_usage(c.db, c.night, "LIVE") == (1, 50.0)
    assert open_count(c.db, "PAPER") == 0


def test_live_size_falls_back_to_a_rule_when_wallet_short():
    c, clock, sb, cl, chain = _live(wallet_sol=0.02 + 30 / SOL_USD)     # reserve + ~$30
    _signal(c, clock, sb, chain, sol_paid=0.3)
    o = _orders(c)[0]
    assert o["size_usd"] == 20 and o["status"] == "open"
    c2, clock2, sb2, cl2, chain2 = _live(wallet_sol=0.02 + 6 / SOL_USD)  # reserve + ~$6
    _signal(c2, clock2, sb2, chain2, sol_paid=0.05)
    o2 = _orders(c2)[0]
    assert 1 <= o2["size_usd"] < 6 and o2["status"] == "open"


def test_live_refused_when_wallet_empty_then_cooldown():
    c, clock, sb, cl, chain = _live(wallet_sol=0.0)
    _signal(c, clock, sb)
    rows = _orders(c)
    assert len(rows) == 1 and rows[0]["status"] == "refused" and rows[0]["mode"] == "LIVE"
    assert "can't cover" in rows[0]["note"] and "0.02 SOL reserve" in rows[0]["note"]
    assert not _swaps(cl)
    assert c.db.one("SELECT status FROM strat_b_watch WHERE mint=?", (M,))["status"] == "watching"   # keeps watching
    pushes = [m["text"] for m in c.notifier.sent if "Skipped" in m["text"]]
    assert len(pushes) == 1 and pushes[0].startswith("B LIVE · Skipped Cat:")
    _tick(sb, clock, 0.84)                                                 # still in the zone: no retry for 10 min
    assert len(_orders(c)) == 1
    clock.t += 601
    _tick(sb, clock, 0.845, dt=0)
    assert len(_orders(c)) == 2 and len([m for m in c.notifier.sent if "Skipped" in m["text"]]) == 1   # pushed once per coin


def test_live_respects_halt_and_own_open_cap():
    c, clock, sb, cl, chain = _live(wallet_sol=5.0)
    c.gmgn.halt(True, "test")
    _signal(c, clock, sb, chain)
    assert _orders(c)[0]["status"] == "refused" and "halted" in _orders(c)[0]["note"]
    # DECISION 71: A LIVE's open positions no longer take B's slots (A at its max 1 doesn't block B)
    c2, clock2, sb2, cl2, chain2 = _live(wallet_sol=5.0, **{"gmgn.max_open": 1})
    c2.db.x("INSERT INTO gmgn_orders(ts,night,mode,mint,status,size_usd) VALUES(?,?,?,?,?,?)", (clock2.t, c2.night, "LIVE", M2, "open", 10))
    _signal(c2, clock2, sb2, chain2)
    assert _orders(c2)[0]["status"] == "open" and len(_swaps(cl2)) == 1
    # ...but B's own strat_b.live_max_open does
    c3, clock3, sb3, cl3, chain3 = _live(wallet_sol=5.0, **{"strat_b.live_max_open": 1})
    c3.db.x("INSERT INTO strat_b_orders(ts,night,mode,mint,name,status,size_usd,lamports,opened_at,entry_price,peak,last_price) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (clock3.t, c3.night, "LIVE", M2, "Other", "open", 10, int(10 / SOL_USD * LAMPORTS), clock3.t, 1.0, 1.0, 1.0))
    _signal(c3, clock3, sb3, chain3)
    assert not _swaps(cl3) and not [o for o in _orders(c3) if o["mint"] == M and o["status"] == "open"]


def test_live_take_profit_real_sell_and_chain_close():
    c, clock, sb, cl, chain = _live(wallet_sol=5.0)
    _signal(c, clock, sb, chain)
    o = _orders(c)[0]
    e = o["entry_price"]
    clock.t += 5
    run(sb.live.once(pairs=[_pair(M, price=e * 2.05)]))
    o = _orders(c)[0]
    assert o["status"] == "exiting" and o["exit_reason"] == strat_b.TP and o["sell_order_id"]
    sells = [b for b in _swaps(cl) if b["input_token"] == M]
    assert len(sells) == 1 and sells[0]["input_amount_bps"] == "10000"
    assert ("strategy_cancel" in cl.kinds())                              # GMGN stop removed after our sell
    # chain: tokens gone, sell tx found -> closed with real P&L (SOL in vs SOL out)
    chain.add("sellsig", clock.t + 3, int(1.5 * LAMPORTS), -int(round(o["tokens"] * 1e6)))
    chain.balance = 0
    clock.t += 20
    run(sb.live.once(pairs=[]))
    o = _orders(c)[0]
    assert o["status"] == "closed" and o["pnl_usd"] == pytest.approx((1.5 - 0.75) * SOL_USD, abs=0.01)
    assert o["sell_tx"] == "sellsig"
    t = [m["text"] for m in c.notifier.sent if m["text"].startswith("B LIVE · Sold")]
    assert t and "+$52.50" in t[0] and "SOL in" in t[0]
    # a second tick never sells again
    run(sb.live.once(pairs=[]))
    assert len([b for b in _swaps(cl) if b["input_token"] == M]) == 1


def test_gmgn_stop_fired_close_from_chain():
    c, clock, sb, cl, chain = _live(wallet_sol=5.0)
    _signal(c, clock, sb, chain)
    o = _orders(c)[0]
    chain.add("stopsig", clock.t + 30, int(0.45 * LAMPORTS), -int(round(o["tokens"] * 1e6)))
    chain.balance = 0
    clock.t += 60
    run(sb.live.once(pairs=[]))
    o = _orders(c)[0]
    assert o["status"] == "closed" and "stop" in o["exit_reason"] and o["pnl_usd"] == pytest.approx(-0.3 * SOL_USD, abs=0.01)
    assert not [b for b in _swaps(cl) if b["input_token"] == M]            # the desk sent no sell


def test_paper_position_stays_paper_and_pnl_never_mixed():
    c, clock, sb, cl, chain = _live(wallet_sol=5.0, **{"strat_b.mode": "PAPER"})
    from tests.test_strat_b import _buy
    _buy(c, clock, sb)                                                    # paper position opened while PAPER
    c.s.set("strat_b.mode", "LIVE", source="test", force=True)
    p = _orders(c)[0]
    assert p["mode"] == "PAPER"
    _tick(sb, clock, p["entry_price"] * 2.1)                              # paper TP -> paper close, no swap
    p = _orders(c)[0]
    assert p["status"] == "closed" and not _swaps(cl)
    c.db.x("INSERT INTO strat_b_orders(ts,night,mint,name,status,size_usd,pnl_usd,pct,mode,closed_at,opened_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
           (clock.t, c.night, M2, "Real", "closed", 20, -7.0, -35.0, "LIVE", clock.t, clock.t - 60))
    card = strat_b.scorecard(c)
    assert card["mode"] == "LIVE" and card["label"] == "B LIVE"
    assert card["bankroll"] == pytest.approx(1000 + p["pnl_usd"], abs=0.01)   # paper only
    assert card["closedAll"] == 1 and card["live"]["closedAll"] == 1
    assert card["live"]["pnlAllUsd"] == -7.0 and card["live"]["label"] == "B LIVE"
    assert card["live"]["closed"][0]["mode"] == "LIVE" and card["closed"][0]["mode"] == "PAPER"
    assert sb.bankroll() == pytest.approx(1000 + p["pnl_usd"], abs=0.01)


def test_daily_loss_stop_uses_real_pnl():
    c, clock, sb, cl, chain = _live(wallet_sol=5.0)
    c.db.x("INSERT INTO strat_b_orders(ts,night,mint,name,status,size_usd,pnl_usd,mode,closed_at,opened_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
           (clock.t, c.night, M2, "Paper loss", "closed", 50, -500.0, "PAPER", clock.t, clock.t))
    assert sb.can_buy() is None                                           # paper losses don't stop LIVE
    c.db.x("INSERT INTO strat_b_orders(ts,night,mint,name,status,size_usd,pnl_usd,mode,closed_at,opened_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
           (clock.t, c.night, M2, "Real loss", "closed", 50, -100.0, "LIVE", clock.t, clock.t))
    assert "daily loss stop" in sb.can_buy()


def test_paper_mode_never_calls_gmgn():
    c, clock, sb, cl, chain = _live(wallet_sol=5.0, **{"strat_b.mode": "PAPER"})
    from tests.test_strat_b import _buy
    _buy(c, clock, sb)
    run(sb.live.once(pairs=[]))
    assert not _swaps(cl) and _orders(c)[0]["mode"] == "PAPER"


def test_live_feed_and_report_labels():
    c, clock, sb, cl, chain = _live(wallet_sol=5.0)
    _signal(c, clock, sb, chain)
    lf = strat_b.live_feed(c)
    assert lf["mode"] == "LIVE" and lf["label"] == "B LIVE"
    card = strat_b.scorecard(c)
    assert card["live"]["open"][0]["label"] == "B LIVE" and card["live"]["open"][0]["stopOnGmgn"]
    assert card["open"] == []                                             # live position not in the paper list


def test_webpush_b_live_titles():
    import webpush
    r = webpush.render("strat_b", "B LIVE · Buying Cat · $20.00 (0.2857 SOL) · mc $200,000\npullback -20%", M)
    assert r["title"].startswith("B LIVE · Buying Cat $20.00")
    r = webpush.render("strat_b", "B LIVE · Sold Cat (take profit 2x) · +$52.50 (+100.0%) · held 12m\nOn-chain: ...", M)
    assert r["title"].startswith("B LIVE · ") and "PAPER" not in r["title"]
    r = webpush.render("strat_b", "B LIVE · Skipped Cat: free 0.0000 SOL can't cover even $1.00", M)
    assert r["title"].startswith("B LIVE · Skipped Cat")
    r = webpush.render("strat_b", "STRATEGY B · PAPER · Bought Cat · $50.00 @ $1\nmc", M)
    assert r["title"].startswith("B PAPER")


def test_telegram_b_live_owner_only_two_step():
    import types
    from telegram import Telegram
    c, clock, sb, cl, chain = _live(wallet_sol=5.0, **{"strat_b.mode": "PAPER"})
    tg = Telegram(types.SimpleNamespace(ctx=c, gmgn=c.gmgn, strat_b=sb))
    assert run(tg.command("b live"))[0].startswith("That command only works from the owner")
    assert run(tg.command("B LIVE"))[0].startswith("That command only works from the owner")
    assert c.s["strat_b.mode"] == "PAPER"
    tg.from_owner = True
    assert run(tg.command("B LIVE"))[0].startswith("Send 'b live' first")
    assert "Type B LIVE" in run(tg.command("b live"))[0] and c.s["strat_b.mode"] == "PAPER"
    assert "PAPER -> LIVE" in run(tg.command("B LIVE"))[0] and c.s["strat_b.mode"] == "LIVE"
    row = c.db.one("SELECT * FROM change_log WHERE key='strat_b.mode' ORDER BY id DESC LIMIT 1")
    assert row["source"] == "telegram owner B LIVE"
    assert "Strategy B LIVE" in run(tg.command("b status"))[0]
    assert c.s["gmgn.mode"] == "PAPER"                                    # A untouched
    assert "LIVE -> PAPER" in run(tg.command("b paper"))[0] and c.s["strat_b.mode"] == "PAPER"
    assert run(tg.command("b paper"))[0] == "Strategy B is already PAPER."


# ---------- DECISION 69: LIVE daily loss is real money (~$20), PAPER stays % of bankroll
def test_live_daily_loss_limit_is_real_money():
    c, clock, sb, cl, chain = _live(wallet_sol=5.0)
    assert sb.daily_loss_limit() == 20.0                      # no wallet cache yet: the $20 setting
    assert sb.can_buy() is None
    c.db.x("INSERT INTO strat_b_orders(ts,night,mint,name,status,size_usd,pnl_usd,mode,closed_at,opened_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
           (clock.t, c.night, M2, "Paper loss", "closed", 50, -500.0, "PAPER", clock.t, clock.t))
    assert sb.can_buy() is None                               # paper losses still don't stop LIVE
    c.db.x("INSERT INTO strat_b_orders(ts,night,mint,name,status,size_usd,pnl_usd,mode,closed_at,opened_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
           (clock.t, c.night, M2, "Real small", "closed", 50, -15.0, "LIVE", clock.t, clock.t))
    assert sb.can_buy() is None                               # -$15 is under the $20 live stop
    c.db.x("INSERT INTO strat_b_orders(ts,night,mint,name,status,size_usd,pnl_usd,mode,closed_at,opened_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
           (clock.t, c.night, M, "Real more", "closed", 50, -5.0, "LIVE", clock.t, clock.t))
    assert "daily loss stop" in sb.can_buy()                  # -$20 tonight hits the limit


def test_live_daily_loss_capped_at_wallet_usd():
    c, clock, sb, cl, chain = _live(wallet_sol=5.0)
    c.gmgn._bal = (clock.t, int(0.1 * LAMPORTS))             # 0.1 SOL
    c.gmgn._sol = (clock.t, 100.0)                           # $10 wallet
    assert c.gmgn.wallet_snapshot()["usd"] == 10.0
    assert sb.daily_loss_limit() == pytest.approx(10.0)      # min($20, $10 wallet)
    c.db.x("INSERT INTO strat_b_orders(ts,night,mint,name,status,size_usd,pnl_usd,mode,closed_at,opened_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
           (clock.t, c.night, M2, "Real", "closed", 20, -10.0, "LIVE", clock.t, clock.t))
    assert "daily loss stop" in sb.can_buy()


def test_paper_daily_loss_still_uses_bankroll_pct():
    c, clock, sb, cl, chain = _live(wallet_sol=5.0, **{"strat_b.mode": "PAPER"})
    assert sb.daily_loss_limit() == pytest.approx(100.0)     # 10% of $1,000
    c.db.x("INSERT INTO strat_b_orders(ts,night,mint,name,status,size_usd,pnl_usd,mode,closed_at,opened_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
           (clock.t, c.night, M2, "Paper", "closed", 50, -99.0, "PAPER", clock.t, clock.t))
    assert sb.can_buy() is None
    c.db.x("UPDATE strat_b_orders SET pnl_usd=-101")
    assert "daily loss stop" in sb.can_buy()
    c.db.x("INSERT INTO strat_b_orders(ts,night,mint,name,status,size_usd,pnl_usd,mode,closed_at,opened_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
           (clock.t, c.night, M, "Live row", "closed", 50, -100.0, "LIVE", clock.t, clock.t))
    # a leftover LIVE loss must not change the paper limit; already stopped on paper -$101
    assert sb.daily_loss_limit() == pytest.approx(100.0)
