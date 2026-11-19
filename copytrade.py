"""Strategy A copy trading (DECISION 92, owner 2026-11-19 4:55 PM CT, t456u): watch one Solana wallet and copy its
new buys and sells into A's PAPER book. Read-only: public Solana RPC (desk.rpc_url) and Jupiter quotes. It never
imports a signing path, never calls GMGN swap / strategy routes, and never touches the real wallet.

Detection: getSignaturesForAddress on the copied wallet every copy.poll_s (3 s), then getTransaction for each new
signature. A trade is read from the wallet's own balance changes in that tx (native SOL + WSOL vs one token):
  buy  = token up and SOL down by at least copy.min_sol;  his fraction = SOL spent / his SOL before the tx
  sell = token down and SOL up;                           his fraction = tokens sold / his tokens before the tx
Anything else (reward claims, transfers, account rent) is ignored. Only signatures newer than the first poll after
the wallet was set are copied, so his existing bags are never bought.

Copy rules (PAPER only):
  buy:  copy.sizing flat (default, owner 5:17 PM CT): copy.flat_usd $20 per copied coin, and with
        copy.first_buy_only only his first buy of a coin is copied (add-ons, coins he already held, coins already
        copied are ignored). copy.sizing fraction: clamp(fraction x book, copy.min_usd, copy.max_usd), book =
        copy.book_usd + realized copy P&L, and a repeat buy adds to the copy up to copy.max_position_usd.
        Jupiter can-it-sell check first (no route = skip). Market-cap / score filters are NOT applied.
        At most copy.max_open copy positions; the paper bankroll must cover the size.
  sell: sell the same fraction of our copy (a closed child row for a partial; the whole row at >= 98%).
  exit: only his sells, plus the copy.backstop_pct (-50%) backstop stop in GmgnTrader._paper_tick. No trail, no
        surge, no timeout, no grace on copy rows (a_kind='copy', no_surge=1)."""
from __future__ import annotations
import asyncio, json, time
from ctx import iso

SOL = "So11111111111111111111111111111111111111112"
USDC = "EPjFWdd5AufqSSqeM2qMEw7NVwQ8MqXHxvd1bUfHL1QZ"
TX_OPTS = {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 1, "commitment": "confirmed"}
META = "copy_watch"
LAMPORTS = 1_000_000_000
MAX_TX_TRIES = 6
ACTIVE = ("submitting", "pending", "open", "exiting")


def quote(*a, **k):
    import jupiter
    return jupiter.quote(*a, **k)


SELLS_H1_MIN = 20                # DexScreener fallback: real sells in the last hour ...
LIQ_MIN_USD = 20000.0            # ... on a pool at least this deep
CONFIRM_GAP_S = 3.0              # LIVE copy: query_order poll gap after a swap
B_KEEP_USD = 20.0                # DECISION 93: when B has nothing open, A borrowing leaves one B $20 buy + fees


async def _dex_sells_ok(net, mint):
    """Fallback when Jupiter doesn't list the token at all (TOKEN_NOT_TRADABLE: e.g. pump AMM Token-2022 coins with a
    transfer tax, which himOthy trades). Sellable if DexScreener shows >= 20 sells in the last hour on a pool with
    >= $20K liquidity. Never used when Jupiter lists the token but finds no sell route (that still skips)."""
    if net is None:
        return None
    try:
        from agents.scanner import pick_pair
        import fields
        p = pick_pair(await net.dex_tokens([mint]), mint)
    except Exception:
        return None
    if not p:
        return None
    sells = int(fields.get(p, "txns.h1.sells") or 0)
    liq = float(fields.get(p, "liquidity.usd") or 0)
    dex = fields.get(p, "dexId") or "?"
    ok = sells >= SELLS_H1_MIN and liq >= LIQ_MIN_USD
    return ok, f"{sells} sells/1h on {dex}, ${liq:,.0f} liquidity"


async def sell_check(s, mint, size_usd, net=None) -> dict:
    """Jupiter round trip for the planned size. {'ok', 'skip', 'why'} like strat_b_research.sell_check, but keyed
    on copy.sell_check so A's check never depends on a Strategy B setting."""
    if not s["copy.sell_check"]:
        return {"ok": True, "skip": False, "why": "sell check off"}
    b = await quote(USDC, mint, int(round(float(size_usd) * 1e6)))
    if not b.get("ok") and "NOT_TRADABLE" in str(b.get("error") or "").upper():
        d = await _dex_sells_ok(net, mint)
        if d is not None:
            if d[0]:
                return {"ok": True, "skip": False, "why": f"Jupiter doesn't list it; DexScreener shows real sells ({d[1]})"}
            return {"ok": False, "skip": True, "why": f"Jupiter doesn't list it and too few real sells ({d[1]})"}
    if not b.get("ok"):
        if b.get("no_route"):
            return {"ok": False, "skip": True, "why": f"no Jupiter buy route ({b.get('error')})"}
        return {"ok": False, "skip": False, "why": f"can't check sellability now ({b.get('error')})"}
    q = await quote(mint, SOL, int(b["out"]))
    if not q.get("ok"):
        if q.get("no_route"):
            return {"ok": False, "skip": True, "why": f"unsellable: no Jupiter sell route for ${float(size_usd):g} ({q.get('error')})"}
        return {"ok": False, "skip": False, "why": f"can't check sellability now ({q.get('error')})"}
    return {"ok": True, "skip": False, "why": f"Jupiter sell route ok ({q.get('route')}, impact {q.get('impact_pct', 0):.1f}%)"}


def classify(t: dict | None, wallet: str, min_sol: float = 0.01) -> dict | None:
    """One parsed tx -> {'side': 'buy'|'sell', 'mint', 'sol' (lamports moved, +), 'pre_sol', 'tok' (raw, +),
    'pre_tok', 'post_tok', 'dec', 'frac', 'ts', 'sig'} or None (not a trade / failed / not his)."""
    if not t or not t.get("meta") or t["meta"].get("err"):
        return None
    m = t["meta"]
    keys = [k["pubkey"] if isinstance(k, dict) else k for k in t["transaction"]["message"]["accountKeys"]]
    if wallet not in keys:
        return None
    i = keys.index(wallet)
    pre_sol, sol = int(m["preBalances"][i]), int(m["postBalances"][i]) - int(m["preBalances"][i])
    toks: dict = {}
    for side, arr in (("pre", m.get("preTokenBalances") or []), ("post", m.get("postTokenBalances") or [])):
        for b in arr:
            if b.get("owner") != wallet:
                continue
            e = toks.setdefault(b.get("mint"), {"pre": 0, "post": 0, "dec": 0})
            e[side] += int(b["uiTokenAmount"]["amount"])
            e["dec"] = int(b["uiTokenAmount"].get("decimals") or 0)
    w = toks.pop(SOL, None)                              # WSOL counts as SOL
    if w:
        pre_sol += w["pre"]; sol += w["post"] - w["pre"]
    moved = [(mt, e) for mt, e in toks.items() if e["post"] != e["pre"]]
    if not moved:
        return None
    mint, e = max(moved, key=lambda x: abs(x[1]["post"] - x[1]["pre"]) / (10 ** x[1]["dec"]))
    d = e["post"] - e["pre"]
    floor = int(float(min_sol) * LAMPORTS)
    base = {"mint": mint, "pre_sol": pre_sol, "pre_tok": e["pre"], "post_tok": e["post"], "dec": e["dec"],
            "ts": t.get("blockTime"), "sig": (t["transaction"].get("signatures") or [None])[0]}
    if d > 0 and -sol >= floor:
        return {**base, "side": "buy", "sol": -sol, "tok": d, "frac": (-sol / pre_sol) if pre_sol > 0 else 1.0}
    if d < 0 and sol > 0:
        return {**base, "side": "sell", "sol": sol, "tok": -d, "frac": (-d / e["pre"]) if e["pre"] > 0 else 1.0}
    return None


def copy_size(s, frac: float, book: float) -> tuple[float, float]:
    if s["copy.sizing"] == "flat":
        v = round(float(s["copy.flat_usd"]), 2)
        return v, v
    raw = max(0.0, float(frac)) * float(book)
    return raw, round(min(max(raw, float(s["copy.min_usd"])), float(s["copy.max_usd"])), 2)


def active(s) -> bool:
    return s["gmgn.profile"] in ("copy", "hybrid") and bool(s["copy.wallet"])


def label(s, mode=None) -> str:
    return f"{mode or s['gmgn.mode']} · copying {s['copy.name'] or (s['copy.wallet'] or '')[:6]}"


def open_copies(db) -> list:
    ph = ",".join("?" * len(ACTIVE))
    return db.rows(f"SELECT * FROM gmgn_orders WHERE a_kind='copy' AND status IN ({ph}) ORDER BY id", ACTIVE)


def realized_copy_pnl(db, since: float) -> float:
    r = db.one("SELECT COALESCE(SUM(pnl_usd),0) p FROM gmgn_orders WHERE mode='PAPER' AND a_kind='copy' "
               "AND status='closed' AND ts>=?", (since or 0,))
    return float(r["p"] or 0)


def status(c) -> dict:
    """For the app / heartbeat: profile, normal vs full copy, last poll and delays."""
    s = c.s
    st = c.db.meta(META) or {}
    if not isinstance(st, dict):
        st = {}
    n = len(open_copies(c.db))
    on = active(s)
    mode = "full copy" if (on and n) else "normal"
    since = st.get("since") or 0
    return {"profile": s["gmgn.profile"], "on": on, "state": mode, "name": s["copy.name"], "wallet": s["copy.wallet"],
            "label": label(s), "openCopies": n, "maxOpen": int(s["copy.max_open"]),
            "smallUsd": float(s["gmgn.small_trade_usd"]), "smallMaxOpen": int(s["gmgn.small_max_open"]),
            "bookUsd": round(float(s["copy.book_usd"]) + realized_copy_pnl(c.db, since), 2),
            "since": iso(since) if since else None, "lastPoll": iso(st["last_poll"]) if st.get("last_poll") else None,
            "lastDelayS": st.get("last_delay_s"), "seenTrades": int(st.get("trades") or 0),
            "mode": s["gmgn.mode"], "paperOnly": s["gmgn.mode"] != "LIVE", "everyN": int(s["copy.every_n"]),
            "maxPositionUsd": float(s["copy.max_position_usd"]), "flatUsd": float(s["copy.flat_usd"]),
            "hisBuys": int(st.get("buys") or 0), "everyNPer": "coin", "smallLive": False}


class CopyWatcher:
    name = "gmgn_copy"

    def __init__(self, ctx, trader):
        self.c, self.t = ctx, trader
        self.log = ctx.logs.get("gmgn") or ctx.logs["executor"]
        self._tries: dict = {}
        self._state_logged = None
        self._sell_retry: dict = {}       # row id -> (bps, reason, next_ts, tries) for LIVE copy sells that failed
        trader.copy_hook = self.live_tick

    @property
    def s(self):
        return self.c.s

    def _log(self, *parts):
        import secrets_store
        self.log.info(secrets_store.redact("\t".join([iso(), "COPY"] + [str(p) for p in parts])))

    def state(self) -> dict:
        st = self.c.db.meta(META)
        return st if isinstance(st, dict) else {}

    def save(self, st):
        self.c.db.set_meta(META, st)

    async def loop(self):
        while True:
            t0 = time.time()
            try:
                note = await self.poll_once()
                self.c.hb("gmgn_copy", note)
            except Exception as e:
                self.c.error("gmgn", "copy", str(e)[:300])
                self.c.hb("gmgn_copy", f"error {type(e).__name__}")
            await asyncio.sleep(max(0.5, float(self.s["copy.poll_s"]) - (time.time() - t0)))

    def _announce_state(self):
        n = len(open_copies(self.c.db))
        mode = "full copy" if n else "normal"
        if mode != self._state_logged:
            self._state_logged = mode
            self._log("A mode", mode, f"open copies={n}", f"profile={self.s['gmgn.profile']}", f"target={self.s['copy.name']}")
        return mode, n

    async def poll_once(self) -> str:
        s, c = self.s, self.c
        if not active(s):
            return f"idle profile={s['gmgn.profile']}"
        wallet = s["copy.wallet"]
        st = self.state()
        sigs = await c.net.rpc("getSignaturesForAddress", [wallet, {"limit": 25, "commitment": "confirmed"}])
        sigs = [x for x in (sigs or []) if x.get("signature")]
        now = c.clock()
        if st.get("wallet") != wallet:                   # first poll for this wallet: baseline, copy nothing old
            st = {"wallet": wallet, "since": now, "last_sig": sigs[0]["signature"] if sigs else None,
                  "last_poll": now, "trades": 0}
            self.save(st)
            self._log("start", f"wallet={wallet}", f"name={s['copy.name']}", f"baseline sig={(st['last_sig'] or '')[:12]}",
                      "existing positions are not copied")
            return "baseline"
        new = []
        for x in sigs:                                   # newest first
            if x["signature"] == st.get("last_sig"):
                break
            new.append(x)
        else:
            if st.get("last_sig") and sigs:
                self._log("gap", f"last seen sig not in the newest {len(sigs)}: processing those")
        for x in reversed(new):                          # oldest first
            sig = x["signature"]
            if not x.get("err") and (x.get("blockTime") or now) >= (st.get("since") or 0) - 5:
                t = await c.net.rpc("getTransaction", [sig, TX_OPTS])
                if t is None:
                    n = self._tries[sig] = self._tries.get(sig, 0) + 1
                    if n < MAX_TX_TRIES:
                        break                            # not indexed yet: retry next poll, keep order
                    self._log("tx unreadable, skipped", sig)
                else:
                    ev = classify(t, wallet, float(s["copy.min_sol"]))
                    if ev:
                        ev["detected"] = c.clock()
                        ev["delay_s"] = round(ev["detected"] - (ev["ts"] or ev["detected"]), 1)
                        st["last_delay_s"] = ev["delay_s"]
                        st["trades"] = int(st.get("trades") or 0) + 1
                        if ev["side"] == "buy":                  # DECISION 94: every-other counts his buys PER COIN
                            st["buys"] = int(st.get("buys") or 0) + 1
                            mb = st.setdefault("mint_buys", {})
                            mb[ev["mint"]] = ev["nth"] = int(mb.get(ev["mint"]) or 0) + 1
                            if len(mb) > 500:                    # bounded: drop the oldest coins
                                for k in list(mb)[:len(mb) - 500]:
                                    mb.pop(k, None)
                        self.save({**st, "last_sig": sig, "last_poll": now})
                        try:
                            if ev["side"] == "buy":
                                await self.copy_buy(ev)
                            else:
                                await self.copy_sell(ev)
                        except Exception as e:
                            c.error("gmgn", "copy trade", f"{sig[:12]} {str(e)[:250]}")
            self._tries.pop(sig, None)
            st["last_sig"] = sig
        st["last_poll"] = now
        self.save(st)
        mode, n = self._announce_state()
        return f"{mode} copies={n} target={s['copy.name'] or wallet[:6]} delay={st.get('last_delay_s')}"

    # ---- prices
    async def _price(self, mint, ev, sol_usd):
        fr = await self.t._fresh(mint)
        his = None
        if ev["tok"] and sol_usd:
            his = (ev["sol"] / LAMPORTS * sol_usd) / (ev["tok"] / 10 ** ev["dec"])
        if fr and fr.get("price"):
            return fr["price"], fr.get("liq"), fr.get("name"), fr.get("symbol"), "dex", his
        return his, None, None, None, "his fill", his

    def _src(self, ev, **kw):
        return {"wallet": self.s["copy.wallet"], "name": self.s["copy.name"], "sig": ev["sig"], "block_ts": ev["ts"],
                "detected_ts": ev["detected"], "delay_s": ev["delay_s"], "his_sol": round(ev["sol"] / LAMPORTS, 6),
                "his_pre_sol": round(ev["pre_sol"] / LAMPORTS, 6), "his_frac": round(ev["frac"], 6), **kw}

    async def _sol_px(self):
        """SOL/USD without a GMGN call when possible: the trader's cached price (< 10 min), else DexScreener's SOL
        pair, else GMGN gas_price."""
        ts, v = self.t._sol
        if v and time.time() - ts < 600:
            return float(v)
        try:
            from agents.scanner import pick_pair
            import fields
            px = pick_pair(await self.c.net.dex_tokens([SOL]), SOL)
            px = float(fields.get(px, "priceUsd") or 0) if px else 0
            if 20 < px < 5000:
                return px
        except Exception:
            pass
        return await self.t.sol_usd() or v

    async def _wallet_rpc(self):
        """Main wallet SOL balance from the public RPC (no GMGN call). None = unknown."""
        import agents.gmgn_trader as A
        try:
            r = await self.c.net.rpc("getBalance", [A.MAIN_WALLET, {"commitment": "confirmed"}])
        except Exception:
            return None
        if isinstance(r, dict):
            r = r.get("value")
        return int(r) if r is not None else None

    def live_budget(self, wallet, need, sol_usd) -> dict:
        """DECISION 93 (owner 5:23 PM CT 'if it has to it can draw from b'): A's own 50% share first; if that can't
        cover the copy buy, borrow B's unused share = the wallet's free SOL (after the reserve and every in-flight buy;
        B's open-position value is in tokens, never in free SOL) minus one B $20 buy + fees when B has nothing open.
        Lamports: {'ok', 'own', 'free', 'b_keep', 'borrow', 'why'}."""
        import agents.gmgn_trader as A
        s, db = self.s, self.c.db
        b = A.budget_lamports(s, db, "A", wallet)
        if b is None:
            return {"ok": False, "why": "wallet balance unknown", "borrow": 0}
        own, free = max(0, b["avail"]), max(0, b["free"])
        if own >= need:
            return {"ok": True, "own": own, "free": free, "b_keep": 0, "borrow": 0, "why": "A share"}
        b_open = A.open_count(db, "LIVE", "B")
        b_keep = 0 if b_open else A.buy_cost_lamports(s, int(B_KEEP_USD / sol_usd * LAMPORTS))
        if free - b_keep >= need:
            return {"ok": True, "own": own, "free": free, "b_keep": b_keep, "borrow": need - own,
                    "why": "borrowed from B's unused share"}
        return {"ok": False, "own": own, "free": free, "b_keep": b_keep, "borrow": 0,
                "why": (f"insufficient balance: A share {own / LAMPORTS:.4f} SOL, wallet free {free / LAMPORTS:.4f} SOL"
                        f" (keeping {b_keep / LAMPORTS:.4f} SOL for one B buy), need {need / LAMPORTS:.4f} SOL")}

    async def copy_buy(self, ev):
        c, s, t = self.c, self.s, self.t
        mint, st = ev["mint"], self.state()
        held = c.db.one(f"SELECT * FROM gmgn_orders WHERE mint=? AND status IN ({','.join('?' * len(ACTIVE))}) ORDER BY id DESC LIMIT 1",
                        (mint, *ACTIVE))
        mode = held["mode"] if held and held.get("a_kind") == "copy" else s["gmgn.mode"]
        lab = label(s, mode)
        n_every, nth = int(s["copy.every_n"]), int(ev.get("nth") or 1)
        if n_every > 1 and (nth - 1) % n_every:          # owner 5:19/5:40 PM CT: every other of his buys IN THE SAME COIN
            return self._log("buy ignored", mint, f"his buy #{nth} of this coin: copying every {n_every} per coin", f"tx={ev['sig']}")
        sol_usd = await (self._sol_px() if mode == "LIVE" else t.sol_usd())
        price, liq, name, symbol, src, his_px = await self._price(mint, ev, sol_usd)
        name = name or mint[:6]
        book = max(float(s["copy.min_usd"]), float(s["copy.book_usd"]) + realized_copy_pnl(c.db, st.get("since") or 0))
        raw, size = copy_size(s, ev["frac"], book)
        info = dict(book=round(book, 2), raw_usd=round(raw, 2), size_usd=size, price=price, price_src=src, his_px=his_px, nth=nth)
        self._log("his buy", name, mint, f"#{nth}", f"tx={ev['sig']}", f"his {ev['sol'] / LAMPORTS:.4f} SOL of {ev['pre_sol'] / LAMPORTS:.4f}",
                  f"frac={ev['frac']:.4f}", f"size=${size:,.2f}", f"mode={mode}", f"delay={ev['delay_s']}s")

        def refuse(why):
            t._row(ts=c.clock(), night=c.night, mode=mode, mint=mint, name=name, symbol=symbol, status="refused",
                   reason=f"copy: {why}", size_usd=size, a_kind="copy", copy_src=json.dumps(self._src(ev, **info)))
            self._log("copy skipped", mode, name, mint, why)
            return {"status": "refused", "reason": why}

        if not price or not sol_usd:
            return refuse("no price")
        if s["copy.first_buy_only"]:                    # owner 5:17 PM CT (off since 5:19): only his first buy of each coin
            why = None
            if ev.get("pre_tok"):
                why = "not his first buy (he already held it)"
            elif held and held.get("a_kind") == "copy":
                why = f"add-on buy: copy #{held['id']} already open"
            elif c.db.one("SELECT id FROM gmgn_orders WHERE mint=? AND a_kind='copy' AND status<>'refused' AND ts>=?",
                          (mint, st.get("since") or 0)):
                why = "already copied this coin once"
            if why:
                return self._log("buy ignored", name, mint, why)
        if held and held.get("a_kind") != "copy":
            return refuse(f"A already holds it ({held.get('a_kind') or held['mode']} #{held['id']})")
        if held:                                         # he adds: add to our copy up to the per-coin cap
            room = float(s["copy.max_position_usd"]) - float(held["size_usd"] or 0) - _pending_usd(held)
            add = round(min(size, room), 2)
            if add <= 0 or add < min(float(s["copy.min_usd"]), size) - 1e-9:
                return self._log("add skipped", name, f"position ${held['size_usd']:,.2f} at the ${s['copy.max_position_usd']:,.2f} per-coin cap")
        else:
            n = len(open_copies(c.db))
            if n >= int(s["copy.max_open"]):
                return refuse(f"{n}/{int(s['copy.max_open'])} copy coins already open")
            add = size
        if mode == "LIVE":
            return await self.live_buy(ev, mint, name, symbol, price, liq, add, held, info, sol_usd, refuse)
        import agents.gmgn_trader as A
        free = A.paper_free_usd(s, c.db)
        if free + 1e-9 < add:
            return refuse(f"paper free ${free:,.2f} can't cover ${add:,.2f}")
        sc = await self._sell_check(mint, add)
        if not sc["ok"]:
            return refuse(sc["why"])
        lam = int(add / sol_usd * LAMPORTS)
        now = c.clock()
        if held:
            old = float(held["size_usd"] or 0)
            tokens = old / held["entry_price"] + add / price
            entry = (old + add) / tokens
            src_list = _srcs(held) + [self._src(ev, **info, add_usd=add, sell_check=sc["why"])]
            t._upd(held["id"], size_usd=round(old + add, 2), lamports=int(held["lamports"] or 0) + lam, entry_price=entry,
                   copy_src=json.dumps(src_list))
            self._log("PAPER", mint, "copy add", f"+${add:,.2f}", f"now ${old + add:,.2f}", f"entry={entry:.10g}", sc["why"])
            await t.push(f"{lab} · Added to {name} · +${add:,.2f} (now ${old + add:,.2f}) · his buy #{nth} "
                         f"· {ev['delay_s']:.0f}s after his buy", mint=mint)
            return {"status": "added", "id": held["id"], "add": add}
        oid = t._row(ts=now, night=c.night, mode="PAPER", mint=mint, name=name, symbol=symbol, status="open", size_usd=add,
                     sol_usd=sol_usd, lamports=lam, entry_price=price, peak=price, last_price=price, last_liq=liq,
                     entry_src=src, reason=f"copy {s['copy.name']}".strip(), quote=sc["why"], a_kind="copy", no_surge=1,
                     copy_src=json.dumps([self._src(ev, **info, sell_check=sc["why"])]))
        self._log("PAPER", mint, "copy sim-fill", f"${add:,.2f}", f"price={price}", f"src={src}", f"his_px={his_px}", sc["why"])
        self._announce_state()
        await t.push(f"{lab} · Buy {name} · ${add:,.2f} · his buy #{nth} ({ev['sol'] / LAMPORTS:.3f} SOL) · "
                     f"{ev['delay_s']:.0f}s after his buy\n"
                     f"Exits: his sells, -{s['copy.backstop_pct']:g}% backstop · simulated fill", mint=mint)
        c.event("fill", mint)
        return {"status": "paper", "id": oid}

    async def _sell_check(self, mint, usd):
        sc = await sell_check(self.s, mint, usd, net=self.c.net)
        if not sc["ok"] and not sc["skip"]:
            await asyncio.sleep(2)
            sc = await sell_check(self.s, mint, usd, net=self.c.net)
        return sc

    # ---- LIVE (DECISION 93)
    async def live_buy(self, ev, mint, name, symbol, price, liq, add, held, info, sol_usd, refuse):
        import agents.gmgn_trader as A
        import gmgn
        c, s, t = self.c, self.s, self.t
        lab, nth = label(s, "LIVE"), info.get("nth")
        if t.halted() or not s["gmgn.enabled"] or t.client is None or not t.client.configured:
            return refuse("halted, GMGN off or GMGN keys missing")
        if add > float(s["gmgn.cap_trade_usd"]) + 1e-9:
            return refuse(f"${add:,.2f} is over the ${s['gmgn.cap_trade_usd']:,.2f} per-trade cap")
        other = A.mint_held_live(c.db, mint)
        if other == "B" or (other == "A" and not held):
            return refuse(f"already held LIVE by {other} in the wallet")
        lam = int(add / sol_usd * LAMPORTS)
        need = A.buy_cost_lamports(s, lam)
        wallet = await self._wallet_rpc()                # public RPC: a refusal here never calls GMGN
        bud = self.live_budget(wallet, need, sol_usd)
        if not bud["ok"]:
            return refuse(bud["why"])
        if bud["borrow"]:
            self._log("BORROW from B", name, mint, f"need {need / LAMPORTS:.4f} SOL", f"A share {bud['own'] / LAMPORTS:.4f}",
                      f"borrowed {bud['borrow'] / LAMPORTS:.4f}", f"wallet free {bud['free'] / LAMPORTS:.4f}",
                      f"kept for B {bud['b_keep'] / LAMPORTS:.4f}")
        sc = await self._sell_check(mint, add)
        if not sc["ok"]:
            return refuse(sc["why"])
        gated = gmgn.GATE.buy_blocked()
        if gated:
            return refuse(f"GMGN gate: {gated}")
        body = A.buy_body(s, mint, lam)
        if held:                                         # adds ride the first buy's server-side backstop (sells 100% of holdings)
            body.pop("condition_orders", None)
            body.pop("sell_ratio_type", None)
        else:
            body["condition_orders"] = [{"order_type": "loss_stop", "side": "sell",
                                         "price_scale": f"{float(s['copy.backstop_pct']):g}", "sell_ratio": "100"}]
        if body["from_address"] != A.MAIN_WALLET or body["input_token"] != SOL or int(body["input_amount"]) != lam:
            return refuse("swap body mismatch")
        now = c.clock()
        src_entry = self._src(ev, **info, add_usd=add, sell_check=sc["why"], borrow_lamports=bud["borrow"])
        if held:
            oid = held["id"]
            aud = _audit(held)
            aud.setdefault("pending", []).append({"order_id": None, "add": add, "lam": lam, "ts": now})
            t._upd(oid, lamports=int(held["lamports"] or 0) + lam, copy_src=json.dumps(_srcs(held) + [src_entry]), audit=json.dumps(aud))
        else:
            oid = t._row(ts=now, night=c.night, mode="LIVE", mint=mint, name=name, symbol=symbol, status="submitting",
                         size_usd=add, sol_usd=sol_usd, lamports=lam, entry_price=price, peak=price, last_price=price, last_liq=liq,
                         reason=f"copy {s['copy.name']}".strip(), quote=sc["why"], a_kind="copy", no_surge=1,
                         body=json.dumps(body), conditions=json.dumps(body["condition_orders"]),
                         copy_src=json.dumps([src_entry]), audit=json.dumps({"buys": []}))
        try:
            r = await t.client.swap(body)
        except Exception as e:
            unknown = isinstance(e, gmgn.GmgnError) and e.status == 0
            self._log("LIVE", mint, "copy swap", "unknown" if unknown else "failed", str(e)[:200])
            if held:
                self._drop_pending(oid, None, lam)
            else:
                t._upd(oid, status="unknown" if unknown else "failed", reason=f"copy: {str(e)[:250]}")
            await t.push(f"{lab} · Buy {name} {'unknown' if unknown else 'failed'}: {str(e)[:160]}"
                         + ("\nCheck your GMGN wallet; I won't retry." if unknown else ""), kind="gmgn_alert", mint=mint)
            return {"status": "failed"}
        t._bal_stale = True
        if held:
            o = c.db.one("SELECT * FROM gmgn_orders WHERE id=?", (oid,))
            aud = _audit(o)
            for p in aud.get("pending") or []:
                if p.get("order_id") is None and p.get("ts") == now:
                    p["order_id"] = r.get("order_id")
            t._upd(oid, audit=json.dumps(aud))
        else:
            t._upd(oid, status="pending", order_id=r.get("order_id"), tx=r.get("hash"), strategy_order_id=r.get("strategy_order_id"))
        self._log("LIVE", mint, "copy swap sent", "add" if held else "buy", f"${add:,.2f}", f"{lam / LAMPORTS:.4f} SOL", r.get("order_id"))
        await t.push(f"{lab} · {'Adding to' if held else 'Buying'} {name} · ${add:,.2f} ({lam / LAMPORTS:.4f} SOL) · his buy #{nth} · "
                     f"{ev['delay_s']:.0f}s after his buy" + (" · borrowed from B's unused share" if bud["borrow"] else "")
                     + ("" if held else f"\nExits: his sells, -{s['copy.backstop_pct']:g}% backstop on GMGN"), mint=mint)
        res = await self._confirm(r)
        if res["status"] == "pending":
            return {"status": "pending", "id": oid}       # live_tick keeps polling query_order
        if held:
            self._drop_pending(oid, r.get("order_id"), lam if res["status"] == "failed" else 0)
        if res["status"] == "failed":
            if not held:
                t._upd(oid, status="failed", reason=f"copy: {res.get('why')}")
            await t.push(f"{lab} · Buy {name} failed: {res.get('why')}", kind="gmgn_alert", mint=mint)
            return {"status": "failed"}
        await self._apply_fill(oid, res["tx"], add, held is not None)
        return {"status": "open", "id": oid}

    def _drop_pending(self, oid, order_id, lam_back):
        o = self.c.db.one("SELECT * FROM gmgn_orders WHERE id=?", (oid,))
        aud = _audit(o)
        aud["pending"] = [p for p in aud.get("pending") or [] if p.get("order_id") != order_id]
        self.t._upd(oid, audit=json.dumps(aud), lamports=max(0, int(o["lamports"] or 0) - int(lam_back or 0)))

    async def _confirm(self, r, polls=10, gap=None) -> dict:
        gap = CONFIRM_GAP_S if gap is None else gap
        st, tx, oid = (r or {}).get("status"), (r or {}).get("hash"), (r or {}).get("order_id")
        for i in range(polls):
            if st in ("confirmed", "successful", "failed", "expired"):
                break
            if i or gap:
                await asyncio.sleep(gap)
            try:
                r = await self.t.client.query_order(oid)
                st, tx = r.get("status"), r.get("hash") or tx
            except Exception as e:
                self._log("LIVE", "query_order", str(e)[:160])
        if st in ("failed", "expired"):
            return {"status": "failed", "why": (r or {}).get("error_status") or st}
        if st in ("confirmed", "successful"):
            return {"status": "confirmed", "tx": tx}
        return {"status": "pending"}

    async def _apply_fill(self, oid, tx, add, is_add):
        t, c = self.t, self.c
        o = c.db.one("SELECT * FROM gmgn_orders WHERE id=?", (oid,))
        aud = _audit(o)
        if tx and tx not in [b.get("sig") for b in aud.get("buys") or []]:
            aud.setdefault("buys", []).append({"sig": tx})
        size = float(o["size_usd"] or 0) + (add if is_add else 0)
        upd = dict(audit=json.dumps(aud), size_usd=round(size, 2))
        if not is_add:
            upd.update(status="open", tx=tx)
        t._upd(oid, **upd)
        o = c.db.one("SELECT * FROM gmgn_orders WHERE id=?", (oid,))
        try:
            d = await t.buy_delta(o)
        except Exception:
            d = None
        if d:
            import chain
            entry = chain.result(d, [], o["sol_usd"])["entry_price"]
            if entry:
                t._upd(oid, entry_price=entry, entry_src="chain", **({} if is_add else {"peak": entry, "last_price": entry}))
        c.event("fill", o["mint"])
        self._announce_state()
        await t.push(f"{label(self.s, 'LIVE')} · {'Added to' if is_add else 'Bought'} {o['name']} · now ${size:,.2f} in it", mint=o["mint"])

    async def live_sell(self, o, bps: int, reason: str, ev=None) -> bool:
        import agents.gmgn_trader as A
        t, s = self.t, self.s
        bps = int(max(1, min(10000, bps)))
        body = A.sell_all_body(s, o["mint"])
        body["input_amount_bps"] = str(bps)
        try:
            r = await t.client.swap(body)
        except Exception as e:
            tries = self._sell_retry.get(o["id"], (0, "", 0, 0))[3] + 1
            self._sell_retry[o["id"]] = (bps, reason, time.time() + 10 * tries, tries)
            self._log("LIVE", o["mint"], "copy sell failed", reason, f"{bps}bps", f"try {tries}", str(e)[:200])
            await t.push(f"{label(s, 'LIVE')} · Sell {o['name']} ({bps / 100:.0f}%) failed: {str(e)[:140]} · retrying",
                         kind="gmgn_alert", mint=o["mint"])
            return False
        self._sell_retry.pop(o["id"], None)
        t._bal_stale = True
        aud = _audit(o)
        aud.setdefault("sells", []).append({"order_id": r.get("order_id"), "bps": bps, "reason": reason, "ts": self.c.clock()})
        upd = {"audit": json.dumps(aud)}
        if bps >= 10000:
            upd.update(status="exiting", exit_reason=reason, sell_order_id=r.get("order_id"))
        t._upd(o["id"], **upd)
        self._log("LIVE", o["mint"], "copy sell sent", reason, f"{bps / 100:.1f}%", r.get("order_id"))
        delay = f" · {ev['delay_s']:.0f}s after his sell" if ev else ""
        await t.push(f"{label(s, 'LIVE')} · Selling {bps / 100:.0f}% of {o['name']} ({A_EXIT.get(reason, reason)}){delay}", mint=o["mint"])
        return True

    async def live_tick(self, o, price, liq, now):
        """GmgnTrader.watch_once hook for LIVE copy rows (every gmgn.watch_s): resolve pending buys, retry failed sells,
        the desk-side -backstop check, and close the row from the chain once the wallet holds none of the coin."""
        import agents.gmgn_trader as A
        import chain
        t, c, s = self.t, self.c, self.s
        upd = {"last_check": now}
        if price:
            upd.update(last_price=price, peak=max(o["peak"] or price, price))
        if liq is not None:
            upd["last_liq"] = liq
        t._upd(o["id"], **upd)
        aud = _audit(o)
        if o["status"] == "pending" and o.get("order_id"):
            res = await self._confirm({"order_id": o["order_id"]}, polls=1, gap=0)
            if res["status"] == "confirmed":
                await self._apply_fill(o["id"], res["tx"], 0, False)
            elif res["status"] == "failed" or now - (o["ts"] or now) > 600:
                t._upd(o["id"], status="failed" if res["status"] == "failed" else "unknown", reason=f"copy: {res.get('why') or 'unconfirmed after 10 min'}")
                return
            else:
                return
        for p in list(aud.get("pending") or []):
            if not p.get("order_id"):
                if now - (p.get("ts") or now) > 120:
                    self._drop_pending(o["id"], None, p.get("lam"))
                continue
            res = await self._confirm({"order_id": p["order_id"]}, polls=1, gap=0)
            if res["status"] == "pending" and now - (p.get("ts") or now) < 600:
                continue
            self._drop_pending(o["id"], p["order_id"], 0 if res["status"] == "confirmed" else p.get("lam"))
            if res["status"] == "confirmed":
                await self._apply_fill(o["id"], res["tx"], p.get("add") or 0, True)
        o = c.db.one("SELECT * FROM gmgn_orders WHERE id=?", (o["id"],))
        if o["status"] not in ("open", "exiting"):
            return
        rt = self._sell_retry.get(o["id"])
        if rt and time.time() >= rt[2] and rt[3] <= 6:
            await self.live_sell(o, rt[0], rt[1])
            o = c.db.one("SELECT * FROM gmgn_orders WHERE id=?", (o["id"],))
        if now - float(o["ts"] or now) < 45:
            return
        bal = await chain.token_balance(c.net, A.MAIN_WALLET, o["mint"])
        if bal == 0:
            return await self._live_close(o)
        if (o["status"] == "open" and price and o.get("entry_price") and o["id"] not in self._sell_retry
                and price <= o["entry_price"] * (1 - float(s["copy.backstop_pct"]) / 100.0)):
            self._log("LIVE", o["mint"], "desk backstop", f"price {price:.10g} vs entry {o['entry_price']:.10g}")
            await self.live_sell(o, 10000, "copy stop")
        elif o["status"] == "exiting" and bal and now - float((_audit(o).get("sells") or [{}])[-1].get("ts") or now) > 120:
            self._log("LIVE", o["mint"], "exit not landed after 120s: selling again")
            await self.live_sell(o, 10000, o.get("exit_reason") or "copy sell")

    async def _live_close(self, o):
        import agents.gmgn_trader as A
        import chain
        t, c = self.t, self.c
        try:
            buy = await t.buy_delta(o)
            sells = await chain.wallet_sells(c.net, A.MAIN_WALLET, o["mint"], o["ts"], cache=t._txc)
        except Exception:
            buy, sells = None, []
        sol_usd = await self._sol_px()
        reason = o.get("exit_reason") or "copy stop"     # tokens gone without our full sell = GMGN's server-side backstop
        upd = dict(status="closed", closed_at=c.clock(), exit_reason=reason)
        if buy and sells and sol_usd:
            r = chain.result(buy, sells, sol_usd)
            upd.update(pnl_usd=r["pnl_usd"], pct=r["pct"], exit_price=r["exit_price"])
        t._upd(o["id"], **upd)
        try:
            await t._cancel_mint_strategies(o)
        except Exception as e:
            self._log("LIVE", o["mint"], "cancel exits", str(e)[:160])
        self._sell_retry.pop(o["id"], None)
        t._bal_stale = True
        c.event("exit", o["mint"])
        self._announce_state()
        pnl = upd.get("pnl_usd")
        tail = (f"{'+' if pnl >= 0 else '-'}${abs(pnl):,.2f} ({upd['pct']:+.1f}%) on chain" if pnl is not None
                else "P&L pending chain read")
        self._log("LIVE", o["mint"], "copy closed", reason, tail)
        await t.push(f"{label(self.s, 'LIVE')} · Closed {o['name']} · {A_EXIT.get(reason, reason)} · {tail}", mint=o["mint"])

    async def copy_sell(self, ev):
        c, s, t = self.c, self.s, self.t
        mint = ev["mint"]
        held = c.db.one("SELECT * FROM gmgn_orders WHERE mint=? AND a_kind='copy' AND status='open' ORDER BY id DESC LIMIT 1", (mint,))
        self._log("his sell", mint, f"tx={ev['sig']}", f"frac={ev['frac']:.4f}", f"delay={ev['delay_s']}s",
                  f"copying ({held['mode']})" if held else "no copy open (old bag or skipped buy): nothing to sell")
        if not held:
            return None
        frac = min(1.0, max(0.0, float(ev["frac"])))
        if held["mode"] == "LIVE":
            full = frac >= 0.98 or float(held["size_usd"] or 0) * (1 - frac) < 1.0
            bps = 10000 if full else int(round(frac * 10000))
            ok = await self.live_sell(held, bps, "copy sell", ev)
            o = c.db.one("SELECT * FROM gmgn_orders WHERE id=?", (held["id"],))
            t._upd(held["id"], copy_src=json.dumps(_srcs(o) + [self._src(ev, sold_frac=round(frac, 6), sent=ok, bps=bps)]))
            return {"status": "selling" if ok else "sell failed", "id": held["id"], "bps": bps}
        sol_usd = await t.sol_usd()
        price, liq, *_ = await self._price(mint, ev, sol_usd)
        price = price or held["last_price"] or held["entry_price"]
        size = float(held["size_usd"] or 0)
        now = c.clock()
        src_list = _srcs(held) + [self._src(ev, sold_frac=round(frac, 6), price=price)]
        if frac >= 0.98 or size * (1 - frac) < 1.0:
            t._upd(held["id"], copy_src=json.dumps(src_list))
            await t._paper_close({**held, "copy_src": json.dumps(src_list)}, "copy sell", price, now)
            self._announce_state()
            return {"status": "closed", "id": held["id"]}
        part = round(size * frac, 2)
        child = {**held, "size_usd": part}
        pnl, pct = t._pnl(child, price)
        cid = t._row(ts=held["ts"], night=held["night"], mode="PAPER", mint=mint, name=held["name"], symbol=held["symbol"],
                     status="closed", size_usd=part, sol_usd=held["sol_usd"], lamports=int((held["lamports"] or 0) * frac),
                     entry_price=held["entry_price"], peak=held["peak"], last_price=price, exit_price=price,
                     exit_reason="copy sell", pnl_usd=pnl, pct=pct, closed_at=now, reason=f"{held['reason']} · partial {frac * 100:.0f}%",
                     a_kind="copy", no_surge=1, copy_src=json.dumps(src_list))
        t._upd(held["id"], size_usd=round(size - part, 2), lamports=int((held["lamports"] or 0) * (1 - frac)),
               copy_src=json.dumps(src_list), last_price=price)
        sign = "+" if pnl >= 0 else "-"
        await t.push(f"{label(s, "PAPER")} · Sold {frac * 100:.0f}% of {held['name']} (he sold {frac * 100:.0f}%) · {sign}${abs(pnl):,.2f} "
                     f"({pct:+.1f}%) · ${size - part:,.2f} left · {ev['delay_s']:.0f}s after his sell", mint=mint)
        c.event("exit", mint)
        return {"status": "partial", "id": held["id"], "child": cid}


A_EXIT = {"copy sell": "his sell", "copy stop": "backstop stop"}


def _audit(o) -> dict:
    try:
        v = json.loads((o or {}).get("audit") or "{}")
        return v if isinstance(v, dict) else {}
    except Exception:
        return {}


def _pending_usd(o) -> float:
    return sum(float(p.get("add") or 0) for p in _audit(o).get("pending") or [])


def _srcs(o) -> list:
    try:
        v = json.loads(o.get("copy_src") or "[]")
        return v if isinstance(v, list) else [v]
    except Exception:
        return []
