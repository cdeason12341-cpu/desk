"""DECISION 95 (owner 2026-11-19 5:46 PM CT, "make sure the app gives a different notification like copied buy"):
copy trades get their own pushes: '🔁 Copied buy · A' / '🔁 Copied sell · A' / '🔁 Copy stop · A', a quiet
'🔁 Copy skipped · A' at most once per coin, their own 'copy' push category and copy-<mint> tag."""
import types
import copytrade, notif_label, webpush
from publisher import _copy_title
from telegram import Telegram
from tests.helpers import make_ctx, run, pair
from tests.test_copy_trade import live, tx, L, MT, M2, swaps


def record(t):
    got = []

    async def push(text, kind="gmgn", mint=None):
        got.append((kind, text, mint))
    t.push = push
    return got


def test_render_copy_kinds_own_category_and_tag():
    b = webpush.labelled("gmgn_copy_buy", "🔁 Copied buy · A\nCopied 9s4ji: bought $20 of FIONA (his buy #1 in this coin) · delay 4s", MT)
    assert b["cat"] == "copy" and b["title"] == "🔁 Copied buy · A" and b["tag"] == f"copy-{MT}" and b["renotify"] is True
    assert b["body"].startswith("Copied 9s4ji: bought $20 of FIONA (his buy #1 in this coin)")
    s = webpush.labelled("gmgn_copy_sell", "🔁 Copied sell · A\nCopied 9s4ji: sold 50% of FIONA · est. P&L +$1.00", MT)
    assert s["cat"] == "copy" and s["title"] == "🔁 Copied sell · A" and s["tag"] == f"copy-{MT}"
    k = webpush.labelled("gmgn_copy_skip", "🔁 Copy skipped · A\n9s4ji bought FIONA · not copied: insufficient balance", MT)
    assert k["cat"] == "copy" and k["silent"] is True and k["renotify"] is False and k["tag"] == f"copyskip-{MT}"
    p = webpush.labelled("gmgn_copy_pending", "🔁 Copying buy · A\nCopying 9s4ji: buying $20 of FIONA", MT)
    assert p["renotify"] is False and p["tag"] == f"copy-{MT}"
    n = webpush.labelled("gmgn", "GMGN · Bought FIONA · $20.00 · 1/4 open · exits live on GMGN · stop -50%", MT)
    assert n["cat"] == "trades" and n["tag"] == f"trade-{MT}"          # normal A buy stays separate
    assert webpush.CATS["copy"] is True
    assert notif_label.telegram("gmgn_copy_buy", "🔁 Copied buy · A\nCopied 9s4ji: bought $20 of X").startswith("🔁 Copied buy · A\nCopied 9s4ji")


def test_live_copy_pushes_buy_sell_close(monkeypatch):
    c, clock, t, w, ch, bal = live(monkeypatch)
    got = record(t)
    clock.t += 5
    ch.add(tx("b1", 100 * L, 99 * L, 0, 10), clock.t - 3)
    run(w.poll_once())
    kinds = [k for k, *_ in got]
    assert kinds == ["gmgn_copy_pending", "gmgn_copy_buy"]
    assert got[1][1] == "🔁 Copied buy · A\nCopied 9s4ji: bought $20 of FIONA (his buy #1 in this coin) · delay 3s"
    clock.t += 5
    ch.add(tx("s1", 10 * L, 12 * L, 1000, 500), clock.t)
    run(w.poll_once())
    assert got[-1][0] == "gmgn_copy_sell" and got[-1][1].startswith("🔁 Copied sell · A\nCopied 9s4ji: sold 50% of FIONA")
    clock.t += 5
    ch.add(tx("s2", 12 * L, 14 * L, 500, 0), clock.t)
    run(w.poll_once())
    assert got[-1][0] == "gmgn_copy_pending" and "selling 100% of FIONA (he sold all)" in got[-1][1]
    clock.t += 60
    bal["tok"][MT] = 0
    o = c.db.one("SELECT * FROM gmgn_orders WHERE a_kind='copy'")
    run(w.live_tick(o, 0.01, 1000, clock.t))
    assert got[-1][0] == "gmgn_copy_sell" and got[-1][1].startswith("🔁 Copied sell · A\nCopied 9s4ji: sold 100% of FIONA · P&L")
    assert not any(k == "gmgn" for k, *_ in got)                       # never a normal A buy / sell alert


def test_copy_skipped_quiet_once_per_coin(monkeypatch):
    c, clock, t, w, ch, bal = live(monkeypatch, wallet_sol=0.1, **{"copy.every_n": 1})
    got = record(t)
    for i in range(3):
        clock.t += 5
        ch.add(tx(f"z{i}", 100 * L, 99 * L, i, i + 10), clock.t)
        run(w.poll_once())
    skips = [x for x in got if x[0] == "gmgn_copy_skip"]
    assert len(skips) == 1 and skips[0][1].startswith("🔁 Copy skipped · A\n9s4ji bought FIONA · not copied: insufficient balance")
    assert c.db.one("SELECT COUNT(*) n FROM gmgn_orders WHERE status='refused'")["n"] == 3   # every refusal still logged
    clock.t += 5
    ch.add(tx("z9", 100 * L, 99 * L, 0, 10, mint=M2), clock.t)
    run(w.poll_once())
    assert len([x for x in got if x[0] == "gmgn_copy_skip"]) == 2      # a new coin gets its own note


def test_paper_copy_close_push_and_feed_titles(monkeypatch):
    c, clock, t, w, ch, bal = live(monkeypatch)
    c.s.set("gmgn.mode", "PAPER", source="test", force=True)
    got = record(t)
    clock.t += 5
    ch.add(tx("b1", 100 * L, 99 * L, 0, 10), clock.t)
    run(w.poll_once())
    assert got[-1][0] == "gmgn_copy_buy" and got[-1][1].endswith("simulated fill") and "· paper" in got[-1][1]
    clock.t += 5
    ch.add(tx("s1", 10 * L, 12 * L, 10, 0), clock.t)
    run(w.poll_once())
    assert got[-1][0] == "gmgn_copy_sell" and got[-1][1].startswith("🔁 Copied sell · A\nCopied 9s4ji: sold 100% of FIONA · P&L")
    o = c.db.one("SELECT * FROM gmgn_orders WHERE a_kind='copy'")
    assert _copy_title(o, "buy", "x") == "🔁 Copied buy · Fiona" and _copy_title(o, "sell", "x") == "🔁 Copied sell · Fiona"
    assert _copy_title({**o, "a_kind": None}, "buy", "Bought Fiona") == "Bought Fiona"


def test_telegram_skip_is_silent():
    c, _ = make_ctx()
    c.webpush = types.SimpleNamespace(mirror=lambda *a: None)
    tg = Telegram(types.SimpleNamespace(ctx=c))
    sent = []

    async def fake_send(text, buttons=None, chat_id=None, mentions=None, **kw):
        sent.append((text, kw))
    tg.send = fake_send
    run(tg.push("🔁 Copy skipped · A\n9s4ji bought X · not copied: caps", kind="gmgn_copy_skip", mint=MT))
    run(tg.push("🔁 Copied buy · A\nCopied 9s4ji: bought $20 of X", kind="gmgn_copy_buy", mint=MT))
    assert sent[0][1] == {"silent": True} and sent[1][1] == {}
    assert sent[1][0].startswith("🔁 Copied buy · A")


def test_copy_test_command_owner_only():
    c, _ = make_ctx()
    seen = []
    c.webpush = types.SimpleNamespace(mirror=lambda k, t, m: seen.append(webpush.labelled(k, t, m)))
    tg = Telegram(types.SimpleNamespace(ctx=c))
    sent = []

    async def fake_send(text, buttons=None, chat_id=None, mentions=None, **kw):
        sent.append(text)
    tg.send = fake_send
    tg.from_owner = False
    assert run(tg.command("copy test"))[0].startswith("Refused")
    tg.from_owner = True
    r = run(tg.command("copy test"))
    assert "TEST" in r[0] and seen[0]["title"] == "🔁 Copied buy · TEST" and seen[0]["cat"] == "copy"
    assert sent[0].startswith("🔁 Copied buy · TEST\nTEST")
