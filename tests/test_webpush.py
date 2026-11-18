"""DECISION 62: live phone notifications (Web Push), Telegram kept as the backup."""
import asyncio, base64, json, time
import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
import crypto, links, stage_pings, webpush
from tests.helpers import make_ctx

M = "Push111111111111111111111111111111111pump"
TOK = "test-app-token-123456"


class Resp:
    def __init__(self, code=201, text=""):
        self.status_code, self.text = code, text


class FakeHttp:
    def __init__(self, code=201):
        self.code, self.posts = code, []

    async def post(self, url, content=None, headers=None):
        self.posts.append({"url": url, "content": content, "headers": headers})
        return Resp(self.code)


def ua():
    k = ec.generate_private_key(ec.SECP256R1())
    auth = webpush.b64u(b"0123456789abcdef")
    return k, webpush.b64u(webpush.pub_raw(k)), auth


def make_wp(code=201):
    c, clock = make_ctx()
    http = FakeHttp(code)
    wp = webpush.WebPush(c, token=TOK, key=ec.generate_private_key(ec.SECP256R1()), http=http)
    c.webpush = wp
    return c, wp, http


def sub_msg(ep="https://fcm.googleapis.com/fcm/send/abc123", cats=None, ts=None, p256dh=None, auth=None, **kw):
    k, p, a = ua()
    m = {"type": "sub", "sub": {"endpoint": ep, "keys": {"p256dh": p256dh or p, "auth": auth or a}}, "cats": cats or {},
         "device": "Android Chrome", "ts": (ts or time.time()) * 1000, **kw}
    return json.dumps(crypto.encrypt_obj(m, TOK)), k, a


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------- crypto
def test_aes128gcm_roundtrip_and_header():
    k, p256dh, auth = ua()
    body = webpush.encrypt(b'{"title":"Sold Gorilla +$22.97 (+45%)"}', p256dh, auth)
    assert body[16:20] == (4096).to_bytes(4, "big") and body[20] == 65
    assert webpush.decrypt(body, k, auth) == b'{"title":"Sold Gorilla +$22.97 (+45%)"}'


def test_vapid_jwt_is_valid_es256_for_the_endpoint_origin():
    key = ec.generate_private_key(ec.SECP256R1())
    h = webpush.vapid_header(key, "https://fcm.googleapis.com/fcm/send/xyz", now=1_800_000_000)
    t = h.split("t=")[1].split(",")[0]
    head, claims, sig = t.split(".")
    c = json.loads(webpush.unb64u(claims))
    assert c["aud"] == "https://fcm.googleapis.com" and c["exp"] == 1_800_000_000 + 12 * 3600 and c["sub"].startswith("mailto:")
    raw = webpush.unb64u(sig)
    der = encode_dss_signature(int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big"))
    key.public_key().verify(der, f"{head}.{claims}".encode(), ec.ECDSA(hashes.SHA256()))
    assert h.endswith("k=" + webpush.b64u(webpush.pub_raw(key)))


def test_topic_is_a_hash_never_the_token():
    t = webpush.topic(TOK)
    assert t.startswith("gbdesk-") and TOK not in t and len(t) == 47 and t == webpush.topic(TOK)


# ---------------------------------------------------------------- text -> notification
class FakeDB:
    def __init__(self, row=None, meta=None):
        self.row, self.m = row, meta or {}

    def one(self, q, a=()):
        return self.row

    def meta(self, k, d=None):
        return self.m.get(k, d)


class FakeCtx:
    def __init__(self, row=None, meta=None):
        self.db = FakeDB(row, meta)


def test_live_sell_short_title_with_pnl_and_hold_time():
    n = webpush.render("gmgn", "GMGN · The Nietzschean Gorilla sold (trailing take-profit (desk)) · +$22.97 (+45.3%)\n"
                               "On-chain: 0.7055 SOL in → 1.0251 SOL out, fees included", M, FakeCtx({"ts": 100.0, "closed_at": 108.0}))
    assert n["cat"] == "trades" and n["title"] == "Sold The Nietzschean Gorilla +$22.97 (+45%)"
    assert n["body"] == "trail · held 8s" and n["tag"] == f"trade-{M}"


@pytest.mark.parametrize("reason,short", [("surge profit floor", "floor"), ("surge top", "surge top"), ("hard stop", "stop"),
                                          ("server-side stop", "stop"), ("trailing take-profit", "trail"), ("server-side exit", "GMGN exit")])
def test_exit_reasons_are_short(reason, short):
    n = webpush.render("gmgn", f"GMGN · Share Bags sold ({reason}) · -$4.21 (-20.7%)", M)
    assert n["title"] == "Sold Share Bags -$4.21 (-21%)" and n["body"].startswith(short)


def test_final_result_reentry_and_small_pct():
    n = webpush.render("gmgn", "GMGN · RE-ENTRY · Paid final result (sold) · +$0.39 (+1.9%)", M)
    assert n["title"] == "Sold Paid +$0.39 (+1.9%)" and "re-entry" in n["body"]


def test_live_buy_and_selling_trigger_share_the_trade_tag():
    b = webpush.render("gmgn", "GMGN · Bought Rockstar Games · $50.00 · exits live on GMGN · stop -50% for first 5 min, then -20%", M)
    assert b["title"] == "Bought Rockstar Games $50.00" and b["body"] == "GMGN LIVE · stop -50%" and b["cat"] == "trades"
    s = webpush.render("gmgn", "GMGN · Share Bags: surged +27% then fell to the +5% profit floor (+4.9% vs entry), market-selling", M)
    assert s["title"] == "Selling Share Bags" and s["tag"] == b["tag"]


@pytest.mark.parametrize("text", ["GMGN · Buying Paid · $20.00 (0.2798 SOL) · 2/4 open",
                                  "GMGN · Paid: 5 min grace over · stop -20% live on GMGN",
                                  "GMGN · Jensen Huang sold, confirming result"])
def test_noise_stays_on_telegram_only(text):
    assert webpush.render("gmgn", text, M) is None
    assert webpush.render("ticket", "PAPER · tickets bumped to $47.50 = 23.75% of stack", None) is None


def test_gmgn_paper_sell_is_labelled_paper():
    n = webpush.render("gmgn", "PAPER · GMGN · Sold Debu · surge top · +$5.00 (+10.0%)", M)
    assert n["title"].startswith("PAPER · Sold Debu +$5.00") and n["cat"] == "trades"


def test_approval_has_buttons_fomo_tap_and_telegram_bot():
    url = links.coin_url(M)
    text = (f"GMGN · APPROVE? Big Cat (BCAT)\nMarket cap $312,000 is over your $300,000 auto-buy limit, so I didn't buy it.\n"
            f"Price $0.0003 · liquidity $40,000 · score 55\n{url}\nApprove buys it ... Expires in 10 min. "
            f'No buttons? Reply "approve 17" or "skip 17".')
    n = webpush.render("gmgn_approve", text, M, FakeCtx(meta={"tg_username": "Denarpobot"}))
    assert n["cat"] == "approvals" and n["title"] == "Approve Big Cat? MC $312K" and "$300K" in n["body"]
    assert n["aid"] == 17 and n["tag"] == "approve-17" and n["requireInteraction"] and n["bot"] == "Denarpobot"
    assert [a["action"] for a in n["actions"]] == ["approve", "skip"]
    assert n["url"] == url and "r=savipww" in n["url"]
    e = webpush.render("gmgn_approve", "GMGN · Big Cat: approval expired after 10 min. Nothing was bought.", M)
    assert e["title"] == "Big Cat: approval expired"


def test_alerts():
    n = webpush.render("gmgn_alert", "GMGN · ALERT nema: couldn't attach a server-side stop, so I market-sold it — and the sell FAILED.", M)
    assert n["cat"] == "alerts" and n["title"] == "⚠️ nema" and "FAILED" in n["body"]
    assert webpush.render("alert", "layer 8 down, every coin is red until rpc_url changes", None)["cat"] == "alerts"
    assert webpush.render("gmgn_refused", "GMGN · Skipped X: free 0.0007 SOL can't cover even $1.00", M)["cat"] == "alerts"
    assert webpush.render("info", "Phone app data is 28 min old (live ts x). The desk is still publishing", None)["cat"] == "alerts"


def test_strategy_b_paper_labels():
    b = webpush.render("strat_b", "STRATEGY B · PAPER · Bought Debu · $50.00 @ $0.000123\nmc $80,000 · dip -22% then bounce\nstop -35%", M)
    assert b["cat"] == "stratb" and b["title"] == "B PAPER · Bought Debu $50" and "dip" in b["body"]
    s = webpush.render("strat_b", "STRATEGY B · PAPER · Sold Debu (take profit) · +$50.00 (+100.0%) · held 12m\nB paper bankroll $1,050.00", M)
    assert s["title"] == "B PAPER · Sold Debu +$50.00 (+100%)" and s["body"] == "take profit · held 12m"


def test_pipeline_one_tag_per_coin_through_the_stages():
    sc = webpush.render("stage_scanner", "✅ Scanner pass · Share Bags · liq $12.8K · MC $39.3K · via pump.fun · recheck 2", M)
    au = webpush.render("stage_auditor", "⚡ Surge +39% in 1m · 🛡️ Audit pass · Share Bags · top10 24.5% · via pump.fun", M)
    an = webpush.render("stage_analyst", "🎯 Analyst pass · score 52 · Share Bags · via pump.fun\nBuy flow next.", M)
    rj = webpush.render("stage_analyst", "Analyst · score 41 (min 50) · Share Bags\nNo buy: alive_6h 0.53, organic 0.54.\nOverride…", M)
    assert {x["tag"] for x in (sc, au, an, rj)} == {f"pipe-{M}"} and {x["cat"] for x in (sc, au, an, rj)} == {"pipeline"}
    assert sc["title"] == "Scanner passed Share Bags" and sc["body"].startswith("liq $12.8K · MC $39.3K")
    assert au["title"] == "Auditor passed Share Bags" and "⚡ +39% in 1m" in au["body"]
    assert an["title"] == "Share Bags · Analyst score 52: buy" and an["renotify"] is True
    assert rj["title"] == "Share Bags · Analyst score 41: skip" and "alive_6h 0.53" in rj["body"] and rj["renotify"] is False
    assert sc["renotify"] is False and au["renotify"] is False and sc["url"] == "#/alive"


# ---------------------------------------------------------------- relay + sending
def test_subscribe_via_relay_then_send_respects_categories():
    c, wp, http = make_wp()
    raw, k, auth = sub_msg(cats={"pipeline": False})

    async def go():
        assert wp.handle(raw) == "subscribed"
        await asyncio.gather(*wp.tasks)                         # the "Notifications on ✓" confirmation
        assert len(http.posts) == 1
        assert json.loads(webpush.decrypt(http.posts[0]["content"], k, auth))["title"] == "Notifications on ✓"
        wp.mirror("stage_scanner", "✅ Scanner pass · X · liq $1K · MC $2K", M)    # pipeline off for this phone
        wp.mirror("gmgn", "GMGN · X sold (surge top) · +$1.00 (+2.0%)", M)
        await asyncio.gather(*wp.tasks)
        return [json.loads(webpush.decrypt(p["content"], k, auth)) for p in http.posts]
    got = run(go())
    assert [g["title"] for g in got] == ["Notifications on ✓", "Strat A sold X +$1.00 (+2.0%) (surge top)"]   # DECISION 73 headline
    h = http.posts[-1]["headers"]
    assert h["Content-Encoding"] == "aes128gcm" and h["Authorization"].startswith("vapid t=") and h["Urgency"] == "high"
    assert json.loads(c.db.one("SELECT cats FROM push_subs")["cats"])["pipeline"] is False
    st = wp.status()
    assert st["vapid"] and len(st["subs"]) == 1 and st["subs"][0]["device"] == "Android Chrome"


def test_relay_rejects_wrong_token_stale_and_foreign_hosts():
    c, wp, http = make_wp()
    k, p, a = ua()
    other = json.dumps(crypto.encrypt_obj({"type": "sub", "ts": time.time() * 1000}, "someone-else-token"))
    assert wp.handle(other) is None and wp.handle("not json") is None
    raw, *_ = sub_msg(ts=time.time() - 3600)
    assert wp.handle(raw) == "stale"
    raw, *_ = sub_msg(ep="https://evil.example.com/collect")
    assert wp.handle(raw) == "bad sub"
    raw, *_ = sub_msg(ep="http://fcm.googleapis.com/fcm/send/x")
    assert wp.handle(raw) == "bad sub"
    raw, *_ = sub_msg(auth="AAAA")
    assert wp.handle(raw) == "bad sub"
    assert c.db.one("SELECT COUNT(*) n FROM push_subs")["n"] == 0


def test_update_prefs_test_and_unsubscribe():
    c, wp, http = make_wp()
    ep = "https://fcm.googleapis.com/fcm/send/abc123"

    async def go():
        raw, k, a = sub_msg(ep=ep)
        assert wp.handle(raw) == "subscribed"
        raw2, *_ = sub_msg(ep=ep, cats={"stratb": False})
        assert wp.handle(raw2) == "updated"
        assert json.loads(c.db.one("SELECT cats FROM push_subs")["cats"])["stratb"] is False
        t = json.dumps(crypto.encrypt_obj({"type": "test", "endpoint": ep, "ts": time.time() * 1000}, TOK))
        assert wp.handle(t) == "test sent"
        u = json.dumps(crypto.encrypt_obj({"type": "unsub", "endpoint": ep, "ts": time.time() * 1000}, TOK))
        assert wp.handle(u) == "unsubscribed"
        await asyncio.gather(*wp.tasks)
    run(go())
    assert c.db.one("SELECT COUNT(*) n FROM push_subs")["n"] == 0


def test_gone_subscription_is_removed():
    c, wp, http = make_wp(code=410)

    async def go():
        raw, *_ = sub_msg()
        wp.handle(raw)
        await asyncio.gather(*wp.tasks)
    run(go())
    assert c.db.one("SELECT COUNT(*) n FROM push_subs")["n"] == 0


def test_no_subscription_means_no_send():
    c, wp, http = make_wp()

    async def go():
        assert wp.mirror("gmgn", "GMGN · X sold (surge top) · +$1.00 (+2.0%)", M) is None
    run(go())
    assert http.posts == []


def test_stage_pings_off_on_telegram_still_reach_the_phone():
    c, wp, http = make_wp()
    c.s.set("stage pings", "off")
    seen = []
    wp.mirror = lambda kind, text, mint=None: seen.append((kind, text)) or None

    async def go():
        stage_pings.ping(c, "scanner", M, "✅ Scanner pass · X · liq $1K · MC $2K")
    run(go())
    assert seen and seen[0][0] == "stage_scanner" and c.notifier.sent == []


def test_telegram_push_mirrors_to_the_phone(monkeypatch):
    import telegram

    class D:
        pass
    c, wp, http = make_wp()
    d = D(); d.ctx = c
    monkeypatch.setattr(telegram.secrets_store, "get", lambda k, default=None: "1" if k == "OWNER_CHAT_ID" else "x")
    tg = telegram.Telegram(d)
    seen = []
    wp.mirror = lambda kind, text, mint=None: seen.append(kind)

    async def fake_send(*a, **k):
        return None
    tg.send = fake_send
    run(tg.push("GMGN · X sold (surge top) · +$1.00 (+2.0%)", kind="gmgn", mint=M))
    assert seen == ["gmgn"]


def test_web_has_no_secret_and_sw_handles_push_and_actions():
    import pathlib
    web = pathlib.Path(__file__).resolve().parents[1] / "web"
    sw = (web / "sw.js").read_text()
    assert 'addEventListener("push"' in sw and "notificationclick" in sw and "gmap" in sw and "gmsk" in sw
    app = (web / "app.js").read_text()
    assert "Turn on notifications" in app and "pushManager.subscribe" in app and "ntfy.sh" in app
    assert "VAPID_PRIVATE_KEY" not in app + sw
