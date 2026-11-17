"""Re-entry after a LIVE GMGN exit (owner 2026-11-15 8:52 PM CT, bounce 7% at 8:53 PM CT; DECISION 46).

When a LIVE GMGN order closes (any exit: stop, trail, take-profit, desk sell), its mint is watched for
gmgn.reentry_watch_min from the sell. Every gmgn.reentry_poll_s the DexScreener price/liquidity is read (one batched
tokens/v1 call for all watches) and the post-sell low is tracked.

Trigger: price >= post-sell low * (1 + gmgn.reentry_bounce_pct/100) AND m5 buys > m5 sells.
Drop:    liquidity down more than gmgn.reentry_max_liq_drop_pct since the sell, or under scanner.min_liquidity_usd.
On a trigger the coin goes through the full Auditor, then the Analyst (current gates), then the normal GMGN buy
(adaptive size, max open, same server-side exits), at most gmgn.reentry_max_per_coin re-entries per coin per night.
One trigger per watch: a rejected re-entry ends the watch (no repeated audits/LLM calls)."""
from __future__ import annotations
import asyncio, json, time
import fields, stage_pings
from ctx import iso

SCHEMA = """CREATE TABLE IF NOT EXISTS reentry_watch(id INTEGER PRIMARY KEY, night TEXT, mint TEXT, order_id INTEGER UNIQUE,
  name TEXT, symbol TEXT, exit_reason TEXT, sold_at REAL, expires REAL, sell_price REAL, sell_liq REAL, low REAL, low_ts REAL,
  last_price REAL, last_liq REAL, buys_m5 INTEGER, sells_m5 INTEGER, last_check REAL, status TEXT, note TEXT, trigger_json TEXT);"""

LIVE_STATES = ("watching", "triggered")


def bounce_pct(low, price):
    return None if not low or not price else 100.0 * (price / low - 1)


def evaluate(s, w: dict, price, liq, buys, sells, now) -> tuple[str, str | None]:
    """('watch'|'trigger'|'drop'|'expire', note). Pure; w carries low/sell_liq."""
    if now >= w["expires"]:
        return "expire", "watch window over"
    if liq is not None:
        floor = float(s["scanner.min_liquidity_usd"])
        if liq < floor:
            return "drop", f"liquidity ${liq:,.0f} under the scanner floor ${floor:,.0f}"
        if w.get("sell_liq") and liq < w["sell_liq"] * (1 - s["gmgn.reentry_max_liq_drop_pct"] / 100.0):
            return "drop", f"liquidity down {100 * (1 - liq / w['sell_liq']):.0f}% since the sell"
    if price is None or not w.get("low"):
        return "watch", None
    b = bounce_pct(w["low"], price)
    if b >= s["gmgn.reentry_bounce_pct"] and buys is not None and sells is not None and buys > sells:
        return "trigger", f"bounced +{b:.1f}% off post-sell low, m5 buys {buys} > sells {sells}"
    return "watch", None


class ReEntry:
    def __init__(self, desk):
        self.d = desk
        self.c = desk.ctx
        self.c.db.c.executescript(SCHEMA)
        self.log = self.c.logs.get("gmgn") or self.c.logs["executor"]
        self._tasks: set = set()

    @property
    def s(self):
        return self.c.s

    # ---- arm
    def arm(self, o: dict, sold_at: float | None = None) -> int | None:
        """Start watching a closed LIVE order's mint. Idempotent per order. None when off / limit already used."""
        s, c = self.s, self.c
        if not s["gmgn.reentry_enabled"] or o.get("mode") != "LIVE":
            return None
        used = c.db.one("SELECT COUNT(*) n FROM gmgn_orders WHERE mint=? AND night=? AND reason='re-entry' AND status NOT IN ('refused')",
                        (o["mint"], c.night))["n"]
        if used >= int(s["gmgn.reentry_max_per_coin"]):
            return None
        sold = float(sold_at or o.get("closed_at") or c.clock())
        cur = c.db.x("INSERT OR IGNORE INTO reentry_watch(night,mint,order_id,name,symbol,exit_reason,sold_at,expires,sell_price,sell_liq,"
                     "low,low_ts,status) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     (c.night, o["mint"], o["id"], o.get("name"), o.get("symbol"), o.get("exit_reason"), sold,
                      sold + float(s["gmgn.reentry_watch_min"]) * 60, o.get("last_price"), o.get("last_liq"),
                      o.get("last_price"), sold if o.get("last_price") else None, "watching"))
        if cur.rowcount:
            self.log.info(f"{iso()}\tLIVE\t{o['mint']}\treentry watch\torder={o['id']}\tuntil={iso(sold + float(s['gmgn.reentry_watch_min']) * 60)}")
            c.event("scan", o["mint"])
            return cur.lastrowid
        return None

    def watches(self, states=LIVE_STATES):
        q = ",".join("?" * len(states))
        return self.c.db.rows(f"SELECT * FROM reentry_watch WHERE status IN ({q}) ORDER BY sold_at", tuple(states))

    def _set(self, wid, **kw):
        self.c.db.x(f"UPDATE reentry_watch SET {', '.join(k + '=?' for k in kw)} WHERE id=?", (*kw.values(), wid))

    # ---- poll
    async def poll_once(self) -> int:
        rows = self.watches(("watching",))
        if not rows:
            return 0
        if not self.s["gmgn.reentry_enabled"]:
            for w in rows:
                self._set(w["id"], status="off", note="re-entry turned off")
            return 0
        from agents.scanner import pick_pair
        pairs = await self.c.net.dex_tokens(sorted({w["mint"] for w in rows}))
        now = self.c.clock()
        for w in rows:
            if self.c.db.one("SELECT id FROM gmgn_orders WHERE mint=? AND status IN ('submitting','pending','open','exiting')", (w["mint"],)):
                self._set(w["id"], status="dropped", note="already holding it", last_check=now)
                continue
            p = pick_pair(pairs, w["mint"])
            price = float(fields.get(p, "priceUsd")) if p and fields.get(p, "priceUsd") else None
            liq = fields.get(p, "liquidity.usd") if p else None
            liq = float(liq) if liq is not None else None
            buys = fields.get(p, "txns.m5.buys") if p else None
            sells = fields.get(p, "txns.m5.sells") if p else None
            upd = {"last_price": price, "last_liq": liq, "buys_m5": buys, "sells_m5": sells, "last_check": now}
            if price is not None and (not w["low"] or price < w["low"]):
                upd.update(low=price, low_ts=now)
                w["low"] = price
            if w["sell_liq"] is None and liq is not None:
                upd["sell_liq"] = liq
                w["sell_liq"] = liq
            verdict, note = evaluate(self.s, w, price, liq, buys, sells, now)
            if verdict == "watch":
                self._set(w["id"], **upd)
                continue
            if verdict in ("drop", "expire"):
                self._set(w["id"], **upd, status="dropped" if verdict == "drop" else "expired", note=note)
                self.log.info(f"{iso()}\tLIVE\t{w['mint']}\treentry {verdict}\t{note}")
                continue
            info = {"low": w["low"], "price": price, "bouncePct": round(bounce_pct(w["low"], price), 1), "buysM5": buys,
                    "sellsM5": sells, "liq": liq, "soldAt": iso(w["sold_at"]), "exitReason": w["exit_reason"]}
            self._set(w["id"], **upd, status="triggered", note=note, trigger_json=json.dumps(info))
            self.log.info(f"{iso()}\tLIVE\t{w['mint']}\treentry trigger\t{note}")
            t = asyncio.ensure_future(self.run_trigger(dict(w, **upd), p, info))
            self._tasks.add(t); t.add_done_callback(self._tasks.discard)
        self.c.event("scan", None)
        return len(rows)

    # ---- trigger: Auditor -> Analyst -> GMGN, all normal gates
    def scan_item(self, w, p, info):
        coin = self.c.coin(w["mint"]) or {}
        from agents.scanner import market_cap
        return {"agent": "scanner", "mint": w["mint"], "name": fields.get(p, "baseToken.name") or w["name"] or coin.get("name"),
                "symbol": fields.get(p, "baseToken.symbol") or w["symbol"] or coin.get("symbol"),
                "pool": fields.get(p, "pairAddress") or coin.get("pool"), "liquidityUsd": info["liq"],
                "marketCapUsd": market_cap(p), "top10Pct": coin.get("top10"), "h1ChangePct": fields.get(p, "priceChange.h1"),
                "ageMin": int((self.c.clock() - (coin.get("migrated_at") or self.c.clock())) / 60), "verdict": "pass",
                "reason": None, "ts": iso(), "reentry": info}

    async def run_trigger(self, w, p, info):
        c, d = self.c, self.d
        mint = w["mint"]
        scan = self.scan_item(w, p, info)
        name = scan["name"] or c.label(mint)
        stage_pings.ping(c, "reentry_trigger", mint,
                         f"🔁 RE-ENTRY watch · {name} bounced +{info['bouncePct']:.0f}% off post-sell low "
                         f"(${info['low']:.8g} → ${info['price']:.8g}), m5 buys {info['buysM5']} > sells {info['sellsM5']} · auditing",
                         stage_pings.coin_button(mint))
        try:
            audit = await d.auditor.run(mint, scan, reentry=True)
            if not audit or not audit.get("passed"):
                return self._end(w, "rejected", f"auditor: {(audit or {}).get('reason') or 'no result'}")
            res = await d.analyst.run(scan, audit, reentry=True)
            if not res or not res.get("passed"):
                return self._end(w, "rejected", f"analyst: score {(res or {}).get('score')} {((res or {}).get('failed') or [''])[0]}")
            price = res.get("_priceUsd") or info["price"]
            liq = info["liq"]
            r = await d.gmgn.on_pass(mint, name, scan["symbol"], price, liq, res.get("score"), reentry=info)
            st = (r or {}).get("status")
            if st in ("open", "pending", "paper"):
                return self._end(w, "bought", f"GMGN {st}")
            return self._end(w, "rejected", f"GMGN {st or 'no buy'}: {(r or {}).get('reason') or ''}".strip())
        except Exception as e:
            c.error("gmgn", "reentry", f"{mint} {str(e)[:300]}")
            return self._end(w, "error", str(e)[:200])

    def _end(self, w, status, note):
        prev = (self.c.db.one("SELECT note FROM reentry_watch WHERE id=?", (w["id"],)) or {}).get("note")
        note = f"{prev} → {note}" if prev else note
        self._set(w["id"], status=status, note=note)
        self.log.info(f"{iso()}\tLIVE\t{w['mint']}\treentry {status}\t{note}")
        self.c.event("scan", w["mint"])
        return status

    async def loop(self):
        while True:
            t0 = time.time()
            n = 0
            try:
                n = await self.poll_once()
            except Exception as e:
                self.c.error("gmgn", "reentry_poll", str(e)[:300])
            self.c.hb("reentry", f"watching={n}")
            await asyncio.sleep(max(1, float(self.s["gmgn.reentry_poll_s"]) - (time.time() - t0)))

    # ---- feed (scanner.json)
    def feed(self):
        now = self.c.clock()
        out = []
        for w in self.c.db.rows("SELECT * FROM reentry_watch WHERE night=? ORDER BY sold_at DESC LIMIT 20", (self.c.night,)):
            b = bounce_pct(w["low"], w["last_price"])
            out.append({"mint": w["mint"], "name": w["name"], "symbol": w["symbol"], "status": w["status"], "note": w["note"],
                        "exitReason": w["exit_reason"], "soldAt": iso(w["sold_at"]), "until": iso(w["expires"]),
                        "minLeft": max(0, int((w["expires"] - now) / 60)) if w["status"] == "watching" else 0,
                        "sellPrice": w["sell_price"], "low": w["low"], "price": w["last_price"],
                        "bouncePct": None if b is None else round(b, 1), "needPct": self.s["gmgn.reentry_bounce_pct"],
                        "buysM5": w["buys_m5"], "sellsM5": w["sells_m5"], "liq": w["last_liq"], "sellLiq": w["sell_liq"]})
        return out
