"""Strategy A dip-watch (DECISION 64): after the Analyst (and the $300K approval
gate, if it applied) wait briefly for a small dip, then buy. No bounce required.
A timeout buys at market; a fresh safety problem skips. Strategy B is unchanged.
FOMO is never sent an order: the paper fill just waits for the same trigger."""
from __future__ import annotations
import asyncio, json, time
import links
from ctx import iso


DIP_GAP_LOG_PCT = 10.0          # still buy if the drop is already deeper than this; just log it


def drop_pct(high, price) -> float:
    if not high or high <= 0 or price is None:
        return 0.0
    return (1.0 - float(price) / float(high)) * 100.0


def dip_ready(s, high, price, now=None, expires=None) -> str | None:
    """'dip' | 'dip_gap' | 'timeout' | None (keep watching).

    Buy on the first tick that is >= gmgn.dip_pct below the high since approval.
    A drop already deeper than 10% is still a buy (logged as dip_gap).
    If the wait runs out first, buy at market (timeout). No bounce check.
    A dip on the same tick as the deadline wins over the timeout.
    """
    if price is not None and high:
        drop = drop_pct(high, price)
        need = float(s["gmgn.dip_pct"] or 0)
        if drop >= need:
            return "dip_gap" if drop > DIP_GAP_LOG_PCT else "dip"
    if expires is not None and now is not None and now >= expires:
        return "timeout"
    return None


def _watch(ctx, trader=None, executor=None):
    w = getattr(ctx, "dip", None)
    if w is None:
        w = DipWatch(ctx, trader=trader or getattr(ctx, "gmgn", None), executor=executor)
        ctx.dip = w
    else:
        if trader is not None:
            w.trader = trader
        elif w.trader is None:
            w.trader = getattr(ctx, "gmgn", None)
        if executor is not None:
            w.executor = executor
    return w


def watching(db, mint=None, night=None, want_gmgn=False):
    q, a = "SELECT * FROM gmgn_dips WHERE status IN ('watching','buying')", []
    if mint:
        q += " AND mint=?"; a.append(mint)
    if night:
        q += " AND night=?"; a.append(night)
    if want_gmgn:
        q += " AND want_gmgn=1"
    return db.rows(q, tuple(a))


def watching_count(db, night=None) -> int:
    if night:
        r = db.one("SELECT COUNT(*) n FROM gmgn_dips WHERE status IN ('watching','buying') AND want_gmgn=1 AND night=?", (night,))
    else:
        r = db.one("SELECT COUNT(*) n FROM gmgn_dips WHERE status IN ('watching','buying') AND want_gmgn=1")
    return int((r or {}).get("n") or 0)


class DipWatch:
    def __init__(self, ctx, trader=None, executor=None):
        self.c = ctx
        self.trader = trader
        self.executor = executor
        self.lock = asyncio.Lock()
        self._firing: set = set()

    def _log(self, *parts):
        import secrets_store
        log = (self.c.logs.get("gmgn") or self.c.logs.get("executor") or self.c.logs["desk"])
        log.info(secrets_store.redact("\t".join([iso()] + [str(p) for p in parts])))

    def _btn(self, mint):
        return [[{"text": "Open on FOMO", "url": links.coin_url(mint)}]]

    async def _push(self, text, mint, kind="stage_dip"):
        try:
            await self.c.notifier.push(text, self._btn(mint), kind=kind, mint=mint)
        except Exception as e:
            self.c.error("gmgn", "dip_push", str(e)[:200])

    # ---- start
    async def start(self, mint, name, symbol, price, liq, score=None, reentry=None,
                    want_gmgn=False, fomo_item=None, mc=None):
        c, s = self.c, self.c.s
        if not s["gmgn.dip_enabled"]:
            return None
        async with self.lock:
            return await self._start(mint, name, symbol, price, liq, score, reentry,
                                     want_gmgn, fomo_item, mc)

    async def _start(self, mint, name, symbol, price, liq, score, reentry, want_gmgn, fomo_item, mc):
        c, s = self.c, self.c.s
        now = c.clock()
        row = c.db.one("SELECT * FROM gmgn_dips WHERE mint=? AND status IN ('watching','buying')", (mint,))
        if row:
            patch = {}
            if want_gmgn and not row["want_gmgn"]:
                patch["want_gmgn"] = 1
            if fomo_item and not row["fomo_item"]:
                patch["fomo_item"] = json.dumps(fomo_item)
            if patch:
                c.db.x(f"UPDATE gmgn_dips SET {', '.join(k + '=?' for k in patch)} WHERE id=?", (*patch.values(), row["id"]))
            return {"status": "dip", "id": row["id"], "reason": "already watching"}
        # already resolved tonight: don't start a second watch (bought or skipped)
        if c.db.one("SELECT id FROM gmgn_dips WHERE mint=? AND night=? AND status IN ('bought','skipped')", (mint, c.night)):
            return None
        timeout = int(s["gmgn.dip_timeout_s"])
        expires = now + timeout
        high = float(price)
        rid = c.db.x(
            "INSERT INTO gmgn_dips(ts,night,mint,name,symbol,score,reentry,high,low,last_price,last_liq,last_mc,"
            "start_price,start_liq,status,expires,updated,want_gmgn,fomo_item) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (now, c.night, mint, name, symbol, score, json.dumps(reentry) if reentry else None,
             high, high, high, liq, mc, high, liq, "watching", expires, now, 1 if want_gmgn else 0,
             json.dumps(fomo_item) if fomo_item else None)).lastrowid
        pct = float(s["gmgn.dip_pct"])
        self._log("dip", mint, "watching", f"high={high:.10g}", f"need=-{pct:g}%", f"timeout={timeout}s",
                  f"gmgn={int(bool(want_gmgn))}", f"fomo={int(bool(fomo_item))}")
        url = links.coin_url(mint)
        text = (f"⏳ Dip watch · {name}\n"
                f"Need -{pct:g}% from ${high:.8g} · buy anyway in {timeout}s\n{url}")
        await self._push(text, mint)
        try:
            c.activity("dip", f"Watching {name} for a dip", "Strategy A", f"-{pct:g}%", f"{timeout}s", mint, "executor", "amber")
        except Exception:
            pass
        return {"status": "dip", "id": rid, "reason": "waiting for a dip"}

    # ---- loop
    async def loop(self):
        while True:
            t0 = time.time()
            try:
                n = await self.tick()
                self.c.hb("gmgn_dip", f"{'on' if self.c.s['gmgn.dip_enabled'] else 'off'} watching={n}")
            except Exception as e:
                self.c.error("gmgn", "dip", str(e)[:300])
                self.c.hb("gmgn_dip", f"error {type(e).__name__}")
                n = 0
            poll = float(self.c.s["risk.fast_poll_s"]) if (self.c.s["gmgn.dip_enabled"] and n) else 5.0
            await asyncio.sleep(max(0.3, poll - (time.time() - t0)))

    async def tick(self, prices=None, liqs=None) -> int:
        """One price check on every watching coin. `prices` is {mint: price} or {mint: (price, src)} (tests)."""
        if not self.c.s["gmgn.dip_enabled"]:
            rows = watching(self.c.db)
            if rows:
                now = self.c.clock()
                for r in rows:
                    px = r["last_price"] or r["high"]
                    await self._fire(r, "timeout", px, r["last_liq"], note="dip turned off, bought at market")
            return 0
        rows = watching(self.c.db)
        if not rows:
            return 0
        got, liq_map = await self._prices(rows, prices, liqs)
        now = self.c.clock()
        n = 0
        for r in rows:
            if r["id"] in self._firing:
                continue
            n += 1
            if r["mint"] not in got:
                sig = dip_ready(self.c.s, r["high"], None, now, r["expires"])
                if sig == "timeout":
                    px = r["last_price"] or r["high"]
                    if not px:
                        await self._skip(r, "no price")
                    else:
                        await self._fire(r, "timeout", px, r["last_liq"])
                continue
            price = got[r["mint"]]
            liq = liq_map.get(r["mint"], r["last_liq"])
            high = max(float(r["high"] or 0), float(price))
            low = min(float(r["low"] or price), float(price))
            self.c.db.x("UPDATE gmgn_dips SET high=?, low=?, last_price=?, last_liq=?, updated=? WHERE id=?",
                        (high, low, price, liq, now, r["id"]))
            r = {**r, "high": high, "low": low, "last_price": price, "last_liq": liq}
            why = self._safety(r, price, liq)
            if why:
                await self._skip(r, why)
                continue
            sig = dip_ready(self.c.s, high, price, now, r["expires"])
            if sig:
                await self._fire(r, sig, price, liq)
        return n

    async def _prices(self, rows, prices, liqs):
        got, liq_map = {}, dict(liqs or {})
        if prices is not None:
            for m, v in prices.items():
                got[m] = float(v[0] if isinstance(v, (tuple, list)) else v)
            return got, liq_map
        mints = [r["mint"] for r in rows]
        fast = getattr(self.trader, "fast", None) if self.trader is not None else None
        if fast is not None:
            try:
                px = await fast.prices(mints)
                for m, v in px.items():
                    got[m] = float(v[0] if isinstance(v, (tuple, list)) else v)
            except Exception as e:
                self.c.error("gmgn", "dip_price", str(e)[:200])
        missing = [m for m in mints if m not in got]
        if missing and self.c.net is not None:
            try:
                from agents.scanner import pick_pair
                import fields
                pairs = await self.c.net.dex_tokens(sorted(set(missing)))
                for m in missing:
                    pair = pick_pair(pairs, m)
                    v = fields.get(pair, "priceUsd") if pair else None
                    if v:
                        got[m] = float(v)
                    if pair and fields.get(pair, "liquidity.usd") is not None:
                        liq_map[m] = float(fields.get(pair, "liquidity.usd") or 0)
            except Exception as e:
                self.c.error("gmgn", "dip_dex", str(e)[:200])
        return got, liq_map

    def _safety(self, row, price, liq):
        s, c = self.c.s, self.c
        mint = row["mint"]
        if liq is not None and float(liq) <= 0:
            return "pool pulled"
        floor = float(s["scanner.min_liquidity_usd"])
        if liq is not None and float(liq) < floor:
            return f"liquidity ${float(liq):,.0f} is under the ${floor:,.0f} floor"
        start = row["start_liq"]
        if start and liq is not None and float(start) > 0:
            drop = 100.0 * (1.0 - float(liq) / float(start))
            if drop > float(s["executor.recheck_liq_drop_pct"]):
                return f"liquidity down {drop:.0f}%"
        if c.db.one("SELECT id FROM positions WHERE mint=? AND status IN ('open','exiting')", (mint,)):
            return "already open"
        if c.db.one("SELECT id FROM gmgn_orders WHERE mint=? AND status IN ('submitting','pending','open','exiting')", (mint,)):
            return "already open"
        return None

    async def _skip(self, row, why):
        if row["id"] in self._firing:
            return
        self._firing.add(row["id"])
        try:
            self.c.db.x("UPDATE gmgn_dips SET status='skipped', reason=?, result=?, updated=? WHERE id=? AND status IN ('watching','buying')",
                        (why, why, self.c.clock(), row["id"]))
            self._log("dip", row["mint"], "skipped", why)
            url = links.coin_url(row["mint"])
            await self._push(f"Dip skipped · {row['name']}\n{why}\n{url}", row["mint"])
            try:
                self.c.activity("dip", f"Skipped {row['name']} (dip)", "Strategy A", why, "", row["mint"], "executor", "red")
            except Exception:
                pass
            self.c.skip(row["mint"], "executor", f"dip skipped: {why}")
        finally:
            self._firing.discard(row["id"])

    async def _fire(self, row, reason, price, liq, note=None):
        if row["id"] in self._firing:
            return
        # spend the watch before anything can buy twice
        cur = self.c.db.one("SELECT * FROM gmgn_dips WHERE id=? AND status='watching'", (row["id"],))
        if not cur:
            return
        self._firing.add(row["id"])
        try:
            self.c.db.x("UPDATE gmgn_dips SET status='buying', reason=?, last_price=?, last_liq=?, updated=? WHERE id=?",
                        (reason, price, liq, self.c.clock(), row["id"]))
            drop = drop_pct(row["high"] or price, price)
            pct = float(self.c.s["gmgn.dip_pct"])
            timeout = int(self.c.s["gmgn.dip_timeout_s"])
            url = links.coin_url(row["mint"])
            if reason == "timeout":
                text = (f"🎯 Dip timeout · {row['name']}\n"
                        f"No {pct:g}% dip in {timeout}s · buying at market\n{url}")
                tag = "timeout-buy"
            elif reason == "dip_gap":
                text = (f"🎯 Dip buy · {row['name']}\n"
                        f"Gapped {drop:.1f}% from the high · still buying\n{url}")
                tag = "dip-gap-buy"
            else:
                text = (f"🎯 Dip buy · {row['name']}\n"
                        f"Down {drop:.1f}% from the high · buying\n{url}")
                tag = "dip-buy"
            if note:
                text = text.replace("\n" + url, f"\n{note}\n{url}")
            self._log("dip", row["mint"], tag, f"price={price:.10g}", f"high={row['high']}", f"drop={drop:.1f}%",
                      note or "")
            await self._push(text, row["mint"])
            results = []
            reentry = json.loads(row["reentry"]) if row["reentry"] else None
            if row["want_gmgn"] and self.trader is not None and getattr(self.trader, "client", None) is not None:
                try:
                    res = await self.trader.on_pass(row["mint"], row["name"], row["symbol"], price, liq or 0,
                                                    row["score"], reentry, from_dip=True)
                    results.append(f"gmgn {(res or {}).get('status') or 'none'}")
                    if res is None:
                        results[-1] = "gmgn skipped"
                    elif (res or {}).get("status") == "refused":
                        results[-1] = f"gmgn refused: {res.get('reason')}"
                except Exception as e:
                    self.c.error("gmgn", "dip_buy", f"{row['mint']} {str(e)[:300]}")
                    results.append(f"gmgn error")
            if row["fomo_item"]:
                try:
                    item = json.loads(row["fomo_item"])
                    item["from_dip"] = True
                    item["dip_price"] = price
                    item["dip_liq"] = liq
                    ex = self.executor
                    if ex is None:
                        from agents.executor import Executor
                        ex = Executor(self.c)
                    out = await ex.run(item)
                    st = (out or {}).get("status") or "none"
                    results.append(f"fomo {st}")
                except Exception as e:
                    self.c.error("executor", "dip_buy", f"{row['mint']} {str(e)[:300]}")
                    results.append("fomo error")
            result = (note + " · " if note else "") + ("; ".join(results) or "fired")
            self.c.db.x("UPDATE gmgn_dips SET status='bought', reason=?, result=?, last_price=?, last_liq=?, updated=? WHERE id=?",
                        (reason, result, price, liq, self.c.clock(), row["id"]))
            try:
                label = "Bought on the dip" if reason in ("dip", "dip_gap") else "Bought (no dip)"
                self.c.activity("dip", f"{label} · {row['name']}", "Strategy A",
                                f"{drop:+.1f}%" if reason != "timeout" else "timeout",
                                f"${float(price):.8g}", row["mint"], "executor", "cyan")
            except Exception:
                pass
        finally:
            self._firing.discard(row["id"])
