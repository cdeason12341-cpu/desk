"""STRATEGY B · PAPER ONLY: B's own feed of older coins (DECISION 63, owner 2026-11-16 8:23 PM CT).

The shared scanner only sees new launches (and drops non-pump coins older than 24h). Strategy A is unchanged.
Every strat_b.feed_poll_s (300s) this pulls GeckoTerminal's Solana trending pools (1h and 6h pages 1-2, 24h pages 1-3; 7 calls, 2.5s apart), keeps
SOL-quoted graduated pools 1h–7d old inside B's market-cap and liquidity range, and runs B's own read-only checks on
up to strat_b.feed_checks_per_poll new coins: mint/freeze authority revoked, top-10 % and holder count
(holders.py helpers), and bundled % (RugCheck insiderNetworks via auditor.bundle_stats). Passing coins go to
StratB.offer like any other coin. Never places an order, never signs, never touches a wallet.
"""
from __future__ import annotations
import asyncio, datetime as dt
import fields, holders, sources
from ctx import iso

GECKO = "https://api.geckoterminal.com/api/v2/networks/solana/"
SOURCES = (("gecko trending 1h", "trending_pools", {"duration": "1h", "page": 1}),
           ("gecko trending 1h", "trending_pools", {"duration": "1h", "page": 2}),
           ("gecko trending 6h", "trending_pools", {"duration": "6h", "page": 1}),
           ("gecko trending 6h", "trending_pools", {"duration": "6h", "page": 2}),
           ("gecko trending 24h", "trending_pools", {"duration": "24h", "page": 1}),
           ("gecko trending 24h", "trending_pools", {"duration": "24h", "page": 2}),
           ("gecko trending 24h", "trending_pools", {"duration": "24h", "page": 3}))
CALL_GAP_S = 2.5          # between GeckoTerminal calls (public limit ~30/min; B makes 7 per 5 min)
RETRY_FAILED_S = 6 * 3600  # a coin whose holder data couldn't be read is retried after 6h
WSOL = "So11111111111111111111111111111111111111112"


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _rel_id(p, name):
    i = (((p.get("relationships") or {}).get(name) or {}).get("data") or {}).get("id") or ""
    return i.split("_", 1)[1] if i.startswith("solana_") else i


def parse_pools(payload, label, now) -> list[dict]:
    out = []
    for p in (payload or {}).get("data") or []:
        a = p.get("attributes") or {}
        try:
            created = dt.datetime.fromisoformat(str(a.get("pool_created_at")).replace("Z", "+00:00")).timestamp()
        except Exception:
            continue
        out.append({"mint": _rel_id(p, "base_token"), "quote": _rel_id(p, "quote_token"), "dex": _rel_id(p, "dex"),
                    "pool": a.get("address"), "name": str(a.get("name") or "").split(" / ")[0].strip(),
                    "symbol": str(a.get("name") or "").split(" / ")[0].strip(), "created": created,
                    "ageMin": (now - created) / 60.0, "mc": _num(a.get("market_cap_usd")) or _num(a.get("fdv_usd")),
                    "liq": _num(a.get("reserve_in_usd")) or 0.0, "src": label})
    return out


def candidate_reason(s, cand) -> str | None:
    """None = worth B's holder checks. Otherwise why it was left out (not recorded; it may qualify later)."""
    if cand["quote"] != WSOL:
        return "not SOL-quoted"
    if cand["dex"] not in sources.GRADUATED_DEX_GECKO:
        return f"dex {cand['dex']}"
    if sources.excluded(cand["mint"], cand["symbol"]):
        return "excluded"
    if cand["ageMin"] < s["strat_b.feed_min_age_min"] or cand["ageMin"] > s["strat_b.max_age_min"]:
        return f"age {cand['ageMin']:.0f}m"
    if cand["mc"] is None or not (s["strat_b.min_mc_usd"] <= cand["mc"] <= s["strat_b.max_mc_usd"]):
        return f"mc {cand['mc']}"
    if cand["liq"] < s["strat_b.min_liq_usd"]:
        return f"liq {cand['liq']:.0f}"
    return None


class BFeed:
    name = "strat_b_feed"

    def __init__(self, sb):
        self.sb, self.c, self.log = sb, sb.c, sb.log
        self.last = 0.0
        self.backoff_until = 0.0
        self.failed: dict[str, float] = {}
        self.stats = {"pulled": 0, "candidates": 0, "checked": 0, "offered": 0}

    async def loop(self):
        while True:
            try:
                note = await self.poll_once()
            except Exception as e:
                note = "error"
                self.c.error("strat_b_feed", "poll", str(e)[:300])
            self.c.hb("strat_b_feed", note or "")
            await asyncio.sleep(15)

    async def gecko(self, path, params):
        net = self.c.net
        r = await net._send(net.http.get(GECKO + path, params=params, headers={"Accept": "application/json"}, timeout=20), 22, "gecko " + path)
        if r.status_code == 429:
            from net import RateLimited
            raise RateLimited("gecko 429")
        if r.status_code != 200:
            raise RuntimeError(f"gecko {path} {r.status_code}")
        d = r.json()
        return d if isinstance(d, dict) else {"data": []}

    async def poll_once(self, now=None, payloads=None):
        c, s = self.c, self.c.s
        now = c.clock() if now is None else now
        if not (self.sb.enabled() and s["strat_b.feed_enabled"]):
            return "off"
        if payloads is None and (now - self.last < s["strat_b.feed_poll_s"] or now < self.backoff_until):
            return f"idle · {self.stats}"
        self.last = now
        pools, errs = [], []
        for i, (label, path, params) in enumerate(SOURCES):
            try:
                d = payloads[i] if payloads is not None else await self.gecko(path, params)
            except Exception as e:
                errs.append(f"{params.get('duration')}p{params.get('page')}:{str(e)[:40]}")
                if type(e).__name__ == "RateLimited":       # polite: stop this round, try again in 60s
                    self.backoff_until = now + 60
                    self.last = now - s["strat_b.feed_poll_s"] + 60
                    break
                continue
            pools += parse_pools(d, label, now)
            if payloads is None:
                await asyncio.sleep(CALL_GAP_S)
        self.stats["pulled"] = len(pools)
        seen, cands = set(), []
        for p in pools:
            if p["mint"] in seen:
                continue
            seen.add(p["mint"])
            if candidate_reason(s, p) is None:
                cands.append(p)
        known = {r["mint"] for r in c.db.rows("SELECT mint FROM strat_b_watch")}
        cands = [p for p in cands if p["mint"] not in known and now - self.failed.get(p["mint"], -1e18) > RETRY_FAILED_S]
        self.stats["candidates"] = len(cands)
        watching = c.db.one("SELECT COUNT(*) n FROM strat_b_watch WHERE status='watching'")["n"]
        n = 0
        for p in cands:
            if n >= s["strat_b.feed_checks_per_poll"] or watching >= s["strat_b.max_watch"]:
                break
            n += 1
            self.c.hb("strat_b_feed", f"checking {p['mint'][:6]}")
            audit = await self.check(p)
            self.stats["checked"] += 1
            if audit is None:
                self.failed[p["mint"]] = now
                continue
            scan = {"mint": p["mint"], "name": p["name"], "symbol": p["symbol"], "ageMin": p["ageMin"],
                    "b_source": f"{p['src']} · {p['ageMin'] / 1440:.1f}d old" if p["ageMin"] >= 1440 else f"{p['src']} · {p['ageMin'] / 60:.1f}h old"}
            self.sb.offer(scan, audit)
            self.stats["offered"] += 1
            if c.db.one("SELECT status FROM strat_b_watch WHERE mint=?", (p["mint"],)) and \
               c.db.one("SELECT status FROM strat_b_watch WHERE mint=?", (p["mint"],))["status"] == "watching":
                watching += 1
        self.log.info(f"{iso(now)}\tfeed\tpulled={len(pools)}\tcandidates={len(cands)}\tchecked={n}\twatching={watching}"
                      + (f"\terrors={errs}" if errs else ""))
        return f"pulled={len(pools)} candidates={len(cands)} checked={n}"

    async def check(self, p) -> dict | None:
        """B's own read-only holder checks. None = couldn't read (retry later). b_block = permanent skip reason."""
        net, mint = self.c.net, p["mint"]
        try:
            mi = await net.rpc("getAccountInfo", [mint, {"encoding": "jsonParsed"}])
            info = fields.get(mi, "value.data.parsed.info") or {}
            supply = int(info.get("supply") or 0)
        except Exception as e:
            self.log.info(f"{iso()}\tfeed check\t{mint}\tno mint info\t{type(e).__name__}")
            return None
        if "mintAuthority" not in info or supply <= 0:
            return None
        if info.get("mintAuthority") is not None:
            return {"b_block": "mint authority not revoked"}
        if info.get("freezeAuthority") is not None:
            return {"b_block": "freeze authority not revoked"}
        try:
            report = await net.rug_report(mint)
        except Exception as e:
            self.log.info(f"{iso()}\tfeed check\t{mint}\tno rugcheck report\t{type(e).__name__}")
            return None
        from agents.auditor import bundle_stats
        bp, _ = bundle_stats(report, supply)
        amm = [k for k, v in ((report if isinstance(report, dict) else {}).get("knownAccounts") or {}).items() if (v or {}).get("type") == "AMM"]
        try:
            acc = await holders.fetch_accounts(net, mint, timeout=60)
        except Exception as e:
            self.log.info(f"{iso()}\tfeed check\t{mint}\tno holder data\t{type(e).__name__}")
            return None
        top10 = holders.top10_pct(acc, supply, holders.excluded_owners(p.get("pool"), mint, amm))
        out = {"holders": holders.holder_count(acc), "bundle_pct": None if bp is None else round(bp, 4),
               "layers": [{"n": 4, "value": "" if top10 is None else f"{top10}%"}]}
        if bp is None:
            out["b_block"] = "no data: bundles"
        return out
