"""Live phone notifications through Web Push (owner 2026-11-16 8:17 PM CT, DECISION 62). Telegram stays as the backup.

Desk -> phone: standard Web Push (RFC 8030/8291/8292): VAPID ES256 JWT + aes128gcm payload encryption, POST to the
browser's push service (FCM for Android Chrome). Every Telegram push is mirrored here by `mirror(kind, text, mint)`,
which turns the long Telegram text into a short title/body, picks a category and drops the noise.

Phone -> desk (Plan B has no endpoint, DECISION 5): the app posts an AES-GCM envelope (same APP_TOKEN key as the data,
crypto.py) to a private ntfy.sh topic derived from APP_TOKEN; the desk streams that topic. Only a holder of APP_TOKEN can
make a message the desk accepts; push endpoints must be on a known push service. The relay never carries trades:
Approve/Skip notification buttons open the owner's Telegram deep link (t.me/<bot>?start=gmap_<id>), owner-chat locked.
"""
from __future__ import annotations
import asyncio, base64, hashlib, hmac, json, os, re, struct, time
from urllib.parse import urlparse
import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
import crypto, links, secrets_store
from ctx import iso

NTFY = "https://ntfy.sh"
TOPIC_SALT = "grok-bot-desk/v1/push:"
CATS = {"trades": True, "approvals": True, "alerts": True, "pipeline": True, "stratb": True, "paper": False}
APP_URL = "https://cdeason12341-cpu.github.io/desk/"
VAPID_SUB = "mailto:desk-push@users.noreply.github.com"
PUSH_HOSTS = (".googleapis.com", ".mozilla.com", ".notify.windows.com", ".push.apple.com", ".mozaws.net")
MSG_MAX_AGE_S = 15 * 60
SCHEMA = """CREATE TABLE IF NOT EXISTS push_subs(id INTEGER PRIMARY KEY, endpoint TEXT UNIQUE, p256dh TEXT, auth TEXT, cats TEXT,
  device TEXT, created REAL, updated REAL, last_ok REAL, last_err TEXT, fails INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS webpushes(id INTEGER PRIMARY KEY, ts REAL, cat TEXT, tag TEXT, title TEXT, body TEXT, sent INTEGER, ok INTEGER, kind TEXT);"""


def b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def unb64u(s: str) -> bytes:
    s = str(s).strip().replace("+", "-").replace("/", "_").rstrip("=")
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def topic(token: str) -> str:
    """ntfy topic the app posts to. A hash of APP_TOKEN, never the token itself."""
    return "gbdesk-" + hashlib.sha256((TOPIC_SALT + token).encode()).hexdigest()[:40]


# ---------------------------------------------------------------- RFC 8291 / 8292
def load_vapid(pem: str | None = None):
    pem = pem or secrets_store.get("VAPID_PRIVATE_KEY")
    if not pem:
        return None
    return serialization.load_pem_private_key(pem.encode() if isinstance(pem, str) else pem, None)


def pub_raw(key) -> bytes:
    return key.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)


def vapid_header(key, endpoint: str, now: float | None = None) -> str:
    u = urlparse(endpoint)
    head = b64u(json.dumps({"typ": "JWT", "alg": "ES256"}, separators=(",", ":")).encode())
    claims = b64u(json.dumps({"aud": f"{u.scheme}://{u.netloc}", "exp": int((now or time.time()) + 12 * 3600),
                              "sub": VAPID_SUB}, separators=(",", ":")).encode())
    r, s = decode_dss_signature(key.sign(f"{head}.{claims}".encode(), ec.ECDSA(hashes.SHA256())))
    jwt = f"{head}.{claims}.{b64u(r.to_bytes(32, 'big') + s.to_bytes(32, 'big'))}"
    return f"vapid t={jwt}, k={b64u(pub_raw(key))}"


def _hmac(k, m):
    return hmac.new(k, m, hashlib.sha256).digest()


def encrypt(payload: bytes, p256dh: str, auth: str, *, salt: bytes | None = None, as_key=None) -> bytes:
    """aes128gcm content coding (RFC 8188) with the Web Push key schedule (RFC 8291), one record."""
    ua_pub = unb64u(p256dh)
    auth_secret = unb64u(auth)
    as_key = as_key or ec.generate_private_key(ec.SECP256R1())
    as_pub = pub_raw(as_key)
    shared = as_key.exchange(ec.ECDH(), ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), ua_pub))
    salt = salt or os.urandom(16)
    ikm = _hmac(_hmac(auth_secret, shared), b"WebPush: info\x00" + ua_pub + as_pub + b"\x01")
    prk = _hmac(salt, ikm)
    cek = _hmac(prk, b"Content-Encoding: aes128gcm\x00\x01")[:16]
    nonce = _hmac(prk, b"Content-Encoding: nonce\x00\x01")[:12]
    ct = AESGCM(cek).encrypt(nonce, payload + b"\x02", None)
    return salt + struct.pack("!IB", 4096, len(as_pub)) + as_pub + ct


def decrypt(body: bytes, ua_key, auth: str) -> bytes:
    """User-agent side of encrypt() (used by the tests)."""
    salt, rs, idlen = body[:16], *struct.unpack("!IB", body[16:21])
    as_pub, ct = body[21:21 + idlen], body[21 + idlen:]
    ua_pub = pub_raw(ua_key)
    shared = ua_key.exchange(ec.ECDH(), ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), as_pub))
    ikm = _hmac(_hmac(unb64u(auth), shared), b"WebPush: info\x00" + ua_pub + as_pub + b"\x01")
    prk = _hmac(salt, ikm)
    cek = _hmac(prk, b"Content-Encoding: aes128gcm\x00\x01")[:16]
    nonce = _hmac(prk, b"Content-Encoding: nonce\x00\x01")[:12]
    pt = AESGCM(cek).decrypt(nonce, ct, None)
    return pt.rstrip(b"\x00")[:-1]


def endpoint_ok(ep: str) -> bool:
    try:
        u = urlparse(ep)
    except Exception:
        return False
    return u.scheme == "https" and bool(u.hostname) and any(u.hostname.endswith(h) for h in PUSH_HOSTS)


# ---------------------------------------------------------------- text -> notification
SHORT = {"trailing take-profit (desk)": "trail", "trailing take-profit": "trail", "server-side trailing stop": "trail",
         "trailing stop": "trailing stop", "surge profit floor": "floor", "surge top": "surge top", "hard stop": "stop",
         "hard stop (desk)": "stop", "server-side stop": "stop", "server-side exit": "GMGN exit", "timeout": "timeout",
         "liquidity pulled": "liquidity pulled", "sold": "sold", "take profit": "take-profit"}
MONEY = r"([+-])\$([\d,]+(?:\.\d+)?) \(([+-]?[\d.]+)%\)"


def held_txt(secs) -> str:
    if secs is None:
        return ""
    secs = max(0, float(secs))
    return f"{secs:.0f}s" if secs < 90 else f"{secs / 60:.0f}m" if secs < 5400 else f"{secs / 3600:.1f}h"


def money_title(verb, name, sign, amt, pct) -> str:
    p = float(pct)
    return f"{verb} {name} {sign}${amt} ({p:+.0f}%)" if abs(p) >= 10 else f"{verb} {name} {sign}${amt} ({p:+.1f}%)"


def _first(text):
    return text.split("\n", 1)[0].strip()


def _clip(s, n=110):
    s = re.sub(r"\s+", " ", s or "").strip()
    return s if len(s) <= n else s[:n - 1].rstrip() + "…"


def _strip_surge(t):
    m = re.match(r"^⚡ Surge \+?([\d.]+)% in (\w+) · (.*)$", t)
    return (m.group(3), f"⚡ +{m.group(1)}% in {m.group(2)}") if m else (t, "")


def render(kind: str, text: str, mint: str | None = None, ctx=None) -> dict | None:
    """Telegram (kind, text) -> {cat, title, body, tag, url, actions?, renotify, ttl}. None = not worth a phone buzz."""
    kind = kind or "info"
    text = (text or "").strip()
    first = _first(text)
    route = {"pipeline": "#/live", "stratb": "#/stratb", "paper": "#/paper"}
    n = lambda cat, title, body="", tag=None, url=None, **kw: dict(cat=cat, title=_clip(title, 70), body=_clip(body, 160),
                                                                      tag=tag, url=url or route.get(cat, "#/home"),
                                                                      renotify=kw.pop("renotify", True), **kw)
    # ---- pipeline (one notification per coin, updated in place)
    if kind.startswith("stage_"):
        stage = kind[6:]
        t, surge = _strip_surge(first)
        tag = f"pipe-{mint}" if mint else None
        via = ""
        mv = re.search(r" · via ([^·]+?)(?: · recheck \d+)?$", t)
        if mv:
            via = mv.group(1).strip()
            t = re.sub(r" · via [^·]+?(?= · |$)", "", t)
        extra = " · ".join(x for x in (surge, f"via {via}" if via else "") if x)
        if stage == "scanner":
            m = re.match(r"^✅ Scanner pass · (.+?) · (liq .+)$", t)
            if m:
                return n("pipeline", f"Scanner passed {m.group(1)}", " · ".join(x for x in (m.group(2), extra) if x), tag, renotify=False)
        if stage == "auditor":
            m = re.match(r"^🛡️ Audit pass · (.+?) · (top10 .+)$", t)
            if m:
                return n("pipeline", f"Auditor passed {m.group(1)}", " · ".join(x for x in ("Scanner ✓ Auditor ✓", m.group(2), extra) if x),
                         tag, renotify=False)
        if stage == "analyst":
            m = re.match(r"^🎯 Analyst pass · score (\d+) · (.+)$", t)
            if m:
                return n("pipeline", f"{m.group(2)} · Analyst score {m.group(1)}: buy", " · ".join(x for x in ("Scanner ✓ Auditor ✓ Analyst ✓ · buy flow next", extra) if x), tag)
            m = re.match(r"^Analyst · score (\d+) \(min (\d+)\) · (.+)$", t)
            if m:
                why = re.search(r"No buy: (.+?)\.?$", text, re.M)
                return n("pipeline", f"{m.group(3)} · Analyst score {m.group(1)}: skip",
                         f"min {m.group(2)}" + (f" · {why.group(1)}" if why else ""), tag, renotify=False)
        if stage == "dip":
            m = re.match(r"^⏳ Dip watch · (.+)$", t)
            if m:
                need = re.search(r"Need (-[\d.]+%) from (\$\S+) · buy anyway in (\d+)s", text)
                body = (f"need {need.group(1)} from {need.group(2)} · buy anyway in {need.group(3)}s" if need else "waiting for a dip")
                return n("pipeline", f"{m.group(1)} · waiting for a dip", " · ".join(x for x in (body, extra) if x), tag)
            m = re.match(r"^🎯 Dip buy · (.+)$", t)
            if m:
                gap = bool(re.search(r"^Gapped ", text, re.M))
                down = re.search(r"(?:Down|Gapped) ([\d.]+%)", text)
                body = (("gapped " if gap else "down ") + (down.group(1) if down else "") + " from the high · buying")
                return n("pipeline", f"{m.group(1)} · buying the dip", " · ".join(x for x in (body, extra) if x), tag)
            m = re.match(r"^🎯 Dip timeout · (.+)$", t)
            if m:
                return n("pipeline", f"{m.group(1)} · buying (no dip)", " · ".join(x for x in ("waited, no dip · buying at market", extra) if x), tag)
            m = re.match(r"^Dip skipped · (.+)$", t)
            if m:
                why = (text.split("\n")[1] if "\n" in text else "") or extra
                return n("pipeline", f"{m.group(1)} · dip skipped", why, tag)
        if stage.startswith("reentry"):
            t2 = t.replace("🔁 RE-ENTRY watch · ", "").replace("🔁 RE-ENTRY · ", "")
            m = re.match(r"^(.+?) bounced (\+[\d.]+%)", t2)
            if m:
                return n("pipeline", f"Re-entry watch {m.group(1)}", f"bounced {m.group(2)} off post-sell low · auditing", tag, renotify=False)
            m = re.match(r"^🛡️ Audit pass · (.+?) · (top10 .+)$", t2)
            if m:
                return n("pipeline", f"Re-entry · Auditor passed {m.group(1)}", m.group(2), tag, renotify=False)
            m = re.match(r"^🎯 Analyst pass · score (\d+) · (.+)$", t2)
            if m:
                return n("pipeline", f"Re-entry {m.group(2)} · Analyst score {m.group(1)}: buy", "buy flow next", tag)
        return n("pipeline", t, extra, tag, renotify=False)
    # ---- approvals
    if kind == "gmgn_approve":
        m = re.match(r"^GMGN · APPROVE\? (.+?)(?: \(([^)]*)\))?( · re-entry)?$", first)
        aid = re.search(r'"approve (\d+)"', text)
        if m and aid:
            mc = re.search(r"Market cap (\$[\d,]+|unknown) is over your \$([\d,]+)", text)
            mc_txt = mc.group(1) if mc else "?"
            return n("approvals", f"Approve {m.group(1)}? MC {_k(mc_txt)}",
                     f"Over your {_k('$' + mc.group(2)) if mc else ''} limit · expires in 10 min · tap Approve or Skip",
                     f"approve-{aid.group(1)}", links.coin_url(mint) if mint else "#/home", aid=int(aid.group(1)),
                     requireInteraction=True, bot=_bot(ctx),
                     actions=[{"action": "approve", "title": "✅ Approve"}, {"action": "skip", "title": "Skip"}])
        m = re.match(r"^GMGN · (.+?): approval expired", first)
        if m:
            return n("approvals", f"{m.group(1)}: approval expired", "Nothing was bought", tag=None, renotify=False)
        return n("approvals", first.replace("GMGN · ", ""), "")
    # ---- strategy B LIVE (real money, DECISION 66)
    if kind == "strat_b" and first.startswith("B LIVE · "):
        t = first[len("B LIVE · "):]
        m = re.match(r"^Buying (.+?) · \$([\d,.]+)", t)
        if m:
            return n("stratb", f"B LIVE · Buying {m.group(1)} ${float(m.group(2).replace(',', '')):,.2f}",
                     _clip(text.split("\n")[1] if "\n" in text else "", 120), f"b-{mint}")
        m = re.match(rf"^Sold (.+?) \((.+?)\) · {MONEY}(?: · held (\S+))?", t)
        if m:
            return n("stratb", "B LIVE · " + money_title("Sold", m.group(1), m.group(3), m.group(4), m.group(5)),
                     " · ".join(x for x in (m.group(2), f"held {m.group(6)}" if m.group(6) else "") if x), f"b-{mint}")
        return n("stratb", "B LIVE · " + _clip(t, 90), _clip(text.split("\n")[1] if "\n" in text else "", 120))
    # ---- strategy B (paper)
    if kind == "strat_b":
        t = first.replace("STRATEGY B · PAPER · ", "")
        m = re.match(r"^Bought (.+?) · \$([\d,.]+) @", t)
        if m:
            return n("stratb", f"B PAPER · Bought {m.group(1)} ${float(m.group(2).replace(',', '')):,.0f}",
                     _clip(text.split("\n")[1] if "\n" in text else "", 120), f"b-{mint}")
        m = re.match(rf"^Sold (.+?) \((.+?)\) · {MONEY}(?: · held (\S+))?", t)
        if m:
            return n("stratb", "B PAPER · " + money_title("Sold", m.group(1), m.group(3), m.group(4), m.group(5)),
                     " · ".join(x for x in (m.group(2), f"held {m.group(6)}" if m.group(6) else "") if x), f"b-{mint}")
        return n("stratb", "B PAPER · " + t, "")
    # ---- FOMO paper test (off by default)
    if kind in ("buy", "sell"):
        t = first.replace("PAPER · ", "")
        m = re.match(r"^(?:(?:OVERRIDE|BYPASS) · (?:score \d+ · )?)?Buy (.+?) · \$([\d,.]+)", t)
        if m:
            return n("paper", f"PAPER · Bought {m.group(1)} ${m.group(2)}", "FOMO paper test", f"paper-{mint}")
        m = re.match(rf"^Sold (.+?) · {MONEY}", t)
        if m:
            return n("paper", "PAPER · " + money_title("Sold", m.group(1), m.group(2), m.group(3), m.group(4)), "FOMO paper test", f"paper-{mint}")
        return n("paper", "PAPER · " + _clip(t, 60), "FOMO paper test", f"paper-{mint}")
    # ---- alerts
    if kind in ("gmgn_alert", "alert", "risk", "gmgn_refused") or (kind == "info" and "Phone app data" in first) \
            or re.search(r"\bALERT\b|FAILED|\bhalt", first):
        t = first.replace("PAPER · ", "").replace("GMGN · ", "").replace("ALERT ", "")
        head, sep, rest = t.partition(": ")
        if not sep:
            head, rest = t, " ".join(text.split("\n")[1:2])
        return n("alerts", f"⚠️ {head}", rest, f"alert-{mint}" if mint else None)
    # ---- GMGN trades
    if kind == "gmgn":
        paper = first.startswith("PAPER · ")
        t = first.replace("PAPER · ", "").replace("GMGN · ", "")
        re_entry = t.startswith("RE-ENTRY · ")
        t = t.replace("RE-ENTRY · ", "")
        label = "PAPER" if paper else "LIVE"
        tag = f"trade-{mint}" if mint else None
        if paper:                                        # GMGN paper fills: "Sold X · reason · +$1 (+2%)"
            m = re.match(rf"^Sold (.+?) · (.+?) · {MONEY}", t)
            if m:
                return n("trades", "PAPER · " + money_title("Sold", m.group(1), m.group(3), m.group(4), m.group(5)),
                         f"{SHORT.get(m.group(2), m.group(2))} · GMGN paper", tag)
        m = re.match(r"^Bought (.+?) · \$([\d,.]+)", t)
        if m:
            stop = re.search(r"stop (-\d+%)", t)
            return n("trades", f"{'PAPER · ' if paper else ''}Bought {m.group(1)} ${m.group(2)}",
                     " · ".join(x for x in (f"GMGN {label}", "re-entry" if re_entry else "", f"stop {stop.group(1)}" if stop else "") if x), tag)
        m = re.match(rf"^(.+?) (?:sold|final result) \((.+?)\) · {MONEY}", t)
        if m:
            name, why = m.group(1), m.group(2)
            held = None
            if ctx is not None and mint:
                try:
                    o = ctx.db.one("SELECT ts, closed_at FROM gmgn_orders WHERE mint=? AND closed_at IS NOT NULL ORDER BY closed_at DESC LIMIT 1", (mint,))
                    if o and o["ts"] and o["closed_at"]:
                        held = o["closed_at"] - o["ts"]
                except Exception:
                    held = None
            body = " · ".join(x for x in (SHORT.get(why, why), f"held {held_txt(held)}" if held is not None else "",
                                          "re-entry" if re_entry else "", "PAPER" if paper else "") if x)
            return n("trades", ("PAPER · " if paper else "") + money_title("Sold", name, m.group(3), m.group(4), m.group(5)), body, tag)
        m = re.match(r"^(.+?): (.+?),? market-selling", t)
        if m:
            return n("trades", f"Selling {m.group(1)}", m.group(2), tag)
        return None                                      # "Buying…", "grace over", "confirming result": Telegram only
    return None


def _bot(ctx) -> str:
    try:
        b = ctx.db.meta("tg_username") if ctx is not None else None
    except Exception:
        b = None
    return b if b and re.fullmatch(r"[A-Za-z0-9_]{5,32}", b) else "Denarpobot"


def _k(v: str) -> str:
    """'$312,000' -> '$312K'."""
    try:
        x = float(v.replace("$", "").replace(",", ""))
    except (ValueError, AttributeError):
        return v
    return f"${x / 1e6:.2f}M" if x >= 1e6 else f"${x / 1e3:.0f}K" if x >= 1e3 else f"${x:,.0f}"


# ---------------------------------------------------------------- the sender + inbox
class WebPush:
    def __init__(self, ctx, token: str | None = None, key=None, http=None):
        self.c = ctx
        self.token = token if token is not None else secrets_store.get("APP_TOKEN")
        self.key = key if key is not None else load_vapid()
        self.http = http or httpx.AsyncClient(timeout=15)
        self.log = ctx.logs["telegram"]
        self.tasks: set = set()
        for stmt in SCHEMA.split(";"):
            if stmt.strip():
                ctx.db.x(stmt)
        self.sent: list = []                             # last payloads (tests)

    @property
    def public_key(self):
        return b64u(pub_raw(self.key)) if self.key else None

    def subs(self):
        return self.c.db.rows("SELECT * FROM push_subs ORDER BY id")

    def status(self) -> dict:
        """For state.json: the app checks its own endpoint hash against `subs`."""
        out = []
        for s in self.subs():
            out.append({"id": hashlib.sha256(s["endpoint"].encode()).hexdigest()[:16], "cats": json.loads(s["cats"] or "{}"),
                        "device": s["device"], "lastOk": iso(s["last_ok"]) if s["last_ok"] else None, "fails": s["fails"]})
        last = self.c.db.one("SELECT ts, title FROM webpushes WHERE ok=1 ORDER BY id DESC LIMIT 1")
        return {"vapid": self.public_key, "subs": out, "cats": CATS,
                "last": {"t": iso(last["ts"]), "title": last["title"]} if last else None}

    # ---- desk -> phone
    def mirror(self, kind, text, mint=None):
        """Called for every Telegram push. Never blocks, never raises."""
        try:
            note = render(kind, text, mint, self.c)
        except Exception as e:
            self.c.error("webpush", "render", f"{kind} {str(e)[:200]}")
            return None
        if not note:
            return None
        note["kind"] = kind
        return self.notify(note)

    def notify(self, note):
        if not self.key or not self.subs():
            return None
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return None
        t = loop.create_task(self.send(note))
        self.tasks.add(t); t.add_done_callback(self.tasks.discard)
        return t

    async def send(self, note, only=None) -> int:
        """Send to every subscription that has note['cat'] on. Returns how many push services accepted it."""
        cat = note.get("cat", "alerts")
        payload = {k: v for k, v in note.items() if v is not None and k != "kind"}
        payload["ts"] = int(time.time() * 1000)
        body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode()
        ok_n = sent = 0
        for s in self.subs():
            if only and s["endpoint"] != only:
                continue
            cats = dict(CATS, **json.loads(s["cats"] or "{}"))
            if not cats.get(cat, False) and cat != "system":
                continue
            sent += 1
            ok = await self._post(s, body, urgent=cat in ("approvals", "alerts", "trades"), topic_tag=note.get("tag"))
            ok_n += 1 if ok else 0
        self.sent.append(payload); del self.sent[:-50]
        self.c.db.x("INSERT INTO webpushes(ts,cat,tag,title,body,sent,ok,kind) VALUES(?,?,?,?,?,?,?,?)",
                    (time.time(), cat, note.get("tag"), note.get("title"), note.get("body"), sent, ok_n, note.get("kind")))
        if sent:
            self.log.info(f"{iso()}\twebpush\t{cat}\t{ok_n}/{sent}\t{note.get('title')}")
        return ok_n

    async def _post(self, s, body: bytes, urgent=False, topic_tag=None) -> bool:
        try:
            data = encrypt(body, s["p256dh"], s["auth"])
            h = {"Authorization": vapid_header(self.key, s["endpoint"]), "Content-Encoding": "aes128gcm",
                 "Content-Type": "application/octet-stream", "TTL": "3600", "Urgency": "high" if urgent else "normal"}
            if topic_tag:                                # push service keeps only the newest per topic while the phone is offline
                h["Topic"] = re.sub(r"[^A-Za-z0-9_-]", "", topic_tag)[:32]
            r = await self.http.post(s["endpoint"], content=data, headers=h)
        except Exception as e:
            self._fail(s, f"{type(e).__name__}: {str(e)[:120]}")
            return False
        if r.status_code in (200, 201, 202):
            self.c.db.x("UPDATE push_subs SET last_ok=?, fails=0, last_err=NULL WHERE id=?", (time.time(), s["id"]))
            return True
        if r.status_code in (404, 410):                  # the phone unsubscribed / reinstalled: forget it
            self.c.db.x("DELETE FROM push_subs WHERE id=?", (s["id"],))
            self.log.info(f"{iso()}\twebpush\tsub #{s['id']} gone ({r.status_code}), removed")
            return False
        self._fail(s, f"HTTP {r.status_code} {r.text[:120]}")
        return False

    def _fail(self, s, why):
        self.c.db.x("UPDATE push_subs SET fails=fails+1, last_err=? WHERE id=?", (secrets_store.redact(why), s["id"]))
        self.c.error("webpush", "send", f"sub #{s['id']} {secrets_store.redact(why)[:200]}")

    # ---- phone -> desk (ntfy relay)
    def handle(self, raw: str, now: float | None = None):
        """One relay message (an encrypted envelope). Returns a short result string, or None if ignored."""
        now = now or time.time()
        try:
            env = json.loads(raw)
            msg = crypto.decrypt_obj(env, self.token)
        except Exception:
            return None                                  # not ours / not encrypted with APP_TOKEN: ignore silently
        try:
            ts = float(msg.get("ts", 0)) / 1000
        except (TypeError, ValueError):
            return None
        if abs(now - ts) > MSG_MAX_AGE_S:
            return "stale"
        typ = msg.get("type")
        if typ == "sub":
            sub = msg.get("sub") or {}
            ep, keys = sub.get("endpoint", ""), sub.get("keys") or {}
            if not endpoint_ok(ep) or not keys.get("p256dh") or not keys.get("auth"):
                return "bad sub"
            try:
                if len(unb64u(keys["p256dh"])) != 65 or len(unb64u(keys["auth"])) != 16:
                    return "bad sub"
            except Exception:
                return "bad sub"
            cats = {k: bool(v) for k, v in (msg.get("cats") or {}).items() if k in CATS}
            old = self.c.db.one("SELECT id FROM push_subs WHERE endpoint=?", (ep,))
            self.c.db.x("INSERT INTO push_subs(endpoint,p256dh,auth,cats,device,created,updated) VALUES(?,?,?,?,?,?,?) "
                        "ON CONFLICT(endpoint) DO UPDATE SET p256dh=excluded.p256dh, auth=excluded.auth, cats=excluded.cats, "
                        "device=excluded.device, updated=excluded.updated",
                        (ep, keys["p256dh"], keys["auth"], json.dumps(dict(CATS, **cats)), str(msg.get("device") or "")[:60], now, now))
            self.log.info(f"{iso()}\twebpush\t{'prefs updated' if old else 'new phone subscribed'}\t{json.dumps(dict(CATS, **cats))}")
            self.c.event("webpush", None)
            if not old or msg.get("test"):
                self.notify({"cat": "system", "title": "Notifications on ✓", "tag": "system",
                             "body": "Trades, approvals and alerts will show here. Telegram keeps working too.", "url": "#/account"})
            return "updated" if old else "subscribed"
        if typ == "unsub":
            self.c.db.x("DELETE FROM push_subs WHERE endpoint=?", (str(msg.get("endpoint") or ""),))
            self.log.info(f"{iso()}\twebpush\tphone unsubscribed")
            self.c.event("webpush", None)
            return "unsubscribed"
        if typ == "test":
            ep = str(msg.get("endpoint") or "")
            if self.c.db.one("SELECT id FROM push_subs WHERE endpoint=?", (ep,)):
                t = self.notify({"cat": "system", "title": "Sold Test Coin +$22.97 (+45%)", "body": "test · this is what a sell looks like",
                                 "tag": "test", "url": "#/account"})
                return "test sent" if t else "test skipped"
            return "unknown endpoint"
        return None

    async def loop(self):
        """Stream the private ntfy topic. Reconnects with backoff; remembers the last message id across restarts."""
        if not self.token:
            await asyncio.sleep(3600); return
        url = f"{NTFY}/{topic(self.token)}/json"
        back = 2.0
        while True:
            since = self.c.db.meta("webpush_ntfy_since") or "12h"
            try:
                async with self.http.stream("GET", url, params={"since": since}, timeout=httpx.Timeout(10, read=90)) as r:
                    if r.status_code != 200:
                        raise RuntimeError(f"ntfy HTTP {r.status_code}")
                    back = 2.0
                    async for line in r.aiter_lines():
                        if not line.strip():
                            continue
                        try:
                            ev = json.loads(line)
                        except ValueError:
                            continue
                        if ev.get("event") != "message":
                            continue
                        res = self.handle(ev.get("message") or "")
                        self.c.db.set_meta("webpush_ntfy_since", ev.get("id"))
                        if res:
                            self.log.info(f"{iso()}\twebpush relay\t{res}")
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.log.info(f"{iso()}\twebpush relay\treconnect in {back:.0f}s\t{secrets_store.redact(str(e))[:160]}")
            await asyncio.sleep(back)
            back = min(back * 2, 60.0)
