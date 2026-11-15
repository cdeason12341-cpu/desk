"""GMGN hands-off mode (separate from FOMO tap-to-trade; FOMO is unchanged).

PAPER: on a coin that passed the desk, size it, run every cap check, call the real quote when the plan allows it,
       record a simulated fill + the exact swap body / condition orders that LIVE would send. Never calls swap.
LIVE:  signed swap from the owner's main wallet ONLY, with server-side exits attached in the same request
       (loss_stop at the desk stop %, loss_stop_trace drawdown = desk trail %, profit_stop_trace from trail start).
       Hard caps are enforced in code immediately before every signed buy. /halt stops all buys.
Missing strategy after a fill -> create a server-side stop_loss strategy -> if that fails, market-sell + alert.
The watcher (heartbeat 'gmgn') follows holdings + open strategies while the desk is awake."""
from __future__ import annotations
import asyncio, json, math, time
import gmgn
from gmgn import GmgnError, LAMPORTS, MAIN_WALLET, SOL
from ctx import iso

ACTIVE = ("submitting", "pending", "open", "exiting")          # count toward caps / block a 2nd buy
COUNTED = ACTIVE + ("closed", "unknown")                        # tickets used tonight
GATE_RULE = "LIVE gate: 1 full PAPER night -> 2h PAPER, user request 2026-11-14 5:42 PM CT"


class CapError(RuntimeError):
    pass


def conditions(s) -> list[dict]:
    """Exact condition orders attached to every GMGN buy (server-side exits)."""
    return [
        {"order_type": "loss_stop", "side": "sell", "price_scale": f"{s['risk.stop_pct']:g}", "sell_ratio": "100"},
        {"order_type": "loss_stop_trace", "side": "sell", "sell_ratio": "100", "drawdown_rate": f"{s['risk.trail_pct']:g}"},
        {"order_type": "profit_stop_trace", "side": "sell", "price_scale": f"{s['risk.trail_start_pct']:g}", "sell_ratio": "100",
         "drawdown_rate": f"{s['risk.trail_pct']:g}"},
    ]


def buy_body(s, mint: str, lamports: int) -> dict:
    return {"chain": gmgn.CHAIN, "from_address": MAIN_WALLET, "input_token": SOL, "output_token": mint,
            "input_amount": str(int(lamports)), "slippage": int(s["gmgn.slippage_pct"]), "is_anti_mev": True,
            "priority_fee": f"{s['gmgn.priority_fee_sol']:g}", "tip_fee": f"{s['gmgn.tip_fee_sol']:g}",
            "condition_orders": conditions(s), "sell_ratio_type": "hold_amount"}


def sell_all_body(s, mint: str) -> dict:
    return {"chain": gmgn.CHAIN, "from_address": MAIN_WALLET, "input_token": mint, "output_token": SOL,
            "input_amount": "0", "input_amount_bps": "10000", "slippage": max(int(s["gmgn.slippage_pct"]), 25),
            "is_anti_mev": True, "priority_fee": f"{s['gmgn.priority_fee_sol']:g}", "tip_fee": f"{s['gmgn.tip_fee_sol']:g}"}


def stop_strategy_body(s, mint: str, fill_price_usd: float, sol_usd: float) -> dict:
    """Repair: a server-side limit stop_loss for 100% of the position (check_price in the quote token, SOL)."""
    check = fill_price_usd * (1 - s["risk.stop_pct"] / 100.0) / sol_usd
    return {"chain": gmgn.CHAIN, "from_address": MAIN_WALLET, "base_token": mint, "quote_token": SOL,
            "order_type": "limit_order", "sub_order_type": "stop_loss", "check_price": f"{check:.12g}",
            "amount_in_percent": "100", "slippage": max(int(s["gmgn.slippage_pct"]), 25), "is_anti_mev": True,
            "priority_fee": f"{s['gmgn.priority_fee_sol']:g}", "tip_fee": f"{s['gmgn.tip_fee_sol']:g}"}


def fee_lamports(s) -> int:
    return int(round((s["gmgn.priority_fee_sol"] + s["gmgn.tip_fee_sol"]) * LAMPORTS))


def night_usage(db, night: str, mode: str) -> tuple[int, float]:
    r = db.one(f"SELECT COUNT(*) n, COALESCE(SUM(size_usd),0) u FROM gmgn_orders WHERE night=? AND mode=? AND status IN ({','.join('?'*len(COUNTED))})",
               (night, mode, *COUNTED))
    return int(r["n"]), float(r["u"])


def check_caps(s, db, night, mode, size_usd, lamports, sol_usd, wallet_lamports) -> None:
    """Hard caps. Raises CapError with a plain reason. Called before the order row exists."""
    cap = float(s["gmgn.cap_trade_usd"]); tickets = int(s["gmgn.max_tickets_night"])
    night_cap = cap * tickets
    if not (size_usd > 0) or not (sol_usd and sol_usd > 0) or lamports <= 0:
        raise CapError("bad size or SOL price")
    if size_usd > cap + 1e-9:
        raise CapError(f"${size_usd:,.2f} is over the per-trade cap ${cap:,.2f}")
    spend_usd = lamports / LAMPORTS * sol_usd
    if spend_usd > cap * 1.0001:
        raise CapError(f"{lamports / LAMPORTS:.4f} SOL (${spend_usd:,.2f}) is over the per-trade cap ${cap:,.2f}")
    n, used = night_usage(db, night, mode)
    if n >= tickets:
        raise CapError(f"night ticket cap reached ({n}/{tickets})")
    if used + size_usd > night_cap + 1e-9:
        raise CapError(f"night spend cap ${night_cap:,.2f} (${used:,.2f} used, +${size_usd:,.2f} would exceed it)")
    if wallet_lamports is None:
        raise CapError("wallet balance unknown")
    need = lamports + fee_lamports(s) + int(s["gmgn.sol_reserve"] * LAMPORTS)
    if need > wallet_lamports:
        raise CapError(f"wallet has {wallet_lamports / LAMPORTS:.4f} SOL, needs {need / LAMPORTS:.4f} SOL "
                       f"(ticket + fees + {s['gmgn.sol_reserve']:g} SOL reserve)")


def sim_exit(s, o, price, now):
    """PAPER mirror of the server-side exits + the desk timeout. Returns (reason|None, peak)."""
    entry = o["entry_price"]; peak = max(o["peak"] or entry, price)
    if price <= entry * (1 - s["risk.stop_pct"] / 100.0):
        return "loss_stop", peak
    if peak >= entry * (1 + s["risk.trail_start_pct"] / 100.0) and price <= peak * (1 - s["risk.trail_pct"] / 100.0):
        return "profit_stop_trace", peak
    if price <= peak * (1 - s["risk.trail_pct"] / 100.0):
        return "loss_stop_trace", peak
    if now - o["ts"] >= s["risk.timeout_h"] * 3600 and price < entry * (1 + s["risk.timeout_min_gain_pct"] / 100.0):
        return "timeout", peak
    return None, peak


EXIT_LABEL = {"loss_stop": "hard stop", "loss_stop_trace": "trailing stop", "profit_stop_trace": "trailing take-profit",
              "timeout": "timeout", "pulled": "liquidity pulled", "server": "server-side exit", "manual": "sold"}


class GmgnTrader:
    name = "gmgn"

    def __init__(self, ctx, client=None):
        self.c = ctx
        self.log = ctx.logs.get("gmgn") or ctx.logs["executor"]
        self.client = client
        self.lock = asyncio.Lock()
        self._sol = (0.0, None)          # (ts, usd)
        self._bal = (0.0, None)          # (ts, lamports)
        self.plan_blocked_at = 0.0       # quote 403 PLAN_UPGRADE_REQUIRED seen at (re-tried after 10 min: plans change)
        if ctx.db.meta("gmgn_paper_since") is None:
            ctx.db.set_meta("gmgn_paper_since", ctx.clock())
            ctx.db.set_meta("gmgn_paper_since_note", "gmgn PAPER start")

    # ---- state
    @property
    def s(self):
        return self.c.s

    def mode(self):
        return self.s["gmgn.mode"]

    def prefix(self):
        return "PAPER · GMGN · " if self.mode() == "PAPER" else "GMGN · "

    def halted(self) -> bool:
        return bool(self.c.db.meta("gmgn_halted", False))

    def halt(self, on: bool, who="telegram"):
        self.c.db.set_meta("gmgn_halted", bool(on))
        self.c.db.x("INSERT INTO change_log(ts,key,old,new,source) VALUES(?,?,?,?,?)",
                    (time.time(), "gmgn.halted", json.dumps(not on), json.dumps(bool(on)), who))

    def paper_since(self):
        return self.c.db.meta("gmgn_paper_since")

    def _log(self, *parts):
        import secrets_store
        self.log.info(secrets_store.redact("\t".join([iso()] + [str(p) for p in parts])))

    async def push(self, text, kind="gmgn", mint=None):
        try:
            return await self.c.notifier.push(text, None, kind=kind, mint=mint)
        except Exception as e:
            self.c.error("gmgn", "push", str(e)[:200])
            return False

    # ---- market data
    async def sol_usd(self):
        ts, v = self._sol
        if v and time.time() - ts < 60:
            return v
        try:
            d = await self.client.gas_price()
            v = float(d.get("native_token_usd_price") or 0) or None
        except Exception as e:
            self.c.error("gmgn", "sol_usd", str(e)[:200]); v = None
        if v:
            self._sol = (time.time(), v)
        return v or self._sol[1]

    async def wallet_lamports(self, fresh=False):
        ts, v = self._bal
        if v is not None and not fresh and time.time() - ts < 30:
            return v
        try:
            d = await self.client.user_info()
            for w in d.get("wallets") or []:
                if w.get("chain") == "sol" and w.get("address") == MAIN_WALLET:
                    for b in w.get("balances") or []:
                        if b.get("token_address") in (SOL, "So11111111111111111111111111111111111111111", "") or b.get("symbol") == "SOL":
                            v = int(round(float(b.get("balance") or 0) * LAMPORTS))
                            break
        except Exception as e:
            self.c.error("gmgn", "balance", str(e)[:200]); v = None
        if v is None and getattr(self.c, "net", None) is not None:
            try:
                r = await self.c.net.rpc("getBalance", [MAIN_WALLET])
                v = int((r or {}).get("value"))
            except Exception:
                v = None
        if v is not None:
            self._bal = (time.time(), v)
        return v

    def wallet_snapshot(self):
        """Last cached wallet SOL + USD. Never calls the API. Used by the read-only publisher."""
        bal_ts, lamports = self._bal
        _sol_ts, sol = self._sol
        sol_amt = (lamports / LAMPORTS) if lamports is not None else None
        usd = round(sol_amt * sol, 2) if (sol_amt is not None and sol) else None
        return {
            "sol": round(sol_amt, 6) if sol_amt is not None else None,
            "usd": usd,
            "solUsd": sol,
            "updatedAt": iso(bal_ts) if (lamports is not None and bal_ts) else None,
        }

    async def refresh_wallet(self, min_age=60):
        """Refresh cached balance and SOL price at most once every min_age seconds."""
        if self.client is None:
            return self.wallet_snapshot()
        now = time.time()
        if self._sol[1] is None or now - self._sol[0] >= min_age:
            await self.sol_usd()
        if self._bal[1] is None or now - self._bal[0] >= min_age:
            await self.wallet_lamports(fresh=True)
        return self.wallet_snapshot()

    def ticket_usd(self, liq):
        """The desk's current ticket (tracks bumps), never above the GMGN per-trade cap."""
        from agents.executor import size_ticket
        stack = self.c.balance()
        sz = size_ticket(self.s, stack, stack, liq)
        if sz["status"] != "ok":
            return None, sz["reason"]
        return round(min(sz["size"], float(self.s["gmgn.cap_trade_usd"])), 2), None

    # ---- entry
    async def on_pass(self, mint, name, symbol, price, liq, score=None):
        if not self.s["gmgn.enabled"] or self.client is None:
            return None
        async with self.lock:                          # one buy decision at a time: caps can't race
            try:
                return await self._on_pass(mint, name, symbol, price, liq, score)
            except Exception as e:
                self.c.error("gmgn", "on_pass", f"{mint} {str(e)[:300]}")
                return None

    def _row(self, **kw):
        cols = ",".join(kw); qs = ",".join("?" * len(kw))
        return self.c.db.x(f"INSERT INTO gmgn_orders({cols}) VALUES({qs})", tuple(kw.values())).lastrowid

    def _upd(self, oid, **kw):
        self.c.db.x(f"UPDATE gmgn_orders SET {', '.join(k + '=?' for k in kw)} WHERE id=?", (*kw.values(), oid))

    def _refuse(self, mint, name, mode, size, reason):
        self._row(ts=self.c.clock(), night=self.c.night, mode=mode, mint=mint, name=name, status="refused", reason=reason, size_usd=size or 0)
        self._log(mode, mint, "refused", reason)

    async def _on_pass(self, mint, name, symbol, price, liq, score):
        c, s = self.c, self.s
        mode = self.mode()
        if c.db.one("SELECT id FROM gmgn_orders WHERE mint=? AND night=? AND status NOT IN ('refused')", (mint, c.night)) or \
           c.db.one(f"SELECT id FROM gmgn_orders WHERE mint=? AND status IN ({','.join('?'*len(ACTIVE))})", (mint, *ACTIVE)):
            return None                                  # one GMGN buy per coin per night, never a 2nd on an open one
        if self.halted():
            self._refuse(mint, name, mode, 0, "halted")
            return {"status": "refused", "reason": "halted"}
        size, why = self.ticket_usd(liq)
        if size is None:
            self._refuse(mint, name, mode, 0, why)
            return {"status": "refused", "reason": why}
        sol_usd = await self.sol_usd()
        if not sol_usd:
            self._refuse(mint, name, mode, size, "no SOL price")
            await self.push(f"{self.prefix()}Skipped {name}: no SOL price from GMGN", mint=mint)
            return {"status": "refused", "reason": "no SOL price"}
        lamports = int(math.floor(size / sol_usd * LAMPORTS))
        wallet = await self.wallet_lamports(fresh=True)          # /v1/user/info before every sizing
        if mode == "PAPER" and wallet is not None:       # PAPER spends nothing real: count simulated open tickets against the wallet
            r = c.db.one(f"SELECT COALESCE(SUM(lamports),0) l FROM gmgn_orders WHERE mode='PAPER' AND status IN ({','.join('?'*len(ACTIVE))})", ACTIVE)
            wallet -= int(r["l"] or 0)
        try:
            check_caps(s, c.db, c.night, mode, size, lamports, sol_usd, wallet)
        except CapError as e:
            self._refuse(mint, name, mode, size, str(e))
            await self.push(f"{self.prefix()}Refused {name}: {e}", kind="gmgn_refused", mint=mint)
            return {"status": "refused", "reason": str(e)}
        body = buy_body(s, mint, lamports)
        exits = f"stop -{s['risk.stop_pct']:g}% · trail {s['risk.trail_pct']:g}% · take-profit trail from +{s['risk.trail_start_pct']:g}% (server-side)"
        if mode == "PAPER":
            q, qnote = await self.try_quote(mint, lamports)
            oid = self._row(ts=c.clock(), night=c.night, mode="PAPER", mint=mint, name=name, symbol=symbol, status="open",
                            size_usd=size, sol_usd=sol_usd, lamports=lamports, entry_price=price, peak=price, last_price=price,
                            last_liq=liq, quote=json.dumps(q) if q else qnote, body=json.dumps(body), conditions=json.dumps(body["condition_orders"]))
            self._log("PAPER", mint, "sim-fill", f"${size:.2f}", f"{lamports}", f"price={price}", qnote)
            await self.push(f"{self.prefix()}Buy {name} · ${size:,.2f} ({lamports / LAMPORTS:.4f} SOL) · simulated fill\n{exits}\nQuote: {qnote}", mint=mint)
            c.event("fill", mint)
            return {"status": "paper", "id": oid, "body": body}
        return await self.live_buy(mint, name, symbol, price, liq, size, sol_usd, lamports, body, exits)

    async def try_quote(self, mint, lamports):
        if time.time() - self.plan_blocked_at < 600:
            return None, "not available on your GMGN plan (free); simulated at the DexScreener price"
        try:
            q = await self.client.quote(SOL, mint, lamports, int(self.s["gmgn.slippage_pct"]))
            return q, f"GMGN quote out {q.get('output_amount')} (min {q.get('min_output_amount')})"
        except GmgnError as e:
            if e.plan_blocked:
                self.plan_blocked_at = time.time()
                return None, "not available on your GMGN plan (free); simulated at the DexScreener price"
            return None, f"quote failed ({e.error or e.status}); simulated at the DexScreener price"

    async def live_buy(self, mint, name, symbol, price, liq, size, sol_usd, lamports, body, exits):
        c, s = self.c, self.s
        # last line of defence, right before the signed swap
        if self.halted() or self.mode() != "LIVE" or not s["gmgn.enabled"]:
            self._refuse(mint, name, "LIVE", size, "halted or not LIVE at send time")
            return {"status": "refused", "reason": "halted"}
        wallet = await self.wallet_lamports(fresh=True)
        try:
            check_caps(s, c.db, c.night, "LIVE", size, lamports, sol_usd, wallet)
            if body["from_address"] != MAIN_WALLET or body["input_token"] != SOL or int(body["input_amount"]) != lamports:
                raise CapError("swap body mismatch")
        except CapError as e:
            self._refuse(mint, name, "LIVE", size, str(e))
            await self.push(f"GMGN · Refused {name}: {e}", kind="gmgn_refused", mint=mint)
            return {"status": "refused", "reason": str(e)}
        oid = self._row(ts=c.clock(), night=c.night, mode="LIVE", mint=mint, name=name, symbol=symbol, status="submitting",
                        size_usd=size, sol_usd=sol_usd, lamports=lamports, entry_price=price, peak=price, last_price=price, last_liq=liq,
                        body=json.dumps(body), conditions=json.dumps(body["condition_orders"]))
        try:
            r = await self.client.swap(body)
        except Exception as e:
            st = "unknown" if isinstance(e, GmgnError) and e.status == 0 else "failed"     # network error: may or may not have landed
            self._upd(oid, status=st, reason=str(e)[:300])
            self._log("LIVE", mint, "swap", st, str(e)[:200])
            await self.push(f"GMGN · Buy {name} {st}: {str(e)[:160]}" + ("\nCheck your GMGN wallet; I won't retry." if st == "unknown" else ""),
                            kind="gmgn_alert", mint=mint)
            return {"status": st}
        self._upd(oid, status="pending", order_id=r.get("order_id"), tx=r.get("hash"), strategy_order_id=r.get("strategy_order_id"))
        self._log("LIVE", mint, "swap sent", r.get("order_id"), r.get("status"))
        await self.push(f"GMGN · Buying {name} · ${size:,.2f} ({lamports / LAMPORTS:.4f} SOL)\n{exits}", mint=mint)
        return await self.settle(oid, r, polls=8, gap=5)

    async def settle(self, oid, r=None, polls=8, gap=5.0):
        """Poll query_order until confirmed/failed; on confirm make sure a server-side exit exists."""
        o = self.c.db.one("SELECT * FROM gmgn_orders WHERE id=?", (oid,))
        st = (r or {}).get("status")
        for i in range(polls):
            if st in ("confirmed", "successful", "failed", "expired"):
                break
            await asyncio.sleep(gap)
            try:
                r = await self.client.query_order(o["order_id"])
                st = r.get("status")
            except GmgnError as e:
                self._log("LIVE", o["mint"], "query_order", str(e)[:160])
        if st in ("failed", "expired"):
            self._upd(oid, status="failed", reason=(r or {}).get("error_status") or st)
            await self.push(f"GMGN · Buy {o['name']} {st}: {(r or {}).get('error_status') or ''}".strip(), kind="gmgn_alert", mint=o["mint"])
            return {"status": "failed"}
        if st not in ("confirmed", "successful"):
            return {"status": "pending"}                # watcher keeps polling
        rep = (r or {}).get("report") or {}
        fill = float(rep.get("price_usd") or 0) or o["entry_price"]
        sid = (r or {}).get("strategy_order_id") or o["strategy_order_id"]
        self._upd(oid, status="open", entry_price=fill, peak=fill, last_price=fill, tx=(r or {}).get("hash") or o["tx"], strategy_order_id=sid)
        self.c.event("fill", o["mint"])
        if not sid:
            return await self.repair(oid)
        await self.push(f"GMGN · Bought {o['name']} · ${o['size_usd']:,.2f} · exits live on GMGN", mint=o["mint"])
        return {"status": "open", "strategy_order_id": sid}

    async def repair(self, oid):
        """strategy_order_id missing: create a server-side stop now; if that fails, market-sell and alert."""
        o = self.c.db.one("SELECT * FROM gmgn_orders WHERE id=?", (oid,))
        sol_usd = o["sol_usd"] or await self.sol_usd()
        try:
            r = await self.client.strategy_create(stop_strategy_body(self.s, o["mint"], o["entry_price"], sol_usd))
            sid = (r or {}).get("order_id")
            if not sid:
                raise GmgnError(200, error="NO_ORDER_ID", message="strategy create returned no order_id")
            self._upd(oid, strategy_order_id=sid, repair="stop_loss strategy created")
            await self.push(f"GMGN · {o['name']}: exits weren't attached, so I created a server-side -{self.s['risk.stop_pct']:g}% stop. "
                            f"The trailing stop runs from my computer while it's awake.", kind="gmgn_alert", mint=o["mint"])
            return {"status": "open", "repair": "strategy"}
        except Exception as e:
            self._log("LIVE", o["mint"], "repair failed", str(e)[:200])
        ok = await self.market_sell(oid, "no server-side exit")
        await self.push(f"GMGN · ALERT {o['name']}: couldn't attach a server-side stop, so I market-sold it"
                        + ("" if ok else " — and the sell FAILED. Open GMGN and sell it yourself now."), kind="gmgn_alert", mint=o["mint"])
        return {"status": "exiting" if ok else "open", "repair": "market_sell" if ok else "failed"}

    async def market_sell(self, oid, reason):
        o = self.c.db.one("SELECT * FROM gmgn_orders WHERE id=?", (oid,))
        try:
            r = await self.client.swap(sell_all_body(self.s, o["mint"]))
            self._upd(oid, status="exiting", exit_reason=reason, sell_order_id=(r or {}).get("order_id"), repair=(o["repair"] or "") + " market_sell")
            self._log("LIVE", o["mint"], "market sell", reason, (r or {}).get("order_id"))
            return True
        except Exception as e:
            self._upd(oid, repair=(o["repair"] or "") + f" market_sell failed: {str(e)[:120]}")
            self._log("LIVE", o["mint"], "market sell failed", str(e)[:200])
            return False

    # ---- watcher (Risk side of GMGN)
    async def loop(self):
        while True:
            t0 = time.time()
            try:
                await self.watch_once()
                n = self.c.db.one(f"SELECT COUNT(*) n FROM gmgn_orders WHERE status IN ({','.join('?'*len(ACTIVE))})", ACTIVE)["n"]
                self.c.hb("gmgn", f"{self.mode()} open={n}{' HALTED' if self.halted() else ''}")
            except Exception as e:
                self.c.error("gmgn", "watch", str(e)[:300])
            await asyncio.sleep(max(1, self.s["risk.poll_s"] - (time.time() - t0)))

    async def watch_once(self, pairs=None, holdings=None, strategies=None):
        c, s = self.c, self.s
        rows = c.db.rows(f"SELECT * FROM gmgn_orders WHERE status IN ({','.join('?'*len(ACTIVE))})", ACTIVE)
        if not rows:
            return
        if pairs is None:
            pairs = await c.net.dex_tokens(sorted({r["mint"] for r in rows}))
        from agents.scanner import pick_pair
        import fields
        now = c.clock()
        live_due = [r for r in rows if r["mode"] == "LIVE" and now - (r["last_check"] or 0) >= s["gmgn.watch_s"]]
        if live_due and holdings is None:
            holdings = await self._holdings()
        if live_due and strategies is None:
            strategies = await self._strategies()
        for o in rows:
            pair = pick_pair(pairs, o["mint"])
            price = float(fields.get(pair, "priceUsd")) if pair and fields.get(pair, "priceUsd") else None
            liq = fields.get(pair, "liquidity.usd") if pair else None
            if o["mode"] == "PAPER":
                await self._paper_tick(o, price, liq, now)
            elif o in live_due:
                await self._live_tick(o, price, liq, now, holdings, strategies)

    async def _paper_tick(self, o, price, liq, now):
        if price is None:
            return
        if liq is not None and float(liq) <= 0:
            return await self._paper_close(o, "pulled", o["last_price"] or price, now)
        reason, peak = sim_exit(self.s, o, price, now)
        self._upd(o["id"], peak=peak, last_price=price, last_liq=liq)
        if reason:
            await self._paper_close(o, reason, price, now)

    def _pnl(self, o, exit_price):
        size = o["size_usd"]; fee = self.s["gmgn.fee_pct"] / 100.0
        gross = size * (exit_price / o["entry_price"] - 1)
        fees = size * fee + size * (exit_price / o["entry_price"]) * fee
        net = gross - fees
        return round(net, 2), round(100 * net / size, 2)

    async def _paper_close(self, o, reason, price, now):
        pnl, pct = self._pnl(o, price)
        self._upd(o["id"], status="closed", exit_reason=reason, exit_price=price, pnl_usd=pnl, pct=pct, closed_at=now)
        sign = "+" if pnl >= 0 else "-"
        await self.push(f"PAPER · GMGN · Sold {o['name']} · {EXIT_LABEL.get(reason, reason)} · {sign}${abs(pnl):,.2f} ({pct:+.1f}%)", mint=o["mint"])
        self.c.event("exit", o["mint"])

    async def _holdings(self):
        try:
            d = await self.client.wallet_holdings()
            return {h.get("token", {}).get("address") or h.get("token_address") or h.get("address"): float(h.get("balance") or 0)
                    for h in (d or {}).get("list") or []}
        except Exception as e:
            self.c.error("gmgn", "holdings", str(e)[:200]); return None

    async def _strategies(self):
        try:
            d = await self.client.strategy_orders()
            out = {}
            for x in (d or {}).get("list") or []:
                out.setdefault(x.get("base_token") or x.get("token_address"), []).append(x)
            return out
        except Exception as e:
            self.c.error("gmgn", "strategies", str(e)[:200]); return None

    async def _live_tick(self, o, price, liq, now, holdings, strategies):
        self._upd(o["id"], last_check=now)
        if price is not None:
            self._upd(o["id"], last_price=price, peak=max(o["peak"] or price, price), last_liq=liq)
        if o["status"] == "pending":
            return await self.settle(o["id"], None, polls=1, gap=0)
        if o["status"] == "submitting":
            return                                       # crashed mid-send: never resend; owner was alerted at send time
        held = None if holdings is None else holdings.get(o["mint"], 0.0)
        if held is not None and held <= 0 and now - o["ts"] > 60:
            exit_price = price or o["last_price"]
            pnl, pct = self._pnl(o, exit_price) if exit_price else (None, None)
            self._upd(o["id"], status="closed", exit_reason=o["exit_reason"] or "server", exit_price=exit_price, pnl_usd=pnl, pct=pct, closed_at=now)
            await self.push(f"GMGN · {o['name']} sold ({EXIT_LABEL.get(o['exit_reason'] or 'server')})"
                            + (f" · about {pct:+.1f}%" if pct is not None else ""), mint=o["mint"])
            self.c.event("exit", o["mint"])
            return
        if o["status"] == "exiting":
            return
        if strategies is not None and o["mint"] not in strategies and not o["repair"]:
            await self.repair(o["id"]); return
        if liq is not None and float(liq) <= 0:
            if await self.market_sell(o["id"], "pulled"):
                await self.push(f"GMGN · {o['name']}: liquidity pulled, market-selling", kind="gmgn_alert", mint=o["mint"])
            return
        if price is not None:
            reason, _ = sim_exit(self.s, o, price, now)
            if reason == "timeout":
                if await self.market_sell(o["id"], "timeout"):
                    await self.push(f"GMGN · {o['name']}: dead money after {self.s['risk.timeout_h']:g}h, market-selling", mint=o["mint"])

    # ---- gate
    def record_gate_rule(self):
        """Owner rule change (2026-11-14 5:42 PM CT): GMGN LIVE after 2h of GMGN PAPER (was 1 full PAPER night). Logged once."""
        if self.c.db.meta("gmgn_gate_rule_logged"):
            return
        self.c.db.x("INSERT INTO change_log(ts,key,old,new,source) VALUES(?,?,?,?,?)",
                    (time.time(), "gmgn.live_gate", json.dumps("1 full PAPER night"), json.dumps("2h PAPER"),
                     "owner via Grok Bot app · " + GATE_RULE))
        self.c.db.set_meta("gmgn_gate_rule_logged", True)

    async def live_check(self) -> str | None:
        """None when GMGN may go LIVE, else the plain reason."""
        s, c = self.s, self.c
        if self.mode() == "LIVE":
            return "GMGN is already LIVE."
        since = self.paper_since()
        need = s["gmgn.live_gate_h"] * 3600
        if since is None or c.clock() - since < need:
            left = need - (c.clock() - (since or c.clock()))
            return f"GMGN LIVE needs {s['gmgn.live_gate_h']:g}h of GMGN PAPER first ({left / 60:.0f} min left)."
        if self.halted():
            return "GMGN is halted. Send /unhalt first."
        if self.client is None or not self.client.configured:
            return "GMGN keys aren't set up."
        sol = await self.sol_usd()
        try:
            await self.client.quote(SOL, "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v", 1_000_000, 15)
        except GmgnError as e:
            if e.plan_blocked:
                return ("Your GMGN plan (free) can't place trades through the API. "
                        "Upgrade at gmgn.ai/ai (Plus or Pro), then send 'gmgn live' again.")
            return f"GMGN check failed: {e.error or e.status}"
        wallet = await self.wallet_lamports(fresh=True)
        cap = s["gmgn.cap_trade_usd"]
        if not sol or wallet is None:
            return "Couldn't read the wallet balance or SOL price."
        need_l = int(cap / sol * LAMPORTS) + fee_lamports(s) + int(s["gmgn.sol_reserve"] * LAMPORTS)
        if wallet < need_l:
            return f"Wallet has {wallet / LAMPORTS:.4f} SOL, one ${cap:,.2f} ticket needs {need_l / LAMPORTS:.4f} SOL."
        return None

    def status_line(self):
        n, used = night_usage(self.c.db, self.c.night, self.mode())
        s = self.s
        el = (self.c.clock() - (self.paper_since() or self.c.clock())) / 3600
        return (f"GMGN {self.mode()}{' · HALTED' if self.halted() else ''}{'' if s['gmgn.enabled'] else ' · off'} · "
                f"tonight {n}/{s['gmgn.max_tickets_night']} tickets, ${used:,.2f}/${s['gmgn.cap_trade_usd'] * s['gmgn.max_tickets_night']:,.2f} · "
                f"cap ${s['gmgn.cap_trade_usd']:,.2f}/trade" + (f" · PAPER {el:.1f}h of {s['gmgn.live_gate_h']:g}h" if self.mode() == "PAPER" else ""))
