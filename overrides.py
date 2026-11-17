"""DECISION 36 · Owner override of the analyst score for ONE coin, from the app's Live feed via a Telegram deep link.

ovp_<mint> (paper): one FOMO PAPER fill at a fresh price, labelled 'PAPER · OVERRIDE · score N', reason 'owner override'.
                    Never GMGN.
ovr_<mint> (real):  a confirm message first. Only on Confirm (within 5 min), freshness is re-checked (liquidity >= scanner
                    floor, price not down > 20% since the reject, reject <= 30 min old) and the coin goes to the GMGN trader
                    as a normal buy. Only the analyst score is bypassed: every GMGN cap, the reserve, the wallet check and
                    the -15/20 condition orders apply unchanged. GMGN must be LIVE. Once per mint.
Owner lock: Telegram only lets chat OWNER_CHAT_ID reach these; every attempt is recorded in the `overrides` table."""
from __future__ import annotations
import json, re
import fields
from ctx import iso
from agents.gmgn_trader import exits_label

REJECT_MAX_AGE_S = 30 * 60
CONFIRM_TTL_S = 5 * 60
MAX_DROP_PCT = 20.0
START_RE = re.compile(r"^(ovp|ovr)_([1-9A-HJ-NP-Za-km-z]{32,44})$")
USED = ("done", "confirmed", "refused")        # an override that reached its decision is spent for that mint


class Overrides:
    def __init__(self, desk):
        self.d, self.c = desk, desk.ctx
        self.log = self.c.logs["override"]

    # ---- helpers
    def _rec(self, mint, kind, status, **kw):
        cols = dict(ts=self.c.clock(), mint=mint, kind=kind, status=status, night=self.c.night, **kw)
        rid = self.c.db.x(f"INSERT INTO overrides({','.join(cols)}) VALUES({','.join('?' * len(cols))})", tuple(cols.values())).lastrowid
        self.log.info(f"{iso()}\t{mint}\t{kind}\t{status}\t{kw.get('result') or ''}")
        return rid

    def _upd(self, rid, **kw):
        self.c.db.x(f"UPDATE overrides SET {', '.join(k + '=?' for k in kw)} WHERE id=?", (*kw.values(), rid))
        self.log.info(f"{iso()}\t#{rid}\t" + "\t".join(f"{k}={v}" for k, v in kw.items()))

    def used(self, mint, kind=None) -> bool:
        q = f"SELECT id FROM overrides WHERE mint=? AND status IN ({','.join('?' * len(USED))})"
        a = [mint, *USED]
        if kind:
            q += " AND kind=?"; a.append(kind)
        return bool(self.c.db.one(q, tuple(a)))

    def used_mints(self) -> set:
        return {r["mint"] for r in self.c.db.rows(f"SELECT DISTINCT mint FROM overrides WHERE status IN ({','.join('?' * len(USED))})", USED)}

    def on_paper(self, mint) -> bool:
        """True when the analyst paper bypass already bought this coin tonight (or a PAPER position is open on it)."""
        c = self.c
        return bool(c.db.one("SELECT id FROM signals WHERE mint=? AND night=? AND reason LIKE 'analyst bypass%'", (mint, c.night))
                    or c.db.one("SELECT id FROM positions WHERE mint=? AND mode='PAPER' AND (night=? OR status IN ('open','exiting'))", (mint, c.night)))

    def reject(self, mint):
        """Latest analyst decision for the mint if it was a reject; else None."""
        r = self.c.db.one("SELECT * FROM analyses WHERE mint=? ORDER BY ts DESC, id DESC LIMIT 1", (mint,))
        if not r or r["passed"]:
            return None
        try:
            body = json.loads(r["json"] or "{}")
        except Exception:
            body = {}
        price = body.get("priceUsd") if isinstance(body, dict) else None
        if not price:                                   # older rows: nearest desk price sample at the decision
            p = self.c.db.one("SELECT price FROM prices WHERE mint=? AND ts<=? ORDER BY ts DESC LIMIT 1", (mint, r["ts"] + 60))
            price = p["price"] if p else None
        return {"ts": r["ts"], "score": r["score"], "price": float(price) if price else None,
                "failed": json.loads(r["failed"] or "[]") if r["failed"] else []}

    def _check(self, mint, kind):
        """-> (reject, None) or (None, reason)."""
        rj = self.reject(mint)
        if rj is None:
            return None, "the Analyst didn't reject that coin (overrides are only for Analyst rejects; safety checks stay)"
        age = self.c.clock() - rj["ts"]
        if age > REJECT_MAX_AGE_S:
            return None, f"the reject is {int(age // 60)} min old (limit 30 min)"
        if self.used(mint, kind):
            return None, "already overridden once"
        return rj, None

    async def _fresh(self, mint):
        from agents.scanner import pick_pair
        try:
            pair = pick_pair(await self.c.net.dex_tokens([mint]), mint)
        except Exception:
            pair = None
        if not pair or not fields.get(pair, "priceUsd"):
            return None
        g = lambda k: fields.get(pair, k)
        return {"price": float(g("priceUsd")), "liq": float(g("liquidity.usd") or 0), "mc": float(g("marketCap") or 0),
                "name": g("baseToken.name") or self.c.label(mint), "symbol": g("baseToken.symbol")}

    # ---- entry points (called only from Telegram after the owner check)
    async def start(self, param: str):
        m = START_RE.match(param or "")
        if not m:
            return "That override link isn't valid.", None
        kind, mint = ("paper" if m.group(1) == "ovp" else "real"), m.group(2)
        return await (self.paper(mint) if kind == "paper" else self.ask_real(mint))

    async def paper(self, mint):
        c = self.c
        rj, why = self._check(mint, "paper")
        if why:
            self._rec(mint, "paper", "blocked", result=why)
            return f"Override (paper) refused: {why}.", None
        if self.on_paper(mint):
            self._rec(mint, "paper", "blocked", score=rj["score"], result="already on paper (analyst bypass)")
            return ("Override (paper) not needed: this coin is already on paper (the analyst bypass bought it). "
                    "Use Override · Real $ if you want real money on it."), None
        if c.mode() != "PAPER":
            self._rec(mint, "paper", "blocked", score=rj["score"], result="desk not PAPER")
            return "Override (paper) refused: the FOMO desk is LIVE, paper overrides only run in PAPER.", None
        co = c.coin(mint) or {}
        if not co.get("name"):
            fr = await self._fresh(mint)
            if fr:
                co = {"name": fr["name"], "symbol": fr["symbol"]}
        item = {"scan": {"mint": mint, "name": co.get("name") or c.label(mint), "symbol": co.get("symbol")},
                "analyst": {"score": rj["score"], "passed": False, "failed": rj["failed"]}, "override": True}
        out = await self.d.executor.run(item)                # FOMO PAPER flow at a fresh price; executor never calls GMGN for it
        if out and out.get("status") == "bought":
            self._rec(mint, "paper", "done", score=rj["score"], reject_ts=rj["ts"], reject_price=rj["price"],
                      price=out.get("signalPrice"), size_usd=out.get("sizeUsd"), result="paper fill")
            return None, None                                # the executor already pushed 'PAPER · OVERRIDE · score N · Buy …'
        why = (out or {}).get("reason") or "already signalled or open tonight"
        self._rec(mint, "paper", "refused", score=rj["score"], reject_ts=rj["ts"], result=why)
        return f"Override (paper) for {co.get('name') or c.label(mint)} not filled: {why}.", None

    def _gmgn_ready(self):
        g = getattr(self.d, "gmgn", None)
        if g is None or getattr(g, "client", None) is None or not self.c.s["gmgn.enabled"]:
            return "GMGN hands-off isn't connected or is off"
        if g.halted():
            return "GMGN is halted (/unhalt first)"
        if g.mode() != "LIVE":
            return "GMGN is in PAPER mode, so a real-money override can't buy. Switch with 'gmgn live' first"
        return None

    async def ask_real(self, mint):
        c, s = self.c, self.c.s
        rj, why = self._check(mint, "real")
        if why:
            self._rec(mint, "real", "blocked", result=why)
            return f"Override (real $) refused: {why}.", None
        g_why = self._gmgn_ready()
        if g_why:
            self._rec(mint, "real", "blocked", score=rj["score"], result=g_why)
            return f"Override (real $) refused: {g_why}.", None
        fr = await self._fresh(mint)
        if fr is None:
            self._rec(mint, "real", "blocked", score=rj["score"], result="no fresh price")
            return "Override (real $) refused: no fresh price for that coin right now.", None
        size, sz_why = self.d.gmgn.ticket_usd(fr["liq"])
        c.db.x("UPDATE overrides SET status='superseded' WHERE mint=? AND kind='real' AND status='asked'", (mint,))
        self._rec(mint, "real", "asked", score=rj["score"], reject_ts=rj["ts"], reject_price=rj["price"], price=fr["price"],
                  liq=fr["liq"], mc=fr["mc"], size_usd=size, expires=c.clock() + CONFIRM_TTL_S)
        since = f"{(fr['price'] / rj['price'] - 1) * 100:+.1f}% since the reject" if rj["price"] else "reject price unknown"
        text = (f"REAL MONEY · OVERRIDE · score {rj['score']}\n"
                f"{fr['name']}{' (' + fr['symbol'] + ')' if fr['symbol'] else ''}\n"
                f"Price ${fr['price']:.8g} ({since})\n"
                f"Liquidity ${fr['liq']:,.0f} · MC ${fr['mc']:,.0f}\n"
                f"Size {'$' + format(size, ',.2f') if size else 'n/a (' + str(sz_why) + ')'}{' (less if free SOL is short: $' + format(float(s['gmgn.fallback_trade_usd']), ',.2f') + ', then what it covers down to $' + format(float(s['gmgn.min_trade_usd']), ',.2f') + ')' if size and size > float(s['gmgn.min_trade_usd']) else ''} from the GMGN main wallet ({self.d.gmgn.open_label('LIVE') if hasattr(self.d.gmgn, 'open_label') else ''}, "
                f"{int(s['gmgn.max_tickets_night'])} tickets/night, {s['gmgn.sol_reserve']:g} SOL reserve)\n"
                f"Exits on GMGN: {exits_label(s)}\n"
                f"Only the Analyst score is skipped. Confirm within 5 min.")
        return text, [[{"text": "Confirm real buy", "callback_data": f"ovrc:{mint}"}, {"text": "Cancel", "callback_data": f"ovrx:{mint}"}]]

    def _asked(self, mint):
        return self.c.db.one("SELECT * FROM overrides WHERE mint=? AND kind='real' AND status='asked' ORDER BY id DESC LIMIT 1", (mint,))

    async def cancel_real(self, mint):
        a = self._asked(mint)
        if not a:
            return "Nothing to cancel for that coin."
        self._upd(a["id"], status="cancelled", result="cancelled by you")
        return "Override cancelled. Nothing was bought."

    async def confirm_real(self, mint):
        c, s = self.c, self.c.s
        if self.used(mint, "real"):
            return "Override (real $) refused: already used once for this coin."
        a = self._asked(mint)
        if not a:
            return "Override (real $) refused: no pending confirm for that coin. Tap the app button again."
        if c.clock() > (a["expires"] or 0):
            self._upd(a["id"], status="expired", result="confirm expired (5 min)")
            return "Override (real $) refused: the confirm expired (5 min). Nothing was bought."
        # spend the override BEFORE anything can buy: a second Confirm can never reach GMGN
        self._upd(a["id"], status="confirmed")
        rid = a["id"]

        def refuse(why):
            self._upd(rid, status="refused", result=why)
            return f"Override (real $) refused: {why}. Nothing was bought."

        age = c.clock() - (a["reject_ts"] or 0)
        if age > REJECT_MAX_AGE_S:
            return refuse(f"the reject is now {int(age // 60)} min old (limit 30 min)")
        g_why = self._gmgn_ready()
        if g_why:
            return refuse(g_why)
        fr = await self._fresh(mint)
        if fr is None:
            return refuse("no fresh price")
        floor = float(s["scanner.min_liquidity_usd"])
        if fr["liq"] < floor:
            return refuse(f"liquidity ${fr['liq']:,.0f} is under the scanner floor ${floor:,.0f}")
        if not a["reject_price"]:
            return refuse("no price recorded at the reject, can't check the drop")
        drop = (1 - fr["price"] / a["reject_price"]) * 100
        if drop > MAX_DROP_PCT:
            return refuse(f"price is down {drop:.0f}% since the reject (limit {MAX_DROP_PCT:g}%)")
        g = self.d.gmgn
        before = (c.db.one("SELECT COALESCE(MAX(id),0) m FROM gmgn_orders") or {}).get("m") or 0
        res = await g.on_pass(mint, fr["name"], fr["symbol"], fr["price"], fr["liq"], a["score"])   # normal GMGN buy: all caps apply
        for o in c.db.rows("SELECT id, reason FROM gmgn_orders WHERE id>? AND mint=?", (before, mint)):
            c.db.x("UPDATE gmgn_orders SET reason=? WHERE id=?", (f"owner override: {o['reason']}" if o["reason"] else "owner override", o["id"]))
        st = (res or {}).get("status")
        if res is None:
            return refuse("GMGN skipped it (already bought or open tonight, or GMGN off)")
        if st == "refused":
            return refuse(res.get("reason") or "GMGN refused")
        self._upd(rid, status="done", price=fr["price"], liq=fr["liq"], result=f"gmgn {st}")
        o = c.db.one("SELECT * FROM gmgn_orders WHERE id>? AND mint=? ORDER BY id DESC LIMIT 1", (before, mint)) or {}
        size = f" · ${o.get('size_usd') or 0:,.2f}" if o else ""
        head = f"GMGN · OVERRIDE · score {a['score']} · {fr['name']}"
        if st == "open" and o.get("strategy_order_id"):         # truthful state (DECISION 39)
            return f"{head}: bought{size}\nExits live on GMGN: {exits_label(s, (o.get('exits_phase') or 'normal'))}"
        if st == "exiting":
            return f"{head}: bought{size}, then market-sold because no server-side exit could be attached. See the GMGN alerts."
        if st in ("open", "pending", "confirmed", "successful"):
            return f"{head}: {st}{size}. Exits not confirmed yet; I'll alert you if they can't be attached."
        return f"GMGN · OVERRIDE · score {a['score']} · {fr['name']}: {st}. Check your GMGN wallet."
