"""STRATEGY B · LIVE (owner 2026-11-17 8:13 AM CT, t304u: "Switch strat a to paper and strat b to live"; DECISION 66).

Only the fill / exit path of Strategy B. Entry (filters, triggers, watch logic, B's feed) stays in strat_b.py, unchanged.
Used only when strat_b.mode == LIVE (force-only, owner command). Real GMGN swaps from the same main wallet and the
same signed client as Strategy A (ctx.gmgn), with A's safety:
- free SOL = wallet - 0.02 SOL reserve (gmgn.sol_reserve) - SOL in flight (A LIVE + B LIVE); refuse with a reason if short.
- size: strat_b.trade_usd when that strategy's share of free SOL covers it with fees and rent, else A's own rule
  (pick_size): gmgn.fallback_trade_usd, else whatever free SOL covers down to gmgn.min_trade_usd (owner 8:15 AM CT,
  t305u; $50 then $20 then $1 as of DECISION 85).
- A's per-trade cap (gmgn.cap_trade_usd); since DECISION 71 B has its own LIVE slots (strat_b.live_max_open) and night
  tickets (strat_b.live_max_tickets_night), and sizes against its own share of the wallet (strat_b.live_wallet_share_pct,
  formula in agents/gmgn_trader.py). A coin held by A LIVE is never bought by B (and vice versa).
- /halt stops B LIVE buys too.
- a GMGN server-side loss_stop rides on the buy (survives box sleep): -strat_b.early_stop_pct (default 50) for the
  first strat_b.early_stop_min (default 5) minutes, then the desk creates a new -strat_b.stop_pct (35) stop and
  cancels the old one (create-new then cancel-old, same pattern as A's grace swap). If the box was paused past the
  window, the tighten runs on resume. If GMGN doesn't show the stop after the fill, a limit stop_loss is created
  at the current window's percent; if that fails, market-sell and alert.
- the desk runs B's other exits (take profit, trailing take-profit with A's +5% net floor, time stop, liquidity
  pulled, plus a backup stop) with real sells on A's real-time pool price feed. The old spike sell is gone
  (DECISION 85). Closes and P&L come from the chain only (SOL in vs SOL out, fees included).
Never reads or asks for a private key; secrets are never logged (log lines are redacted)."""
from __future__ import annotations
import asyncio, collections, json, time
import chain, links
from ctx import iso
from agents import gmgn_trader as A
from gmgn import LAMPORTS, MAIN_WALLET, SOL

LIVE_LABEL = "B LIVE"
REFUSE_RETRY_S = 600          # a refused coin is tried again at most every 10 min while it stays in the buy zone
REFUSE_PUSH_GAP_S = 1800      # wallet / cap refusals push at most once per 30 min (each coin's first refusal tonight pushes)
CLOSE_CHECK_S = 15.0          # chain close check / pending settle cadence
SELL_RETRY_S = 15.0


def b_conditions(s, phase=None) -> list[dict]:
    """B's only server-side exit: the hard stop. phase='early' uses -early_stop_pct; 'normal' uses -stop_pct.
    B runs TP / trail / time stop itself. phase=None means whatever a new buy would attach."""
    import strat_b as B
    if phase is None:
        phase = "early" if float(s["strat_b.early_stop_min"] or 0) > 0 else "normal"
    stop = s["strat_b.early_stop_pct"] if phase == "early" else s["strat_b.stop_pct"]
    return [{"order_type": "loss_stop", "side": "sell", "price_scale": f"{stop:g}", "sell_ratio": "100"}]


def b_buy_body(s, mint: str, lamports: int, phase=None) -> dict:
    body = A.buy_body(s, mint, lamports)
    body["condition_orders"] = b_conditions(s, phase)
    return body


def budget(c):
    """B's DECISION 71 wallet-share view from the cached wallet read (no network call)."""
    g = getattr(c, "gmgn", None)
    lam = getattr(g, "_bal", (0, None))[1] if g is not None else None
    sol = getattr(g, "_sol", (0, None))[1] if g is not None else None
    return A.budget_view(c.s, c.db, "B", lam, sol)


def wallet_view(c):
    """(cached wallet snapshot, free SOL after reserve + in-flight buys) from A's last wallet read. No network call."""
    g = getattr(c, "gmgn", None)
    if g is None or not hasattr(g, "wallet_snapshot"):
        return None, None
    snap, lam = g.wallet_snapshot(), getattr(g, "_bal", (0, None))[1]
    if lam is None:
        return snap, None
    return snap, round(max(0, A.free_lamports(c.s, lam, A.committed_lamports(c.db, "LIVE"))) / LAMPORTS, 6)


class BLive:
    ATTACH_POLLS = 15
    ATTACH_GAP_S = 3.0
    ATTACH_GRACE_POLLS = 4
    SETTLE_GAP_S = 5.0

    def __init__(self, sb):
        self.sb = sb
        self.c = sb.c
        self.log = sb.log
        self._busy: set = set()
        self._retry_buy: dict = {}      # mint -> (until, reason)
        self._retry_sell: dict = {}     # order id -> earliest retry
        self._last_close = 0.0
        self._trail_below_floor: set = set()

    @property
    def t(self):
        return getattr(self.c, "gmgn", None)

    def _log(self, *parts):
        import secrets_store
        self.log.info(secrets_store.redact("\t".join([iso(self.c.clock())] + [str(p) for p in parts])))

    def _row(self, **kw):
        cols = ",".join(kw); qs = ",".join("?" * len(kw))
        return self.c.db.x(f"INSERT INTO strat_b_orders({cols}) VALUES({qs})", tuple(kw.values())).lastrowid

    def _upd(self, oid, **kw):
        self.c.db.x(f"UPDATE strat_b_orders SET {', '.join(k + '=?' for k in kw)} WHERE id=?", (*kw.values(), oid))

    def _get(self, oid):
        return self.c.db.one("SELECT * FROM strat_b_orders WHERE id=?", (oid,))

    async def _push(self, text, mint):
        await self.sb._push(text, mint)

    def blocked_retry(self, mint, now):
        r = self._retry_buy.get(mint)
        if r and now < r[0]:
            return r[1]
        return None

    # ---------- buy
    async def _refuse(self, w, reason, size, now, wallet_kind=False):
        c = self.c
        first = not c.db.one("SELECT id FROM strat_b_orders WHERE mode='LIVE' AND status='refused' AND mint=? AND night=?", (w["mint"], c.night))
        self._row(ts=now, night=c.night, mode="LIVE", mint=w["mint"], name=w["name"], symbol=w.get("symbol"), status="refused",
                  size_usd=size or 0, note=reason, opened_at=now)
        self._retry_buy[w["mint"]] = (now + REFUSE_RETRY_S, reason)
        self._log("LIVE refused", w["mint"], w["name"], reason)
        last = float(c.db.meta("strat_b_live_refuse_push_at", 0) or 0)
        if first and (not wallet_kind or now - last >= REFUSE_PUSH_GAP_S):
            if wallet_kind:
                c.db.set_meta("strat_b_live_refuse_push_at", now)
            await self._push(f"{LIVE_LABEL} · Skipped {w['name']}: {reason}", w["mint"])
        return {"status": "refused", "reason": reason}

    async def buy(self, w, m, why, now) -> dict:
        c, s, t = self.c, self.c.s, self.t
        mint, name = w["mint"], w["name"]
        if s["strat_b.mode"] != "LIVE":
            return await self._refuse(w, "not LIVE", 0, now)
        if t is None or t.client is None:
            return await self._refuse(w, "GMGN client not configured", 0, now)
        if t.halted():
            return await self._refuse(w, "GMGN halted (/halt)", 0, now)
        act = ",".join("?" * len(A.ACTIVE))
        if c.db.one(f"SELECT id FROM gmgn_orders WHERE mint=? AND mode='LIVE' AND status IN ({act})", (mint, *A.ACTIVE)) or \
                c.db.one(f"SELECT id FROM strat_b_orders WHERE mint=? AND mode='LIVE' AND status IN ({act})", (mint, *A.ACTIVE)):
            return await self._refuse(w, "already held in the wallet", 0, now)
        sol_usd = await t.sol_usd()
        if not sol_usd:
            return await self._refuse(w, "no SOL price from GMGN", 0, now)
        wallet = await t.wallet_lamports(fresh=True)
        free = A.strategy_free_lamports(s, c.db, "B", wallet)          # DECISION 71: B's share of the wallet only
        pool_cap = (m["liq"] * s["strat_b.max_pool_pct"] / 100.0) if m.get("liq") else None
        size, lamports, short = A.pick_size(s, free, sol_usd, pool_cap, top_usd=s["strat_b.trade_usd"])
        if size is None:
            return await self._refuse(w, short, 0, now, wallet_kind=True)
        try:
            A.check_caps(s, c.db, c.night, "LIVE", size, lamports, sol_usd, wallet, strategy="B")
        except A.CapError as e:
            return await self._refuse(w, str(e), size, now, wallet_kind=True)
        held = A.mint_held_live(c.db, mint)          # re-check after the awaits above; nothing awaits from here to the row insert
        if held:
            return await self._refuse(w, f"already held by {held} LIVE in the wallet", 0, now)
        import strat_b as B
        ef = B.early_fields(s, now)
        body = b_buy_body(s, mint, lamports, phase=ef["exits_phase"])
        if body["from_address"] != MAIN_WALLET or body["input_token"] != SOL or int(body["input_amount"]) != lamports:
            return await self._refuse(w, "swap body mismatch", size, now)
        if t.halted() or s["strat_b.mode"] != "LIVE":                    # last line of defence before the signed swap
            return await self._refuse(w, "halted or not LIVE at send time", size, now)
        oid = self._row(ts=now, night=c.night, mode="LIVE", mint=mint, name=name, symbol=w.get("symbol"), status="submitting",
                        size_usd=size, lamports=lamports, sol_usd=sol_usd, signal_price=m["price"], entry_price=m["price"],
                        peak=m["price"], last_price=m["price"], last_liq=m["liq"], entry_liq=m["liq"], mc_entry=m["mc"],
                        trail_armed=0, opened_at=now, why=json.dumps(dict(m, reason=why)), last_seen=now,
                        exits_phase=ef["exits_phase"], early_until=ef["early_until"])
        c.db.x("UPDATE strat_b_watch SET status='bought', reason=?, updated=? WHERE mint=?", (f"{LIVE_LABEL}: {why}", now, mint))
        self._retry_buy.pop(mint, None)
        self._busy.add(oid)
        try:
            try:
                r = await t.client.swap(body)
            except Exception as e:
                st = "unknown" if isinstance(e, A.GmgnError) and e.status == 0 else "failed"
                self._upd(oid, status=st, note=str(e)[:300])
                self._log("LIVE swap", st, mint, name, f"#{oid}", str(e)[:200])
                await self._push(f"{LIVE_LABEL} · Buy {name} {st}: {str(e)[:160]}"
                                 + ("\nCheck your GMGN wallet; I won't retry." if st == "unknown" else ""), mint)
                return {"status": st, "id": oid}
            self._upd(oid, status="pending", order_id=(r or {}).get("order_id"), tx=(r or {}).get("hash"),
                      strategy_order_id=(r or {}).get("strategy_order_id"))
            self._log("LIVE BUY sent", mint, name, f"#{oid}", f"${size:.2f}", f"{lamports / LAMPORTS:.4f} SOL",
                      f"signal={m['price']:.6g}", f"mc={m['mc']}", why, (r or {}).get("order_id"))
            await self._push(f"{LIVE_LABEL} · Buying {name} · ${size:,.2f} ({lamports / LAMPORTS:.4f} SOL) · mc ${m['mc']:,.0f}\n"
                             f"{why}\n{B.b_exits_text(s)} (stop is server-side on GMGN)", mint)
            return await self._settle(oid, r)
        finally:
            self._busy.discard(oid)

    async def settle(self, oid, r=None, polls=8):
        if oid in self._busy:
            return {"status": "busy"}
        self._busy.add(oid)
        try:
            return await self._settle(oid, r, polls)
        finally:
            self._busy.discard(oid)

    async def _settle(self, oid, r=None, polls=8):
        t, o = self.t, self._get(oid)
        st = (r or {}).get("status")
        for _ in range(polls):
            if st in ("confirmed", "successful", "failed", "expired"):
                break
            await asyncio.sleep(self.SETTLE_GAP_S)
            try:
                r = await t.client.query_order(o["order_id"])
                st = (r or {}).get("status")
            except Exception as e:
                self._log("LIVE query_order", o["mint"], str(e)[:160])
        if st in ("failed", "expired"):
            self._upd(oid, status="failed", note=(r or {}).get("error_status") or st)
            await self._push(f"{LIVE_LABEL} · Buy {o['name']} {st}: {(r or {}).get('error_status') or ''}".strip(), o["mint"])
            return {"status": "failed", "id": oid}
        if st not in ("confirmed", "successful"):
            return {"status": "pending", "id": oid}
        rep = (r or {}).get("report") or {}
        fill = float(rep.get("price_usd") or 0) or o["entry_price"]
        tx = (r or {}).get("hash") or o["tx"]
        src, tokens = "gmgn", None
        buy = await t.buy_delta({"tx": tx, "mint": o["mint"], "audit": None})
        if buy:
            fill, src = chain.result(buy, [], o["sol_usd"])["entry_price"], "chain"
            tokens = buy["tok"] / 10 ** (buy.get("dec") or 6)
        now = self.c.clock()
        self._upd(oid, status="open", entry_price=fill, entry_src=src, peak=fill, last_price=fill, tx=tx, tokens=tokens,
                  strategy_order_id=(r or {}).get("strategy_order_id") or o["strategy_order_id"], last_seen=now)
        self.sb.hist[o["mint"]] = collections.deque([(now, fill)])
        self.c.event("fill", o["mint"])
        self._log("LIVE BUY filled", o["mint"], o["name"], f"#{oid}", f"entry={fill:.6g}", f"src={src}")
        try:
            filled = int(rep.get("output_amount") or 0) or None
        except (TypeError, ValueError):
            filled = None
        return await self.ensure_stop(oid, filled)

    # ---------- server-side stop (same race handling as A, DECISION 39)
    async def ensure_stop(self, oid, filled=None):
        t, s, o = self.t, self.c.s, self._get(oid)
        mint, held = o["mint"], None
        for i in range(self.ATTACH_POLLS):
            sid = await t.find_strategy(mint)
            if sid:
                self._upd(oid, strategy_order_id=sid)
                self._log("LIVE stop on GMGN", mint, f"#{oid}", sid)
                import strat_b as B
                await self._push(f"{LIVE_LABEL} · Bought {o['name']} · ${o['size_usd']:,.2f} @ ${o['entry_price'] or 0:.6g} · "
                                 f"{B.b_exits_text(s, o, self.c.clock())} (stop live on GMGN)", mint)
                return {"status": "open", "id": oid, "strategy_order_id": sid}
            held = await t.token_balance(mint)
            if i >= self.ATTACH_GRACE_POLLS - 1 and (held is None or held > 0):
                break
            await asyncio.sleep(self.ATTACH_GAP_S)
        if held == 0:
            self._log("LIVE no tokens seen after fill", mint, f"#{oid}", f"filled={filled}")
            await self._push(f"{LIVE_LABEL} · {o['name']}: the buy confirmed but the tokens haven't shown in your wallet yet. "
                             f"I'm not selling anything; I'll keep checking.", mint)
            return {"status": "open", "id": oid, "repair": "waiting"}
        amount = held or filled
        for attempt in range(4):
            try:
                import strat_b as B
                r = await t.client.strategy_create(A.stop_strategy_body(s, mint, o["entry_price"], o["sol_usd"], amount,
                                                                         stop_pct=B.b_stop_pct(s, o, self.c.clock())))
                sid = (r or {}).get("order_id")
                if not sid:
                    raise A.GmgnError(200, error="NO_ORDER_ID", message="strategy create returned no order_id")
                self._upd(oid, strategy_order_id=sid, note="stop_loss strategy created")
                self._log("LIVE stop created", mint, f"#{oid}", sid)
                await self._push(f"{LIVE_LABEL} · Bought {o['name']} · ${o['size_usd']:,.2f} · "
                                 f"{B.b_exits_text(s, o, self.c.clock())} (stop created on GMGN)", mint)
                return {"status": "open", "id": oid, "repair": "strategy"}
            except Exception as e:
                self._log("LIVE stop create failed", mint, str(e)[:200])
                if "amount_in must be greater than zero" not in str(e):
                    break
                await asyncio.sleep(self.ATTACH_GAP_S)
                amount = await t.token_balance(mint) or amount
                if await t.find_strategy(mint):
                    return {"status": "open", "id": oid, "repair": "none needed"}
        res = await self._market_sell(oid, "no server-side stop")
        if res in ("sent", "already"):
            await self._push(f"{LIVE_LABEL} · ALERT {o['name']}: couldn't attach a server-side stop, so I market-sold it.", mint)
            return {"status": "exiting", "id": oid}
        if res == "none_held":
            return {"status": "exiting", "id": oid}
        await self._push(f"{LIVE_LABEL} · ALERT {o['name']}: couldn't attach a stop and the market sell FAILED. "
                         f"Open GMGN and sell it yourself now.", mint)
        return {"status": "open", "id": oid, "repair": "failed"}

    # ---------- adopt a manual wallet buy (DECISION 77)
    async def adopt(self, mint, name, symbol, sigs, *, note=None) -> dict:
        """Adopt a manual wallet buy into B LIVE with B's normal protection (owner command).
        Records the real entry from the buy tx(s), places a GMGN limit_order stop at -strat_b.stop_pct
        for 100% of the tokens held, and marks the row reason='adopted manual buy', audit.adopted=True.
        Counts toward B's live_max_open and wallet budget. Does not change modes, caps, or settings,
        and does not spend new SOL. Refuses if A or B already holds the mint LIVE, or if the wallet
        no longer holds the bought tokens."""
        c, s, t = self.c, self.c.s, self.t
        if t is None or t.client is None:
            return {"status": "refused", "reason": "GMGN client not configured"}
        held = A.mint_held_live(c.db, mint)
        if held:
            return {"status": "refused", "reason": f"already held by {held} LIVE in the wallet"}
        if not sigs:
            return {"status": "refused", "reason": "no buy signature"}
        legs = []
        for sig in sigs:
            d = await chain.tx_delta(c.net, sig, MAIN_WALLET, mint, getattr(t, "_txc", None))
            if not d or d["err"] or d["tok"] <= 0 or d["sol"] >= 0:
                return {"status": "refused", "reason": f"buy tx is not a wallet buy of this mint ({sig[:8]})"}
            legs.append(d)
        legs.sort(key=lambda l: l["ts"] or 0)
        tok, lam = sum(l["tok"] for l in legs), -sum(l["sol"] for l in legs)
        ts = min((l["ts"] or 0) for l in legs)
        dec = legs[0].get("dec") or 6
        bal = await chain.token_balance(c.net, MAIN_WALLET, mint)
        if bal is None:
            return {"status": "refused", "reason": "wallet balance unknown"}
        if bal < tok * 0.99:
            return {"status": "refused", "reason": "wallet no longer holds the bought tokens"}
        sol_usd = await t.sol_usd()
        if not sol_usd:
            return {"status": "refused", "reason": "no SOL price from GMGN"}
        entry = chain.result({"sol": -lam, "tok": tok, "dec": dec}, [], sol_usd)["entry_price"]
        size = round(lam / 1e9 * sol_usd, 2)
        tokens = tok / 10 ** dec
        entry_mc = None
        try:
            r = await c.net.rpc("getTokenSupply", [mint])
            supply_ui = float((r or {}).get("value", {}).get("uiAmount") or 0) or None
            if entry and supply_ui:
                entry_mc = round(entry * supply_ui, 2)
        except Exception:
            pass
        import strat_b as B
        now = c.clock()
        opened = float(ts) or now
        ef = B.early_fields(s, opened)
        if ef["exits_phase"] == "early" and now >= ef["early_until"]:
            ef = {"exits_phase": "normal", "early_until": None}
        stop_pct = float(s["strat_b.early_stop_pct"] if ef["exits_phase"] == "early" else s["strat_b.stop_pct"])
        existing = []
        for tag in ("STMix", "LimitOrder"):
            try:
                d = await t.client.strategy_orders(base_token=mint, kind="open", group_tag=tag)
            except TypeError:
                d = await t.client.strategy_orders(base_token=mint, kind="open")
            existing += [x for x in (d or {}).get("list") or []
                         if (x.get("base_token") or x.get("token_address")) == mint
                         and (x.get("order_id") or x.get("id"))]
        if existing:
            sid = existing[0].get("order_id") or existing[0].get("id")
            repair = "adopted manual buy; existing GMGN strategy"
        else:
            body = A.stop_strategy_body(s, mint, entry, sol_usd, tok, stop_pct=stop_pct)
            try:
                r = await t.client.strategy_create(body)
            except Exception as e:
                self._log("LIVE adopt stop failed", mint, str(e)[:200])
                return {"status": "refused", "reason": f"could not create GMGN stop: {str(e)[:160]}"}
            sid = (r or {}).get("order_id")
            if not sid:
                return {"status": "refused", "reason": "could not create GMGN stop"}
            repair = "adopted manual buy; stop_loss strategy created"
        held = A.mint_held_live(c.db, mint)
        if held:
            return {"status": "refused", "reason": f"already held by {held} LIVE in the wallet"}
        audit = {"adopted": True,
                 "buys": [{"sig": l["sig"], "ts": l["ts"], "sol": l["sol"], "tok": l["tok"]} for l in legs],
                 "tok_raw": tok, "sol_lamports": lam, "phase": ef["exits_phase"], "strategy_kind": "limit_order",
                 "note": note or "adopted manual buy"}
        oid = self._row(ts=opened, night=c.night, mode="LIVE", mint=mint, name=name, symbol=symbol, status="open",
                        reason="adopted manual buy", size_usd=size, sol_usd=sol_usd, lamports=lam,
                        signal_price=entry, entry_price=entry, tokens=tokens, peak=entry, last_price=entry,
                        mc_entry=entry_mc, trail_armed=0, opened_at=opened, last_seen=now, order_id="",
                        strategy_order_id=sid, note=repair, tx=legs[-1]["sig"], entry_src="chain",
                        audit=json.dumps(audit), why=json.dumps({"reason": "adopted manual buy"}),
                        exits_phase=ef["exits_phase"], early_until=ef["early_until"])
        self._log("LIVE adopted", mint, name, f"#{oid}", f"${size:.2f}", f"stop={sid}", repair)
        stub = {"exits_phase": ef["exits_phase"], "early_until": ef["early_until"], "opened_at": opened}
        await self._push(
            f"{LIVE_LABEL} · {symbol}: your manual buy is now managed by Strategy B (#{oid}) · "
            f"${size:,.2f} ({lam / LAMPORTS:.4f} SOL) · server-side -{stop_pct:g}% stop created on GMGN · "
            f"{B.b_exits_text(s, stub, now)}",
            mint)
        return {"status": "open", "id": oid, "strategy_order_id": sid, "entry_price": entry,
                "size_usd": size, "lamports": lam, "tokens": tokens, "mc_entry": entry_mc,
                "check_price": entry * (1 - stop_pct / 100.0) if entry else None, "note": repair}

    # ---------- sells
    async def _market_sell(self, oid, reason) -> str:
        """Guarded market sell: never a 2nd sell while one is pending/done, never with 0 held. sent|already|none_held|failed|unknown."""
        t, o = self.t, self._get(oid)
        if o["sell_order_id"]:
            try:
                st = ((await t.client.query_order(o["sell_order_id"])) or {}).get("status")
            except Exception:
                st = "unknown"
            if st not in ("failed", "expired"):
                return "already"
        held = await t.token_balance(o["mint"])
        if held is None:
            return "unknown"
        if held == 0:
            self._upd(oid, status="exiting", exit_reason=o["exit_reason"] or reason)
            self._log("LIVE sell skipped", o["mint"], f"#{oid}", "0 tokens held (GMGN stop already sold?)")
            return "none_held"
        try:
            r = await t.client.swap(A.sell_all_body(self.c.s, o["mint"]))
        except Exception as e:
            self._upd(oid, note=((o["note"] or "") + f" sell failed: {str(e)[:120]}")[-500:])
            self._log("LIVE sell failed", o["mint"], f"#{oid}", reason, str(e)[:200])
            return "failed"
        self._upd(oid, status="exiting", exit_reason=reason, sell_order_id=(r or {}).get("order_id"))
        self._log("LIVE SELL sent", o["mint"], o["name"], f"#{oid}", reason, (r or {}).get("order_id"))
        return "sent"

    async def sell(self, o, reason, price, now):
        oid = o["id"]
        if oid in self._busy or now < self._retry_sell.get(oid, 0):
            return None
        self._busy.add(oid)
        try:
            o = self._get(oid)
            if not o or o["status"] != "open" or o["sell_order_id"]:
                return None
            res = await self._market_sell(oid, reason)
            if res in ("failed", "unknown"):
                self._retry_sell[oid] = now + SELL_RETRY_S
                if res == "failed" and "alerted" not in (o["note"] or ""):
                    self._upd(oid, note=((self._get(oid)["note"] or "") + " alerted")[-500:])
                    await self._push(f"{LIVE_LABEL} · ALERT {o['name']}: {reason} hit, but the market sell failed. "
                                     f"The GMGN stop is still on. I'll retry.", o["mint"])
                return res
            if res == "sent":
                await self.t._cancel_mint_strategies({"mint": o["mint"], "strategy_order_id": o["strategy_order_id"],
                                                      "repair": o["note"]})
                e = o["entry_price"] or price
                await self._push(f"{LIVE_LABEL} · {o['name']}: {reason} at {100 * (price / e - 1):+.0f}% from entry, market-selling", o["mint"])
            self._retry_sell.pop(oid, None)
            return res
        finally:
            self._busy.discard(oid)

    def _b_strategy_type(self, o) -> str:
        note = o.get("note") or ""
        return "limit_order" if "stop_loss strategy created" in note else "smart_trade"

    async def promote_early_stop(self, oid):
        """Replace the first-window -early_stop_pct GMGN stop with the normal -stop_pct stop.
        Create-new then cancel-old so there is never a gap with no server-side stop (same as A's grace swap)."""
        import strat_b as B
        if oid in self._busy:
            return {"status": "busy"}
        self._busy.add(oid)
        try:
            return await self._promote_early_stop(oid)
        finally:
            self._busy.discard(oid)

    async def _promote_early_stop(self, oid):
        import strat_b as B
        o = self._get(oid)
        if not o or o["status"] != "open" or not B.early_window_ended(self.c.s, o, self.c.clock()):
            return {"status": o["status"] if o else "missing"}
        s, t = self.c.s, self.t
        if t is None or t.client is None:
            return {"status": "open", "promote": "no client"}
        old_sid, old_type = o.get("strategy_order_id"), self._b_strategy_type(o)
        amount = await t.token_balance(o["mint"])
        if amount == 0:
            return {"status": "open", "promote": "none_held"}
        sol_usd = o["sol_usd"] or await t.sol_usd()
        new_sid = None
        created_first = False
        try:
            body = A.stop_strategy_body(s, o["mint"], o["entry_price"], sol_usd, amount or None,
                                        stop_pct=s["strat_b.stop_pct"])
            new_sid = await t._create_strategy(body)
            created_first = True
        except Exception as e:
            self._log("LIVE early-stop promote create-first failed", o["mint"], str(e)[:200])

        if created_first:
            cancelled = await t._cancel_strategy(old_sid, old_type)
            note = "early_stop_swap" if cancelled else "early_stop_swap: new stop on, old cancel failed"
            self._upd(oid, strategy_order_id=new_sid, exits_phase="normal",
                      note=((o.get("note") or "") + (" " + note if o.get("note") else note))[-500:])
            if cancelled:
                await self._push(f"{LIVE_LABEL} · {o['name']}: first {s['strat_b.early_stop_min']:g} min over · "
                                 f"stop tightened to -{s['strat_b.stop_pct']:g}% on GMGN", o["mint"])
                return {"status": "open", "promote": "ok"}
            await self._push(f"{LIVE_LABEL} · ALERT {o['name']}: the -{s['strat_b.stop_pct']:g}% stop is on, "
                             f"but I couldn't cancel the old -{s['strat_b.early_stop_pct']:g}% early stop. Both are live.",
                             o["mint"])
            return {"status": "open", "promote": "new_on_old_cancel_failed"}

        cancelled = False
        for _ in range(getattr(t, "SWAP_TRIES", 3)):
            if await t._cancel_strategy(old_sid, old_type):
                cancelled = True
                break
            await asyncio.sleep(getattr(t, "SWAP_GAP_S", 2))
        if not cancelled:
            if "early_stop_swap: cancel_failed" not in (o.get("note") or ""):
                self._upd(oid, note=((o.get("note") or "") + " early_stop_swap: cancel_failed")[-500:])
                await self._push(f"{LIVE_LABEL} · ALERT {o['name']}: the early-stop window ended, but I couldn't switch "
                                 f"to the -{s['strat_b.stop_pct']:g}% stop. The -{s['strat_b.early_stop_pct']:g}% stop "
                                 f"is still on GMGN. I'll keep trying. The desk will sell at -{s['strat_b.stop_pct']:g}% while I'm awake.",
                                 o["mint"])
            return {"status": "open", "promote": "cancel_failed"}

        for _ in range(getattr(t, "SWAP_TRIES", 3)):
            try:
                body = A.stop_strategy_body(s, o["mint"], o["entry_price"], sol_usd, amount or None,
                                            stop_pct=s["strat_b.stop_pct"])
                new_sid = await t._create_strategy(body)
                self._upd(oid, strategy_order_id=new_sid, exits_phase="normal",
                          note=((o.get("note") or "") + " early_stop_swap: cancel-then-create")[-500:])
                await self._push(f"{LIVE_LABEL} · {o['name']}: first {s['strat_b.early_stop_min']:g} min over · "
                                 f"stop tightened to -{s['strat_b.stop_pct']:g}% on GMGN", o["mint"])
                return {"status": "open", "promote": "cancel_then_create"}
            except Exception as e:
                self._log("LIVE early-stop promote create after cancel failed", o["mint"], str(e)[:200])
                await asyncio.sleep(getattr(t, "SWAP_GAP_S", 2))

        res = await self._market_sell(oid, "no server-side stop after early-stop swap")
        if res in ("sent", "already"):
            await self._push(f"{LIVE_LABEL} · ALERT {o['name']}: the early stop was cancelled and I couldn't attach "
                             f"the -{s['strat_b.stop_pct']:g}% stop, so I market-sold it.", o["mint"])
            return {"status": "exiting", "promote": "market_sell"}
        if res == "none_held":
            return {"status": "exiting", "promote": "none_held"}
        await self._push(f"{LIVE_LABEL} · ALERT {o['name']}: the early stop was cancelled and I couldn't attach a new "
                         f"stop or market-sell. Open GMGN and sell it yourself now.", o["mint"])
        return {"status": "open", "promote": "failed"}

    # ---------- exits on the fast feed
    async def manage(self, o, price, liq, now):
        from strat_b import exit_check, TRAIL, b_trail_settings
        s = self.c.s
        if o["id"] in self._busy:
            return None
        hist = self.sb._sample(o["mint"], now, price)
        below = o["id"] in self._trail_below_floor or o["id"] in self.sb._trail_below_floor
        reason, upd = exit_check(s, o, price, liq, hist, now, below_floor=below)
        self.c.db.x("UPDATE strat_b_orders SET peak=?, trail_armed=?, last_price=?, last_liq=COALESCE(?, last_liq), last_seen=? WHERE id=?",
                    (upd["peak"], upd["trail_armed"], price, liq, now, o["id"]))
        if upd.get("below_floor"):
            self._trail_below_floor.add(o["id"])
            self.sb._trail_below_floor.add(o["id"])
        if upd["trail_armed"] and not o["trail_armed"]:
            self._log("trail armed", o["mint"], o["name"], f"#{o['id']}", f"peak={upd['peak']:.6g}")
        if reason == TRAIL and self.t is not None:
            qsol = await self.t._trail_sell_quote_sol(o)
            if qsol is not None and not A.trail_net_ok(b_trail_settings(s), o, price, quote_sol_out=qsol):
                self._trail_below_floor.add(o["id"])
                self.sb._trail_below_floor.add(o["id"])
                self._log("trail held under net floor (quote)", o["mint"], o["name"], f"#{o['id']}",
                          f"quote_sol={qsol:.6g}")
                return None
        if reason:
            self._trail_below_floor.discard(o["id"])
            self.sb._trail_below_floor.discard(o["id"])
            return await self.sell(dict(o, **upd), reason, price, now)
        if o["id"] in self._trail_below_floor and not upd.get("below_floor"):
            self._trail_below_floor.discard(o["id"])
            self.sb._trail_below_floor.discard(o["id"])
        return None

    def _rows(self, statuses):
        return self.c.db.rows(f"SELECT * FROM strat_b_orders WHERE mode='LIVE' AND status IN ({','.join('?' * len(statuses))})", tuple(statuses))

    async def once(self, pairs=None, now=None) -> str:
        t = self.t
        if t is None or t.client is None:
            return "no GMGN client"
        now = self.c.clock() if now is None else now
        import strat_b as B
        rows = [o for o in self._rows(("open",)) if o["entry_price"]]
        for o in list(rows):
            if B.early_window_ended(self.c.s, o, now) and o["id"] not in self._busy:
                await self.promote_early_stop(o["id"])
        rows = [o for o in self._rows(("open",)) if o["entry_price"]]
        if rows:
            px = await t.exit_prices(rows, pairs)
            for o in rows:
                if o["id"] in px:
                    await self.manage(o, px[o["id"]][0], None, now)
        if now - self._last_close >= CLOSE_CHECK_S:
            self._last_close = now
            for o in self._rows(("pending",)):
                await self.settle(o["id"], {"status": None}, polls=1)
            for o in self._rows(("open", "exiting")):
                if o["id"] not in self._busy:
                    await self.check_closed(o, now)
        return f"open={len(rows)}"

    async def loop(self):
        while True:
            t0 = time.time()
            try:
                note = await self.once()
            except Exception as e:
                note = f"error {type(e).__name__}"
                self.c.error("strat_b", "live", str(e)[:300])
            self.c.hb("strat_b_live", f"{self.c.s['strat_b.mode']} {note}")
            f = getattr(self.t, "fast", None) if self.t is not None else None
            poll = f.poll_s() if (f is not None and f.enabled()) else A.TRAIL_POLL_S
            await asyncio.sleep(max(0.5, poll - (time.time() - t0)))

    # ---------- close from the chain only (DECISION 42)
    async def check_closed(self, o, now) -> bool:
        t, c = self.t, self.c
        buy = await t.buy_delta({"tx": o["tx"], "mint": o["mint"], "audit": o.get("audit")})
        if not buy:
            return False
        if o["entry_src"] != "chain":
            e = chain.result(buy, [], o["sol_usd"])["entry_price"]
            self._upd(o["id"], entry_price=e, entry_src="chain", tokens=buy["tok"] / 10 ** (buy.get("dec") or 6))
            o = {**o, "entry_price": e, "entry_src": "chain"}
        bal = await chain.token_balance(c.net, MAIN_WALLET, o["mint"])
        if bal is None or bal > max(1, buy["tok"] // A.DUST_DIV):
            return False
        try:
            sells = await chain.wallet_sells(c.net, MAIN_WALLET, o["mint"], buy["ts"], skip={o["tx"]}, cache=t._txc)
        except Exception as e:
            self._log("LIVE wallet sigs", o["mint"], str(e)[:160]); sells = []
        sold = -sum(x["tok"] for x in sells)
        if sells and sold >= (buy["tok"] - bal) * 0.99:
            r = chain.result(buy, sells, o["sol_usd"])
            reason = o["exit_reason"]
            if not reason:
                srv = await t.strategy_exit({"mint": o["mint"], "strategy_order_id": o["strategy_order_id"]})
                reason = "stop (GMGN server-side)" if srv in (A.LIVE_EXIT["loss_stop"], None) else ("sold by hand" if srv == "manual" else srv)
            cur = c.db.x("UPDATE strat_b_orders SET status='closed', closed_at=?, exit_reason=?, exit_signal_price=?, exit_price=?, "
                         "pnl_usd=?, pct=?, sell_tx=? WHERE id=? AND status IN ('open','exiting')",
                         (r["sell_ts"] or now, reason, o["last_price"], r["exit_price"], r["pnl_usd"], r["pct"], r["sell_tx"], o["id"]))
            if not cur.rowcount:
                return True
            self.sb.hist.pop(o["mint"], None)
            held = (r["sell_ts"] or now) - o["opened_at"]
            held_txt = f"{held / 3600:.1f}h" if held >= 5400 else f"{held / 60:.0f}m"
            sign = "+" if r["pnl_usd"] >= 0 else "-"
            self._log("LIVE SELL closed", o["mint"], o["name"], f"#{o['id']}", reason, r["sell_tx"], f"sol_in={r['sol_in']:.9f}",
                      f"sol_out={r['sol_out']:.9f}", f"pnl=${r['pnl_usd']:+.2f}", f"{(r['pct'] or 0):+.1f}%", f"held={held_txt}")
            await self._push(f"{LIVE_LABEL} · Sold {o['name']} ({reason}) · {sign}${abs(r['pnl_usd']):,.2f} ({(r['pct'] or 0):+.1f}%) · held {held_txt}\n"
                             f"On-chain: {r['sol_in']:.4f} SOL in → {r['sol_out']:.4f} SOL out, fees included", o["mint"])
            c.event("exit", o["mint"])
            return True
        if not o["confirm_push_at"]:
            self._upd(o["id"], status="exiting", confirm_push_at=now)
            self._log("LIVE sold, confirming", o["mint"], f"#{o['id']}", f"bal={bal}")
        return True
