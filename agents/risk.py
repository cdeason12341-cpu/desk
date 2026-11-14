"""AGENT 5 · RISK. Exits by rule; sends the Sell button. Never places an order, never moves a stop down."""
from __future__ import annotations
import asyncio, json, time
import fees, fields, links
from ctx import iso
from net import RateLimited


def evaluate(s, pos: dict, price: float, now: float) -> tuple[str | None, dict]:
    """Pure rule pass, in order: peak, stop, trail on, trail, timeout. Returns (exit_reason|None, updates)."""
    entry = pos["entry_price"]
    peak = max(pos["peak"] or entry, price)
    upd = {"peak": peak, "last_price": price}
    if price <= entry * (1 - pos["stop_pct"] / 100.0):
        return "stop", upd
    trail = bool(pos["trail_active"])
    if price >= entry * (1 + s["risk.trail_start_pct"] / 100.0):
        trail = True
    upd["trail_active"] = 1 if trail else 0
    if trail and price <= peak * (1 - s["risk.trail_pct"] / 100.0):
        return "trail", upd
    if now - pos["opened_at"] >= s["risk.timeout_h"] * 3600 and price < entry * (1 + s["risk.timeout_min_gain_pct"] / 100.0):
        return "timeout", upd
    return None, upd


def lines(s, pos: dict) -> dict:
    entry, peak = pos["entry_price"], pos["peak"] or pos["entry_price"]
    stop = entry * (1 - pos["stop_pct"] / 100.0)
    trail = peak * (1 - s["risk.trail_pct"] / 100.0) if pos["trail_active"] else None
    return {"stop": stop, "trail": trail, "exit_line": max(stop, trail or 0)}


def pnl(s, pos, exit_price):
    size = pos["size_usd"]
    gross = size * (exit_price / pos["entry_price"] - 1)
    f = fees.round_trip(size, size * exit_price / pos["entry_price"], s["desk.fees_ref_discount"])
    net = gross - f
    return round(net, 2), round(100.0 * net / size, 2), round(f, 4)


def exit_text(prefix, reason, name, pos, pnl_usd, pct, s):
    if reason == "trail":
        sign = "+" if pnl_usd >= 0 else "-"
        return f"{prefix}Sold {name} · {sign}${abs(pnl_usd):,.2f} ({pct:+.1f}%)"
    if reason == "stop":
        return f"{prefix}{name} hit the -{pos['stop_pct']:g}% stop · -${abs(pnl_usd):,.2f}"
    if reason == "timeout":
        return f"{prefix}Sell {name}, dead money · {pct:+.1f}% in {s['risk.timeout_h']:g}h"
    return f"{prefix}Sell {name} now, liquidity pulled · {pct:+.1f}%"


def sell_buttons(mint, mode):
    b = [[{"text": "Sell on FOMO", "url": links.coin_url(mint, "solana")}]]
    if mode == "LIVE":
        b.append([{"text": "Sold", "callback_data": f"sold:{mint}"}])
    return b


class Risk:
    name = "risk"

    def __init__(self, ctx):
        self.c = ctx
        self.log = ctx.logs["risk"]
        ctx.s.listeners.append(self.on_setting)
        self.last_exit = None

    def on_setting(self, key, old, new):
        if key == "risk.stop_pct":       # applies to open positions only if tighter
            self.c.db.x("UPDATE positions SET stop_pct=? WHERE status='open' AND stop_pct>?", (new, new))

    def open_positions(self):
        return self.c.db.rows("SELECT * FROM positions WHERE status IN ('open','exiting')")

    async def loop(self):
        while True:
            t0 = time.time()
            try:
                await self.poll_once()
                self.c.hb("risk", f"open={len(self.open_positions())}")
            except RateLimited:
                self.log.info(f"{iso()}\t429\tback off {self.c.s['risk.backoff_429_s']}s, polls missed")
                await asyncio.sleep(self.c.s["risk.backoff_429_s"])
            except Exception as e:
                self.c.error("risk", "poll", str(e)[:300])
            await asyncio.sleep(max(1, self.c.s["risk.poll_s"] - (time.time() - t0)))

    async def poll_once(self, pairs=None):
        c, s = self.c, self.c.s
        pos = self.open_positions()
        if not pos:
            return
        if pairs is None:
            pairs = await c.net.dex_tokens(sorted({p["mint"] for p in pos}))
        from agents.scanner import pick_pair
        now = c.clock()
        for p in pos:
            pair = pick_pair(pairs, p["mint"])
            if p["status"] == "exiting":
                await self.maybe_repush(p, now)
                continue
            price = float(fields.get(pair, "priceUsd")) if pair and fields.get(pair, "priceUsd") else None
            liq = fields.get(pair, "liquidity.usd") if pair else None
            if price is None:
                n = (p["no_price"] or 0) + 1
                c.db.x("UPDATE positions SET no_price=? WHERE id=?", (n, p["id"]))
                self.log.info(f"{iso(now)}\t{p['mint']}\tprice=None\tno_price={n}")
                if n == int(s["risk.no_price_polls"]):
                    await c.notifier.push(f"{c.prefix()}{p['name']}: no price for 1 min", sell_buttons(p["mint"], p["mode"]), kind="risk", mint=p["mint"])
                continue
            c.db.x("INSERT INTO prices(ts,mint,price,vol_m5,src) VALUES(?,?,?,?,?)", (now, p["mint"], price, fields.get(pair, "volume.m5"), "risk"))
            if liq is not None and float(liq) <= 0:
                await self.exit(p, "pulled", p["last_price"] or price, now)
                continue
            reason, upd = evaluate(s, p, price, now)
            c.db.x("UPDATE positions SET peak=?, last_price=?, trail_active=?, last_liq=?, no_price=0 WHERE id=?",
                   (upd["peak"], price, upd.get("trail_active", p["trail_active"]), liq, p["id"]))
            p.update(upd)
            ln = lines(s, p)
            self.log.info(f"{iso(now)}\t{p['mint']}\tprice={price:.10g}\tpeak={upd['peak']:.10g}\tstop={ln['stop']:.10g}"
                          f"\ttrail={ln['trail'] if ln['trail'] is None else format(ln['trail'], '.10g')}\taction={reason or 'hold'}")
            if reason:
                await self.exit(p, reason, price, now)

    async def exit(self, p, reason, price, now):
        c, s = self.c, self.c.s
        pnl_usd, pct, f = pnl(s, p, price)
        name = p["name"]
        text = exit_text(c.prefix() if p["mode"] == "PAPER" else "", reason, name, p, pnl_usd, pct, s)
        status = "closed" if p["mode"] == "PAPER" else "exiting"
        c.db.x("UPDATE positions SET status=?, exit_price=?, exit_reason=?, pnl_usd=?, pct=?, fees_usd=?, exit_pushed_at=?, closed_at=? WHERE id=?",
               (status, price, reason, pnl_usd, pct, f, now, now if status == "closed" else None, p["id"]))
        out = {"agent": "risk", "mint": p["mint"], "name": name, "ts": iso(now), "reason": reason,
               "entryPrice": p["entry_price"], "exitPrice": price, "peakPrice": p["peak"], "pnlUsd": pnl_usd, "pct": pct,
               "feesUsd": f, "stopPct": p["stop_pct"], "mode": p["mode"], "status": status, "link": links.coin_url(p["mint"], "solana")}
        self.last_exit = out
        c.db.set_meta("risk_last_exit", out)
        sign = "+" if pnl_usd >= 0 else "-"
        if reason == "stop":
            c.activity("stop", f"Stopped {name}", f"Risk · -{p['stop_pct']:g}% stop", f"-${abs(pnl_usd):,.2f}", f"{pct:+.1f}%", p["mint"], "risk", "red")
        elif reason == "trail":
            c.activity("sell", f"Sold {name}", "Risk · trailing stop", f"{sign}${abs(pnl_usd):,.2f}", f"{pct:+.1f}%", p["mint"], "risk", "green" if pnl_usd >= 0 else "red")
        elif reason == "timeout":
            c.activity("sell", f"Sold {name}", f"Risk · timeout {s['risk.timeout_h']:g}h", f"{sign}${abs(pnl_usd):,.2f}", f"{pct:+.1f}%", p["mint"], "risk", "green" if pnl_usd >= 0 else "red")
        else:
            c.activity("sell", f"Pulled {name}", "Risk · liquidity pulled", f"{sign}${abs(pnl_usd):,.2f}", f"{pct:+.1f}%", p["mint"], "risk", "red")
        await c.notifier.push(text, sell_buttons(p["mint"], p["mode"]), kind="sell", mint=p["mint"])
        if status == "closed":
            c.record_balance()
        c.event("exit", p["mint"])
        return out, text

    async def maybe_repush(self, p, now):
        if not p["repushed"] and p["exit_pushed_at"] and now - p["exit_pushed_at"] > self.c.s["risk.confirm_window_min"] * 60:
            self.c.db.x("UPDATE positions SET repushed=1 WHERE id=?", (p["id"],))
            await self.c.notifier.push(f"{p['name']} still open · tap Sell on FOMO", sell_buttons(p["mint"], p["mode"]), kind="sell", mint=p["mint"])

    async def on_sold(self, mint):
        p = self.c.db.one("SELECT * FROM positions WHERE mint=? AND status='exiting' ORDER BY id DESC LIMIT 1", (mint,))
        if not p:
            return "no exiting position for that coin"
        self.c.db.x("UPDATE positions SET status='closed', closed_at=? WHERE id=?", (self.c.clock(), p["id"]))
        self.c.record_balance()
        self.c.event("exit", mint)
        return f"Closed {p['name']} · {p['pct']:+.1f}%"
