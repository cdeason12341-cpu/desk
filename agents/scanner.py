"""AGENT 1 · SCANNER. Counts and cuts. Never judges quality."""
from __future__ import annotations
import asyncio, json, time, unicodedata
import fields, holders, solana
from ctx import iso
from net import RateLimited

WS_URL = "wss://pumpportal.fun/api/data"


def norm_name(s: str | None) -> str:
    """lowercase, strip spaces, digits, emoji (keep letters only)."""
    if not s:
        return ""
    return "".join(ch for ch in s.lower() if unicodedata.category(ch).startswith("L"))


def is_copycat(name, symbol, runners: list[dict]) -> str | None:
    mine = [n for n in (norm_name(name), norm_name(symbol)) if n]
    for r in runners:
        for theirs in (norm_name(r.get("name")), norm_name(r.get("symbol"))):
            if not theirs:
                continue
            for m in mine:
                if m == theirs or (min(len(m), len(theirs)) >= 3 and (m in theirs or theirs in m)):
                    return r.get("name") or r.get("symbol")
    return None


def fmt_k(v: float) -> str:
    return f"${v/1000:.1f}K" if v < 1_000_000 else f"${v/1_000_000:.2f}M"


def pick_pair(pairs: list[dict], mint: str) -> dict | None:
    mine = [p for p in pairs or [] if fields.get(p, "baseToken.address") == mint]
    return fields.select_pair(mine)


def decide(s, coin: dict, pair: dict | None, top10: float | None, runners: list[dict], now: float) -> tuple[str, str | None]:
    """('pass'|'kill'|'wait'|'drop', reason). Pure; used by the loop, tests and replay."""
    mint = coin["mint"]
    age_min = (now - coin["migrated_at"]) / 60.0
    if not mint.endswith("pump"):
        return "kill", "not a pump mint"
    if pair is None:                                   # DexScreener doesn't know it yet: it waits
        return ("drop", "never qualified") if age_min >= s["scanner.max_wait_min"] else ("wait", None)
    if fields.get(pair, "quoteToken.address") != solana.WSOL:
        return "kill", f"not SOL-quoted ({fields.get(pair, 'quoteToken.symbol') or '?'})"
    liq = float(fields.get(pair, "liquidity.usd") or 0)
    if liq < s["scanner.min_liquidity_usd"]:
        return "kill", f"liq {fmt_k(liq)}"
    if top10 is not None and top10 > s["scanner.max_top10_pct"]:
        return "kill", f"top 10 hold {top10:.0f}%"
    h1 = fields.get(pair, "priceChange.h1")
    if h1 is not None and float(h1) > s["scanner.max_h1_change_pct"]:
        return "kill", f"+{float(h1):.0f}% in 1h"
    cc = is_copycat(fields.get(pair, "baseToken.name"), fields.get(pair, "baseToken.symbol"),
                    [r for r in runners if r.get("mint") != mint])
    if cc:
        return "kill", f"copycat of {cc}"
    if age_min >= s["scanner.max_wait_min"]:
        return "drop", "never qualified"
    if top10 is None:
        return "wait", None                              # never pass without a top-10 number
    if age_min >= s["scanner.min_pool_age_min"]:
        return "pass", None
    return "wait", None


async def creator_balance(net, creator, mint) -> int | None:
    try:
        r = await net.rpc("getTokenAccountsByOwner", [creator, {"mint": mint}, {"encoding": "jsonParsed"}])
        tot = 0
        for a in (r or {}).get("value", []):
            tot += int(a["account"]["data"]["parsed"]["info"]["tokenAmount"]["amount"])
        return tot
    except Exception:
        return None


class Scanner:
    name = "scanner"

    def __init__(self, ctx):
        self.c = ctx
        self.log = ctx.logs["scanner"]
        self.ws_last = 0.0
        self.supply_cache: dict[str, int] = {}
        self.extra: dict[str, dict] = {}

    # ---------- websocket (ONE connection for the desk)
    async def ws_loop(self):
        import websockets
        backoff = 1
        while True:
            try:
                async with websockets.connect(WS_URL, max_size=2**22, ping_interval=20, ping_timeout=20) as ws:
                    await ws.send(json.dumps({"method": "subscribeMigration"}))
                    self.c.db.x("INSERT INTO ws_events(ts,kind,note) VALUES(?,?,?)", (time.time(), "open", ""))
                    self.log.info(f"{iso()}\tws\topen\tsubscribeMigration")
                    backoff = 1
                    self.ws_last = time.time()
                    while True:
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=self.c.s["scanner.ws_silent_s"])
                        except asyncio.TimeoutError:
                            raise ConnectionError(f"silent {self.c.s['scanner.ws_silent_s']}s")
                        self.ws_last = time.time()
                        try:
                            msg = json.loads(raw)
                        except Exception:
                            continue
                        if isinstance(msg, dict) and msg.get("mint"):
                            await self.on_migration(msg)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.c.db.x("INSERT INTO ws_events(ts,kind,note) VALUES(?,?,?)", (time.time(), "reconnect", str(e)[:200]))
                self.log.info(f"{iso()}\tws\treconnect\t{str(e)[:120]}\tbackoff={backoff}s")
                await asyncio.sleep(backoff)
                backoff = min(60, backoff * 2)

    async def on_migration(self, msg: dict):
        m = fields.FIELDS["migration"]
        mint = fields.get(msg, m["mint"])
        if not mint or self.c.coin(mint):
            return
        now = self.c.clock()
        self.c.db.x("INSERT OR IGNORE INTO coins(mint,signature,pool_label,creator,migrated_at,first_seen,status,night,raw) VALUES(?,?,?,?,?,?,?,?,?)",
                    (mint, fields.get(msg, m["signature"]), fields.get(msg, m["pool_label"]), None, now, now, "room",
                     self.c.night, json.dumps(msg)))
        self.c.hb("scanner", "migration")
        if not mint.endswith("pump"):
            self.emit({"mint": mint, "migrated_at": now}, None, None, "kill", "not a pump mint", now)
            return
        asyncio.create_task(self.capture_creator(mint))

    async def capture_creator(self, mint):
        """Creator: migration message has none -> RugCheck report. Creator balance NOW via RPC."""
        creator, amm = None, []
        for _ in range(3):
            try:
                rep = await self.c.net.rug_report(mint)
                creator = fields.get(rep, fields.FIELDS["rugcheck_report"]["creator"])
                ka = fields.get(rep, "knownAccounts") or {}
                amm = [k for k, v in ka.items() if (v or {}).get("type") == "AMM"]
                break
            except Exception:
                await asyncio.sleep(2)
        self.extra[mint] = {"amm": amm}
        bal = await creator_balance(self.c.net, creator, mint) if creator else None
        self.c.db.x("UPDATE coins SET creator=?, creator_balance_raw=?, creator_known=? WHERE mint=?",
                    (creator, None if bal is None else str(bal), 1 if creator else 0, mint))

    # ---------- polling
    async def poll_loop(self):
        while True:
            t0 = time.time()
            try:
                if "scanner" not in self.c.paused:
                    await self.poll_once()
                self.c.hb("scanner", f"room={len(self.room())}" + (" paused" if "scanner" in self.c.paused else ""))
            except RateLimited:
                self.log.info(f"{iso()}\tdexscreener\t429\twait {self.c.s['scanner.backoff_429_s']}s")
                await asyncio.sleep(self.c.s["scanner.backoff_429_s"])
            except Exception as e:
                self.c.error("scanner", "poll", str(e)[:300])
            await asyncio.sleep(max(1, self.c.s["scanner.poll_s"] - (time.time() - t0)))

    def room(self):
        return self.c.db.rows("SELECT * FROM coins WHERE status='room'")

    def runners(self):
        return self.c.db.rows("SELECT mint,name,symbol FROM coins WHERE runner=1 AND first_seen>=?", (time.time() - 86400,))

    async def poll_once(self):
        room = self.room()
        if not room:
            return
        pairs = await self.c.net.dex_tokens([r["mint"] for r in room])
        now = self.c.clock()
        thr = self.c.s["scanner.runner_h24_pct"]
        for coin in room:
            pair = pick_pair(pairs, coin["mint"])
            if pair:
                price = fields.get(pair, "priceUsd")
                h24 = fields.get(pair, "priceChange.h24")
                self.c.db.x("UPDATE coins SET name=?, symbol=?, pool=?, price=?, quote=?, h1=?, h24=?, runner=? WHERE mint=?",
                            (fields.get(pair, "baseToken.name"), fields.get(pair, "baseToken.symbol"),
                             fields.get(pair, "pairAddress"), float(price) if price else None,
                             fields.get(pair, "quoteToken.address"), fields.get(pair, "priceChange.h1"), h24,
                             1 if (h24 is not None and float(h24) >= thr) else 0, coin["mint"]))
                if price:
                    self.c.db.x("INSERT INTO prices(ts,mint,price,vol_m5,src) VALUES(?,?,?,?,?)",
                                (now, coin["mint"], float(price), fields.get(pair, "volume.m5"), "scanner"))
            top10 = None
            pre, _ = decide(self.c.s, coin, pair, None, [], now)
            if pair and pre == "wait" and (now - coin["migrated_at"]) / 60.0 >= 0:
                top10 = await self.top10(coin["mint"], fields.get(pair, "pairAddress"))
            verdict, reason = decide(self.c.s, coin, pair, top10, self.runners(), now)
            if verdict != "wait":
                self.emit(coin, pair, top10, verdict, reason, now)

    async def top10(self, mint, pool):
        try:
            supply = self.supply_cache.get(mint)
            if supply is None:
                r = await self.c.net.rpc("getTokenSupply", [mint])
                supply = int(r["value"]["amount"])
                self.supply_cache[mint] = supply
            accts = await holders.fetch_accounts(self.c.net, mint)
            ex = holders.excluded_owners(pool, mint, (self.extra.get(mint) or {}).get("amm"))
            return holders.top10_pct(accts, supply, ex)
        except Exception as e:
            self.c.error("scanner", "top10", f"{mint} {str(e)[:200]}")
            return None

    def emit(self, coin, pair, top10, verdict, reason, now):
        mint = coin["mint"]
        row = self.c.coin(mint) or {}
        out = {"agent": "scanner", "mint": mint,
               "name": (fields.get(pair, "baseToken.name") if pair else None) or row.get("name"),
               "symbol": (fields.get(pair, "baseToken.symbol") if pair else None) or row.get("symbol"),
               "pool": fields.get(pair, "pairAddress") if pair else None,
               "liquidityUsd": float(fields.get(pair, "liquidity.usd") or 0) if pair else None,
               "top10Pct": top10,
               "h1ChangePct": fields.get(pair, "priceChange.h1") if pair else None,
               "ageMin": int((now - coin["migrated_at"]) / 60),
               "verdict": "pass" if verdict == "pass" else "kill",
               "reason": reason, "ts": iso(now)}
        status = {"pass": "passed", "kill": "killed", "drop": "dropped"}[verdict]
        self.c.db.x("UPDATE coins SET status=?, reason=?, liq_at_scan=?, top10=? WHERE mint=?",
                    (status, reason, out["liquidityUsd"], top10, mint))
        self.c.db.x("INSERT INTO scans(ts,mint,verdict,reason,json,night) VALUES(?,?,?,?,?,?)",
                    (now, mint, out["verdict"], reason, json.dumps(out), self.c.night))
        self.log.info(f"{out['ts']}\t{mint}\t{out['verdict']}\t{reason or ''}\tliq={out['liquidityUsd']}\ttop10={top10}\th1={out['h1ChangePct']}\tage={out['ageMin']}")
        if verdict == "pass":
            sent = self.c.db.one("SELECT sent FROM coins WHERE mint=?", (mint,))
            if not (sent and sent["sent"]):                     # never sends the same mint twice
                self.c.db.x("UPDATE coins SET sent=1 WHERE mint=?", (mint,))
                if self.c.bus:
                    self.c.bus.auditor.put_nowait(out)
        self.c.event("scan", mint)
        return out
