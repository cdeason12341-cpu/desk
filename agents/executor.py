"""AGENT 4 · EXECUTOR. Arithmetic + 1 button. FOMO has no public order API: never places an order."""
from __future__ import annotations
import asyncio, json, time
import fields, links
from ctx import iso


def size_ticket(s, stack: float, free: float, pool_liq: float | None) -> dict:
    """Returns {'size','raw','cap','status','reason'}; status 'ok' or 'skipped'."""
    raw = stack * s["executor.stack_pct"] / 100.0
    size, cap = raw, "stack_pct"
    if pool_liq is not None:
        pool_cap = pool_liq * s["executor.max_pool_pct"] / 100.0
        if pool_cap < size:
            size, cap = pool_cap, "max_pool_pct"
    mn = s["executor.min_ticket_usd"]
    if size < mn:
        if s["executor.below_min"] == "bump":
            if free >= mn:
                return {"size": round(mn, 2), "raw": round(raw, 2), "cap": cap + "+bump", "status": "ok", "reason": None}
            return {"size": 0, "raw": round(raw, 2), "cap": cap, "status": "skipped", "reason": f"free ${free:,.2f} under ${mn:,.2f}"}
        return {"size": 0, "raw": round(raw, 2), "cap": cap, "status": "skipped", "reason": f"below min ${mn:,.2f}"}
    if size > free:
        if free >= mn:
            size, cap = free, "free"
        else:
            return {"size": 0, "raw": round(raw, 2), "cap": "free", "status": "skipped", "reason": f"free ${free:,.2f} under ${mn:,.2f}"}
    return {"size": round(size, 2), "raw": round(raw, 2), "cap": cap, "status": "ok", "reason": None}


def buy_text(prefix, name, size, pct, score):
    return f"{prefix}Buy {name} · ${size:,.2f} · {pct:.2f}% of stack · score {score}"


def buy_buttons(mint, mode):
    b = [[{"text": "Buy on FOMO", "url": links.coin_url(mint, "solana")}]]
    if mode == "LIVE":
        b.append([{"text": "Bought", "callback_data": f"bought:{mint}"}, {"text": "Skip", "callback_data": f"skip:{mint}"}])
    return b


class Executor:
    name = "executor"

    def __init__(self, ctx):
        self.c = ctx
        self.log = ctx.logs["executor"]
        self.pending: dict[str, dict] = {}      # LIVE: mint -> waiting for Bought/Skip
        self.bar: dict[str, dict] = {}          # app progress bar

    async def loop(self):
        while True:
            self.c.hb("executor", f"queue={self.c.bus.executor.qsize()} pending={len(self.pending)}")
            await self.expire_pending()
            try:
                item = await asyncio.wait_for(self.c.bus.executor.get(), timeout=10)
            except asyncio.TimeoutError:
                continue
            while "executor" in self.c.paused:
                self.c.hb("executor", "paused")
                await asyncio.sleep(5)
            try:
                await self.run(item)
            except Exception as e:
                self.c.error("executor", "run", f"{item['scan'].get('mint')} {str(e)[:300]}")

    def _record(self, out, stack, sz):
        c = self.c
        c.db.x("INSERT INTO executions(ts,mint,status,reason,size_usd,json,night) VALUES(?,?,?,?,?,?,?)",
               (c.clock(), out["mint"], out["status"], out["reason"], out["sizeUsd"], json.dumps(out), c.night))
        self.log.info(f"{iso()}\t{out['mint']}\tstack={stack:.2f}\traw={sz.get('raw')}\tcap={sz.get('cap')}\tsize={out['sizeUsd']}"
                      f"\t{out['mode']}\t{out['status']}\t{out['reason'] or ''}\t{out['link']}")
        c.event("signal", out["mint"])

    async def run(self, item, pair=None):
        c, s = self.c, self.c.s
        scan, an = item["scan"], item["analyst"]
        mint = scan["mint"]
        mode = c.mode()
        link = links.coin_url(mint, "solana")
        if c.db.one("SELECT id FROM executions WHERE mint=? AND night=? AND status IN ('sent','bought')", (mint, c.night)):
            return None                                       # 1 push per coin per night
        if c.db.one("SELECT id FROM positions WHERE mint=? AND status IN ('open','exiting')", (mint,)):
            return None                                       # never a 2nd buy on an open position
        base = {"agent": "executor", "mint": mint, "signalPrice": None, "sizeUsd": 0, "stackPct": 0,
                "link": link, "mode": mode, "status": "skipped", "reason": None}
        if pair is None:
            try:
                from agents.scanner import pick_pair
                pair = pick_pair(await c.net.dex_tokens([mint]), mint)
            except Exception:
                pair = None
        price = fields.get(pair, "priceUsd") if pair else None
        stack, free = c.balance(), c.free()
        if not price:
            out = dict(base, reason="no price")
            self._record(out, stack, {}); c.skip(mint, "executor", "no price")
            return out
        price = float(price)
        liq = float(fields.get(pair, "liquidity.usd") or 0)
        scan_liq = scan.get("liquidityUsd") or 0
        if scan_liq and liq < scan_liq * (1 - s["executor.recheck_liq_drop_pct"] / 100.0):
            out = dict(base, signalPrice=price, reason=f"liquidity down {100*(1-liq/scan_liq):.0f}%")
            self._record(out, stack, {}); c.skip(mint, "executor", out["reason"])
            return out
        ref = item.get("ref_price")
        if ref and price > ref * (1 + s["executor.recheck_price_up_pct"] / 100.0):
            out = dict(base, signalPrice=price, reason=f"price up {100*(price/ref-1):.0f}% since signal")
            self._record(out, stack, {}); c.skip(mint, "executor", out["reason"])
            return out
        sz = size_ticket(s, stack, free, liq)
        if sz["status"] != "ok":
            out = dict(base, signalPrice=price, reason=sz["reason"])
            self._record(out, stack, sz); c.skip(mint, "executor", sz["reason"])
            return out
        size = sz["size"]
        pct = 100.0 * size / stack if stack else 0
        name = scan.get("name") or c.label(mint)
        text = buy_text(c.prefix(), name, size, pct, an.get("score"))
        ok = await self.push(text, buy_buttons(mint, mode), mint)
        out = dict(base, signalPrice=price, sizeUsd=size, stackPct=round(pct, 2), status="sent")
        if mode == "PAPER":
            out["status"] = "bought"                         # paper fills at the signal price when the push goes out
            if not ok:
                out["reason"] = "push failed"
            self._record(out, stack, sz)
            self.bar[mint] = {"pct": 100, "ts": c.clock()}
            self.open_position(mint, name, scan.get("symbol"), price, size, pct, liq)
            return out
        if not ok:
            out.update(status="skipped", reason="push failed")
            self._record(out, stack, sz); c.skip(mint, "executor", "push failed")
            return out
        self.pending[mint] = {"out": out, "name": name, "symbol": scan.get("symbol"), "since": c.clock(), "liq": liq, "pct": pct}
        self._record(out, stack, sz)
        return out

    async def push(self, text, buttons, mint):
        for i in range(3):
            try:
                if await self.c.notifier.push(text, buttons, kind="buy", mint=mint):
                    return True
            except Exception:
                pass
            await asyncio.sleep(1 + i)
        return False

    def open_position(self, mint, name, symbol, price, size, pct, liq):
        c = self.c
        c.db.x("INSERT INTO positions(mint,name,symbol,mode,entry_price,size_usd,peak,stop_pct,trail_active,opened_at,status,last_price,last_liq,night)"
               " VALUES(?,?,?,?,?,?,?,?,0,?,'open',?,?,?)",
               (mint, name, symbol, c.mode(), price, size, price, c.s["risk.stop_pct"], c.clock(), price, liq, c.night))
        tm = time.strftime("%H:%M", time.localtime(c.clock()))
        c.activity("buy", f"Bought {name}", f"Executor · {tm}", f"${size:,.2f}", f"{pct:.2f}% of stack", mint, "executor", "cyan")
        c.record_balance()
        c.event("fill", mint)

    async def on_bought(self, mint):
        p = self.pending.pop(mint, None)
        if not p:
            return "nothing pending for that coin"
        out = p["out"]
        self.open_position(mint, p["name"], p["symbol"], out["signalPrice"], out["sizeUsd"], p["pct"], p["liq"])
        self.c.db.x("UPDATE executions SET status='bought' WHERE mint=? AND night=? AND status='sent'", (mint, self.c.night))
        return f"Position open: {p['name']} ${out['sizeUsd']:,.2f}"

    async def on_skip(self, mint):
        p = self.pending.pop(mint, None)
        if not p:
            return "nothing pending for that coin"
        self.c.db.x("UPDATE executions SET status='skipped', reason='skipped by you' WHERE mint=? AND night=? AND status='sent'", (mint, self.c.night))
        self.c.skip(mint, "executor", "skipped by you")
        return f"Skipped {p['name']}"

    async def expire_pending(self):
        win = self.c.s["executor.confirm_window_min"] * 60
        for mint, p in list(self.pending.items()):
            if self.c.clock() - p["since"] > win:
                self.pending.pop(mint, None)
                self.c.db.x("UPDATE executions SET status='missed', reason='no tap' WHERE mint=? AND night=? AND status='sent'", (mint, self.c.night))
                self.c.skip(mint, "executor", "missed (no tap)")
