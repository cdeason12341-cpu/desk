"""DECISION 73: Telegram + phone pushes say which strategy did what, with the action in the headline. Text only."""
import types
import pytest
import notif_label as nl
import webpush
from telegram import Telegram
from tests.helpers import make_ctx, run

M = "Labl111111111111111111111111111111111pump"

CASES = [
    ("gmgn", "GMGN · Bought Sloth · $50.00 · exits live on GMGN · stop -50%", "Strat A bought Sloth $50.00"),
    ("gmgn", "GMGN · Buying Sloth · $50.00 (0.6688 SOL) · 1/4 open\nstop -50%", "Strat A buying Sloth $50.00"),
    ("gmgn", "GMGN · Aura sold (server-side exit) · +$4.58 (+9.0%)\nOn-chain: …", "Strat A sold Aura +$4.58 (+9.0%) (GMGN exit)"),
    ("gmgn", "GMGN · Sloth: trailing take-profit · peaked +153%, now +57%, market-selling", "Strat A selling Sloth (trail)"),
    ("gmgn", "GMGN · Sloth: your manual buy is now managed by Strategy A (#60) · $11.67 · server-side -50% stop created on GMGN",
     "Strat A · your manual buy Sloth is now protected"),
    ("gmgn", "GMGN · Foo: exits weren't attached, so I created a server-side -50% stop. ok", "Strat A set a -50% stop on Foo"),
    ("gmgn_refused", "GMGN · Skipped Rock: free 0.0000 SOL can't cover even $1.00", "Strat A skipped Rock (free 0.0000 SOL can't cover even $1.00)"),
    ("gmgn_alert", "GMGN · ALERT nema: couldn't attach a server-side stop, so I market-sold it.", "Strat A ⚠️ alert · nema"),
    ("gmgn_approve", "GMGN · APPROVE? Sloth ($1.2M MC)\nMarket cap $1,200,000 is over your $500,000", "Strat A · approve Sloth?"),
    ("strat_b", "B LIVE · Sold JALU (stop (GMGN server-side)) · -$9.85 (-19.4%) · held 3.2h\nOn-chain: x", "Strat B sold JALU −$9.85 (−19%) (stop)"),
    ("strat_b", "B LIVE · Bought GM · $18.13 @ $0.000258751 · stop -35% live on GMGN", "Strat B bought GM $18.13"),
    ("strat_b", "B LIVE · Skipped Foo: wallet short", "Strat B skipped Foo (wallet short)"),
    ("strat_b", "B LIVE · NOLAW: your manual buy is now managed by Strategy B (#9) · $37.79 · server-side -35% stop created on GMGN",
     "Strat B · your manual buy NOLAW is now protected"),
    ("stage_scanner", "✅ Scanner pass · Sloth · liq $11.9K · MC $31.0K · via pump.fun", "Strat A · Sloth passed the scanner"),
    ("stage_analyst", "🎯 Analyst pass · score 52 · Bags · via pump.fun\nBuy flow next.", "Strat A · Bags passed the analyst (score 52)"),
    ("info", "Phone app data is 55 min old. Retrying.", "Desk · Phone app data is 55 min old. Retrying."),
]


@pytest.mark.parametrize("kind,text,title", CASES)
def test_headlines(kind, text, title):
    assert nl.headline(kind, text)[1] == title
    out = nl.telegram(kind, text)
    assert out.split("\n")[0] == title
    assert nl.telegram(kind, out) == out                     # idempotent
    for line in text.split("\n")[1:]:
        assert line in out                                   # details kept below the headline


@pytest.mark.parametrize("kind,text,title", [c for c in CASES if c[0] != "info"])
def test_every_strategy_message_makes_a_phone_push_with_the_headline(kind, text, title):
    c, _ = make_ctx()
    note = webpush.labelled(kind, text, M, c)
    assert note is not None and note["title"] == title[:70] and note["cat"] in ("trades", "stratb", "approvals", "alerts", "pipeline")


def test_adopted_buy_push_and_approval_keeps_buttons():
    c, _ = make_ctx()
    n = webpush.labelled("gmgn", CASES[4][1], M, c)
    assert n["cat"] == "trades" and n["tag"] == f"trade-{M}" and "managed by Strategy A" in n["body"]
    ap = webpush.labelled("gmgn_approve", "GMGN · APPROVE? Sloth ($1.2M MC)\nMarket cap $1,200,000 is over your $500,000 limit. "
                                         'Reply "approve 7" or "skip 7".', M, c)
    assert ap["title"] == "Strat A · approve Sloth?" and ap.get("aid") == 7 and len(ap["actions"]) == 2


def test_desk_label_and_prelabelled_test_note():
    c, _ = make_ctx()
    n = webpush.labelled("info", "Desk · test: notifications now show which strategy (Strat A / Strat B)", None, c)
    assert n["title"].startswith("Desk · test")
    assert webpush.labelled("info", "plain info nobody needs on the phone", None, c) is None


def test_telegram_push_sends_labelled_text_and_mirrors(monkeypatch):
    c, _ = make_ctx()
    seen = {}
    c.webpush = types.SimpleNamespace(mirror=lambda k, t, m: seen.update(kind=k, text=t))
    tg = Telegram(types.SimpleNamespace(ctx=c))
    sent = []

    async def fake_send(text, buttons=None, chat_id=None, mentions=None):
        sent.append(text)
    tg.send = fake_send
    run(tg.push("B LIVE · Bought GM · $18.13 @ $0.0002 · stop -35% live on GMGN", kind="strat_b", mint=M))
    assert sent[0].startswith("Strat B bought GM $18.13\n") and seen["kind"] == "strat_b"
    row = c.db.one("SELECT text FROM pushes ORDER BY id DESC LIMIT 1")
    assert row["text"].startswith("Strat B bought GM")
