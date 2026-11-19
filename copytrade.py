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
  buy:  size = clamp(fraction x book, copy.min_usd, copy.max_usd); book = copy.book_usd + realized copy P&L.
        A second buy of a mint we already copy adds to it (weighted entry) up to copy.max_position_usd.
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
    raw = max(0.0, float(frac)) * float(book)
    return raw, round(min(max(raw, float(s["copy.min_usd"])), float(s["copy.max_usd"])), 2)


def active(s) -> bool:
    return s["gmgn.profile"] in ("copy", "hybrid") and bool(s["copy.wallet"])


def label(s) -> str:
    return f"PAPER · copying {s['copy.name'] or (s['copy.wallet'] or '')[:6]}"


def open_copies(db) -> list:
    ph = ",".join("?" * len(ACTIVE))
    return db.rows(f"SELECT * FROM gmgn_orders WHERE mode='PAPER' AND a_kind='copy' AND status IN ({ph}) ORDER BY id", ACTIVE)


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
            "paperOnly": True}


class CopyWatcher:
    name = "gmgn_copy"

    def __init__(self, ctx, trader):
        self.c, self.t = ctx, trader
        self.log = ctx.logs.get("gmgn") or ctx.logs["executor"]
        self._tries: dict = {}
        self._state_logged = None

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
        if s["gmgn.mode"] != "PAPER":
            return "idle: copy is PAPER-only and gmgn.mode is LIVE"
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

    async def copy_buy(self, ev):
        c, s, t = self.c, self.s, self.t
        mint, lab = ev["mint"], label(s)
        sol_usd = await t.sol_usd()
        price, liq, name, symbol, src, his_px = await self._price(mint, ev, sol_usd)
        name = name or mint[:6]
        st = self.state()
        book = max(float(s["copy.min_usd"]), float(s["copy.book_usd"]) + realized_copy_pnl(c.db, st.get("since") or 0))
        raw, size = copy_size(s, ev["frac"], book)
        info = dict(book=round(book, 2), raw_usd=round(raw, 2), size_usd=size, price=price, price_src=src, his_px=his_px)
        self._log("his buy", name, mint, f"tx={ev['sig']}", f"his {ev['sol'] / LAMPORTS:.4f} SOL of {ev['pre_sol'] / LAMPORTS:.4f}",
                  f"frac={ev['frac']:.4f}", f"book=${book:,.2f}", f"raw=${raw:,.2f}", f"size=${size:,.2f}", f"delay={ev['delay_s']}s")

        def refuse(why):
            t._row(ts=c.clock(), night=c.night, mode="PAPER", mint=mint, name=name, symbol=symbol, status="refused",
                   reason=f"copy: {why}", size_usd=size, a_kind="copy", copy_src=json.dumps(self._src(ev, **info)))
            self._log("copy skipped", name, mint, why)
            return {"status": "refused", "reason": why}

        if not price or not sol_usd:
            return refuse("no price")
        held = c.db.one(f"SELECT * FROM gmgn_orders WHERE mint=? AND status IN ({','.join('?' * len(ACTIVE))}) ORDER BY id DESC LIMIT 1",
                        (mint, *ACTIVE))
        if held and held.get("a_kind") != "copy":
            return refuse(f"A already holds it ({held.get('a_kind') or held['mode']} #{held['id']})")
        if held:                                         # he adds: add to our copy up to the position cap
            room = float(s["copy.max_position_usd"]) - float(held["size_usd"] or 0)
            add = round(min(size, room), 2)
            if add < float(s["copy.min_usd"]):
                return self._log("add skipped", name, f"position ${held['size_usd']:,.2f} at the ${s['copy.max_position_usd']:,.2f} cap")
        else:
            n = len(open_copies(c.db))
            if n >= int(s["copy.max_open"]):
                return refuse(f"{n}/{int(s['copy.max_open'])} copy positions already open")
            add = size
        import agents.gmgn_trader as A
        free = A.paper_free_usd(s, c.db)
        if free + 1e-9 < add:
            return refuse(f"paper free ${free:,.2f} can't cover ${add:,.2f}")
        sc = await sell_check(s, mint, add, net=c.net)
        if not sc["ok"] and not sc["skip"]:
            await asyncio.sleep(2)
            sc = await sell_check(s, mint, add, net=c.net)
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
            await t.push(f"{lab} · Added to {name} · +${add:,.2f} (now ${old + add:,.2f}) · he spent {ev['frac'] * 100:.1f}% of his SOL "
                         f"· {ev['delay_s']:.0f}s after his buy", mint=mint)
            return {"status": "added", "id": held["id"], "add": add}
        oid = t._row(ts=now, night=c.night, mode="PAPER", mint=mint, name=name, symbol=symbol, status="open", size_usd=add,
                     sol_usd=sol_usd, lamports=lam, entry_price=price, peak=price, last_price=price, last_liq=liq,
                     entry_src=src, reason=f"copy {s['copy.name']}".strip(), quote=sc["why"], a_kind="copy", no_surge=1,
                     copy_src=json.dumps([self._src(ev, **info, sell_check=sc["why"])]))
        self._log("PAPER", mint, "copy sim-fill", f"${add:,.2f}", f"price={price}", f"src={src}", f"his_px={his_px}", sc["why"])
        self._announce_state()
        await t.push(f"{lab} · Buy {name} · ${add:,.2f} · he spent {ev['frac'] * 100:.1f}% of his SOL "
                     f"({ev['sol'] / LAMPORTS:.3f} SOL) · {ev['delay_s']:.0f}s after his buy\n"
                     f"Exits: his sells, -{s['copy.backstop_pct']:g}% backstop · simulated fill", mint=mint)
        c.event("fill", mint)
        return {"status": "paper", "id": oid}

    async def copy_sell(self, ev):
        c, s, t = self.c, self.s, self.t
        mint = ev["mint"]
        held = c.db.one("SELECT * FROM gmgn_orders WHERE mint=? AND a_kind='copy' AND mode='PAPER' AND status='open' ORDER BY id DESC LIMIT 1",
                        (mint,))
        self._log("his sell", mint, f"tx={ev['sig']}", f"frac={ev['frac']:.4f}", f"delay={ev['delay_s']}s",
                  "copying" if held else "no copy open (old bag or skipped buy): nothing to sell")
        if not held:
            return None
        sol_usd = await t.sol_usd()
        price, liq, *_ = await self._price(mint, ev, sol_usd)
        price = price or held["last_price"] or held["entry_price"]
        frac = min(1.0, max(0.0, float(ev["frac"])))
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
        await t.push(f"{label(s)} · Sold {frac * 100:.0f}% of {held['name']} (he sold {frac * 100:.0f}%) · {sign}${abs(pnl):,.2f} "
                     f"({pct:+.1f}%) · ${size - part:,.2f} left · {ev['delay_s']:.0f}s after his sell", mint=mint)
        c.event("exit", mint)
        return {"status": "partial", "id": held["id"], "child": cid}


def _srcs(o) -> list:
    try:
        v = json.loads(o.get("copy_src") or "[]")
        return v if isinstance(v, list) else [v]
    except Exception:
        return []
