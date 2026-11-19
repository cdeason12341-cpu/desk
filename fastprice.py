"""Fast price feed for the GMGN exit watchers (gmgn_surge, gmgn_trail), DECISION 57.

Order per open LIVE mint, every risk.fast_poll_s (default 1.5 s):
  1. pool  - the pool's on-chain state over RPC (poolprice.py): ONE getMultipleAccounts call for all open mints
             (batched 100 accounts/call), desk.rpc_url. 429 -> exponential backoff 2 s .. 60 s; other errors 5 s.
  2. gmgn_quote - a GMGN sell quote for ~$1 of the token (GET /v1/trade/quote, read-only), at most one call every
             2 s across all mints; any GMGN error backs the quote off 30 s (plan block: 10 min, shared with the trader).
  3. dex   - DexScreener tokens/v1 (the old source), at most one call per 3 s, shared 250/min bucket.
A pool spec is resolved once per mint (DexScreener pair -> pool account -> decimals) and checked against the
DexScreener price before it is trusted: more than POOL_SANITY_PCT apart -> not used for that mint (re-tried later).
(Jupiter's free price API is the second opinion when DexScreener disagrees, e.g. lagging a crash.)
Never buys, never signs, never sends a transaction. Only reads.

check_buy_dex_pool (DECISION 86) is a separate pre-buy gate used by Strategy A and Strategy B
LIVE buys. It does not write FastPrice.specs and does not change exit pricing for open positions.
"""
from __future__ import annotations
import asyncio, time
import poolprice as pp
from poolprice import WSOL, USD_MINTS

POOL_SANITY_PCT = 35.0        # pool vs DexScreener at resolution; DexScreener can lag a few %, never a wrong parser
JUP_SANITY_PCT = 15.0         # ...and if DexScreener disagrees, the pool must be within this of Jupiter's price
JUP_PRICE_URL = "https://lite-api.jup.ag/price/v3"
RESOLVE_RETRY_S = 120.0       # unsupported / failed pool resolution: try again after this
RPC_BACKOFF_MAX_S = 60.0
RPC_ERROR_BACKOFF_S = 5.0
RPC_TIMEOUT_S = 6
QUOTE_MIN_GAP_S = 2.0
QUOTE_BACKOFF_S = 30.0
QUOTE_USD = 1.0               # sell-quote size: ~$1 of the token (mid-price proxy, tiny price impact)
DEX_MIN_GAP_S = 3.0           # same cadence the watchers used before (~20 calls/min)
DEX_MAX_AGE_S = 15.0          # a cached DexScreener price older than this is not used


class FastPrice:
    def __init__(self, ctx, trader=None, clock=time.time):
        self.c, self.t, self.clock = ctx, trader, clock
        self.specs: dict = {}         # mint -> spec dict, or {"bad": reason, "retry_at": ts}
        self.cache: dict = {}         # mint -> (price_usd, src, ts)
        self.dex: dict = {}           # mint -> (price_usd, ts) from the last DexScreener call
        self.dex_at = 0.0
        self.rpc_until = 0.0          # RPC backoff end
        self.rpc_backoff = 0.0
        self.quote_at = 0.0
        self.quote_until = 0.0
        self.src: dict = {}           # mint -> last source used (logged on change)
        self.stats = {"pool": 0, "gmgn_quote": 0, "dex": 0, "rpc_calls": 0, "rpc_429": 0, "rpc_err": 0}
        self.lock = asyncio.Lock()

    # ---- settings
    @property
    def s(self):
        return self.c.s

    def enabled(self) -> bool:
        return self.s["risk.price_source"] == "pool"

    def poll_s(self) -> float:
        return float(self.s["risk.fast_poll_s"])

    def _log(self, *parts):
        if self.t is not None:
            self.t._log("LIVE", *parts)

    async def _sol_usd(self):
        return await self.t.sol_usd() if self.t is not None else None

    # ---- public
    async def prices(self, mints) -> dict:
        """{mint: (price_usd, src)} for the mints we could price this tick (missing = no price, skip the tick)."""
        mints = sorted(set(mints))
        if not mints:
            return {}
        async with self.lock:
            now = self.clock()
            fresh = {m: self.cache[m] for m in mints if m in self.cache and now - self.cache[m][2] < 0.95 * self.poll_s()}
            todo = [m for m in mints if m not in fresh]
            if todo:
                got = await self._fetch(todo)
                for m, (p, src) in got.items():
                    self.cache[m] = (p, src, self.clock())
                    fresh[m] = self.cache[m]
            for m in list(self.cache):
                if m not in mints and now - self.cache[m][2] > 60:
                    self.cache.pop(m, None)
            return {m: (v[0], v[1]) for m, v in fresh.items()}

    def note(self) -> str:
        cur = {}
        for v in self.src.values():
            k = v.split(":")[0]
            cur[k] = cur.get(k, 0) + 1
        return ",".join(f"{k}={n}" for k, n in sorted(cur.items())) or "-"

    # ---- one fetch for every mint that needs a price
    async def _fetch(self, mints) -> dict:
        out, why = {}, {}
        if self.enabled():
            for m in mints:
                if m not in self.specs or ("bad" in self.specs[m] and self.clock() >= self.specs[m]["retry_at"]):
                    await self._resolve(m)
            ok = [m for m in mints if "bad" not in self.specs.get(m, {"bad": 1})]
            for m in mints:
                if m not in ok:
                    why[m] = (self.specs.get(m) or {}).get("bad", "no pool")
            if ok:
                if self.clock() < self.rpc_until:
                    for m in ok:
                        why[m] = f"rpc backoff {self.rpc_until - self.clock():.0f}s"
                else:
                    got, errs = await self._pool_prices(ok)
                    out.update(got); why.update(errs)
        else:
            why = {m: "price_source=dex" for m in mints}
        rest = [m for m in mints if m not in out]
        if rest and self.enabled():
            for m in rest:
                q = await self._quote_price(m)
                if q:
                    out[m] = (q, "gmgn_quote")
        rest = [m for m in mints if m not in out]
        if rest:
            out.update(await self._dex_prices(rest))
        for m, (p, src) in out.items():
            self.stats[src.split(":")[0]] = self.stats.get(src.split(":")[0], 0) + 1
            if self.src.get(m) != src:
                prev = self.src.get(m)
                self.src[m] = src
                self._log(m, "price source", src, f"price={p:.10g}", f"was={prev or '-'}",
                          f"why={why.get(m)}" if not src.startswith("pool") and why.get(m) else "")
        return out

    # ---- 1. pool reserves over RPC
    async def _rpc_accounts(self, keys) -> list:
        vals = []
        for i in range(0, len(keys), 100):
            self.stats["rpc_calls"] += 1
            r = await self.c.net.rpc("getMultipleAccounts", [keys[i:i + 100], {"encoding": "base64", "commitment": "confirmed"}],
                                     timeout=RPC_TIMEOUT_S)
            v = (r or {}).get("value") if isinstance(r, dict) else None
            if v is None or len(v) != len(keys[i:i + 100]):
                raise RuntimeError("getMultipleAccounts: bad result")
            vals.extend(v)
        return vals

    def _rpc_failed(self, e):
        import net as _net
        if isinstance(e, _net.RateLimited) or "429" in str(e):
            self.stats["rpc_429"] += 1
            self.rpc_backoff = min(RPC_BACKOFF_MAX_S, max(2.0, self.rpc_backoff * 2))
        else:
            self.stats["rpc_err"] += 1
            self.rpc_backoff = RPC_ERROR_BACKOFF_S
        self.rpc_until = self.clock() + self.rpc_backoff
        return f"rpc {type(e).__name__} {str(e)[:60]} (backoff {self.rpc_backoff:.0f}s)"

    def _to_usd(self, spec, p, sol_usd):
        if spec["quote"] == WSOL:
            return p * sol_usd if sol_usd else None
        return p if spec["quote"] in USD_MINTS else None

    async def _pool_prices(self, mints):
        keys = []
        for m in mints:
            for k in self.specs[m]["accounts"]:
                if k not in keys:
                    keys.append(k)
        try:
            vals = await self._rpc_accounts(keys)
        except Exception as e:
            why = self._rpc_failed(e)
            return {}, {m: why for m in mints}
        self.rpc_backoff = 0.0
        data = dict(zip(keys, (pp.b64(v) for v in vals)))
        sol = await self._sol_usd()
        out, errs = {}, {}
        for m in mints:
            spec = self.specs[m]
            try:
                p = pp.price_in_quote(spec, [data.get(k) for k in spec["accounts"]])
            except pp.Unsupported as e:
                errs[m] = f"pool {e}"
                self.specs[m] = {"bad": f"pool {e}", "retry_at": self.clock() + RESOLVE_RETRY_S, "dec": spec.get("dec_mint")}
                continue
            usd = self._to_usd(spec, p, sol)
            if usd:
                out[m] = (usd, f"pool:{spec['kind']}")
            else:
                errs[m] = "no SOL/USD"
        return out, errs

    async def _resolve(self, mint):
        """DexScreener pair -> pool account -> decimals -> one price read, checked against DexScreener."""
        from agents.scanner import pick_pair
        import fields
        bad = lambda why: self.specs.__setitem__(mint, {"bad": why, "retry_at": self.clock() + RESOLVE_RETRY_S})
        if self.clock() < self.rpc_until:
            return bad("rpc backoff")
        try:
            pairs = await self.c.net.dex_tokens([mint])
            pair = pick_pair(pairs, mint)
        except Exception as e:
            return bad(f"dex pair {type(e).__name__}")
        dex_usd = float(fields.get(pair, "priceUsd") or 0) if pair else 0.0
        if dex_usd:
            self.dex[mint] = (dex_usd, self.clock())
        addr = fields.get(pair, "pairAddress") if pair else None
        if pair and (fields.get(pair, "dexId") or "") == "pumpfun":
            addr = pp.curve_address(mint)
        if not addr:
            return bad("no pool")
        try:
            acc = (await self.c.net.rpc("getAccountInfo", [addr, {"encoding": "base64", "commitment": "confirmed"}], timeout=RPC_TIMEOUT_S) or {}).get("value")
            if not acc:
                return bad("pool account missing")
            spec = pp.describe(addr, acc["owner"], pp.b64(acc), mint)
            need = pp.needs_decimals(spec)
            if need:
                ms = (await self.c.net.rpc("getMultipleAccounts", [need, {"encoding": "base64"}], timeout=RPC_TIMEOUT_S) or {}).get("value") or []
                for m_, a in zip(need, ms):
                    spec["dec_a" if m_ == spec.get("mint_a") else "dec_b"] = pp.mint_decimals(pp.b64(a))
            if spec["kind"] != "pump_curve":
                spec["dec_mint"] = spec.get("dec_a") if spec["a_is_mint"] else spec.get("dec_b")
            vals = await self._rpc_accounts(spec["accounts"])
            p = pp.price_in_quote(spec, [pp.b64(v) for v in vals])
        except pp.Unsupported as e:
            return bad(f"unsupported: {e}")
        except Exception as e:
            why = self._rpc_failed(e)
            return bad(why)
        usd = self._to_usd(spec, p, await self._sol_usd())
        if not usd:
            return bad("no SOL/USD")
        diff = 100 * (usd / dex_usd - 1) if dex_usd else None
        if diff is None or abs(diff) > POOL_SANITY_PCT:
            # DexScreener can be far behind on a fast move (CHIMP 2026-11-16: -59% vs a lagging DexScreener while the
            # pool matched Jupiter within 2%). Second opinion: Jupiter's price API, which tracks recent swaps.
            jup = await self._jup_price(mint)
            jd = 100 * (usd / jup - 1) if jup else None
            if jd is None or abs(jd) > JUP_SANITY_PCT:
                self._log(mint, "pool price check FAILED", spec["kind"], f"pool={addr}", f"pool_usd={usd:.10g}",
                          f"dex={dex_usd or 0:.10g}", f"diff={diff if diff is not None else 0:+.1f}%",
                          f"jup={jup or 0:.10g}", f"jup_diff={jd if jd is not None else 0:+.1f}%")
                return bad(f"pool vs dex {diff if diff is not None else 0:+.0f}% / jup {'-' if jd is None else f'{jd:+.0f}%'}")
            self._log(mint, "pool price check ok (jupiter)", spec["kind"], f"pool={addr}", f"pool_usd={usd:.10g}",
                      f"dex={dex_usd or 0:.10g}", f"jup={jup:.10g}", f"jup_diff={jd:+.1f}%")
        else:
            self._log(mint, "pool price check ok", spec["kind"], f"pool={addr}", f"pool_usd={usd:.10g}", f"dex={dex_usd:.10g}", f"diff={diff:+.1f}%")
        self.specs[mint] = spec

    async def _jup_price(self, mint):
        try:
            http = getattr(self.c.net, "http", None)
            if http is None:
                return None
            r = await asyncio.wait_for(http.get(JUP_PRICE_URL, params={"ids": mint}, timeout=8), 10)
            if r.status_code != 200:
                return None
            v = ((r.json() or {}).get(mint) or {}).get("usdPrice")
            return float(v) if v else None
        except Exception:
            return None

    # ---- 2. GMGN sell quote
    async def _quote_price(self, mint):
        t = self.t
        client = getattr(t, "client", None) if t is not None else None
        now = self.clock()
        if client is None or not hasattr(client, "quote") or now < self.quote_until or now - self.quote_at < QUOTE_MIN_GAP_S:
            return None
        if time.time() - getattr(t, "plan_blocked_at", 0.0) < 600:
            return None
        row = self.c.db.one("SELECT entry_price FROM gmgn_orders WHERE mint=? AND status='open' ORDER BY id DESC LIMIT 1", (mint,))
        if not row:                                      # Strategy B LIVE position (DECISION 66)
            try:
                row = self.c.db.one("SELECT entry_price FROM strat_b_orders WHERE mint=? AND mode='LIVE' AND status='open' ORDER BY id DESC LIMIT 1", (mint,))
            except Exception:
                row = None
        entry = (row or {}).get("entry_price")
        spec = self.specs.get(mint) or {}
        dec = spec.get("dec_mint") if spec.get("dec_mint") is not None else spec.get("dec")
        if dec is None and mint.endswith("pump"):
            dec = 6
        sol = await self._sol_usd()
        if not entry or dec is None or not sol:
            return None
        amount = max(1, int(QUOTE_USD / entry * 10 ** int(dec)))
        self.quote_at = now
        try:
            import gmgn as _g
            from gmgn import SOL as GSOL
            with _g.kind("price"):                       # DECISION 88: price fallback quotes never wait or crowd out a sell
                q = await client.quote(mint, GSOL, amount, int(self.s["gmgn.slippage_pct"]))
            out = int(float((q or {}).get("output_amount") or 0))
        except Exception as e:
            self.quote_until = now + QUOTE_BACKOFF_S
            if getattr(e, "plan_blocked", False):
                t.plan_blocked_at = time.time()
            return None
        if out <= 0:
            return None
        return (out / 1e9) * sol / (amount / 10 ** int(dec))

    # ---- 3. DexScreener (old source)
    async def _dex_prices(self, mints) -> dict:
        from agents.scanner import pick_pair
        import fields
        now = self.clock()
        if now - self.dex_at >= DEX_MIN_GAP_S:
            self.dex_at = now
            pairs = await self.c.net.dex_tokens(mints)              # RateLimited propagates to the loop's backoff
            for m in mints:
                pair = pick_pair(pairs, m)
                v = fields.get(pair, "priceUsd") if pair else None
                if v:
                    self.dex[m] = (float(v), now)
        return {m: (self.dex[m][0], "dex") for m in mints if m in self.dex and now - self.dex[m][1] <= DEX_MAX_AGE_S}


# ---- DECISION 86: pre-buy DexScreener vs on-chain pool (A and B LIVE). Not used for open positions.

def dex_pool_diff_pct(dex_usd, pool_usd) -> float | None:
    """Signed percent: 100 * (pool/dex - 1). None if either price is missing or not positive."""
    try:
        d, p = float(dex_usd), float(pool_usd)
    except (TypeError, ValueError):
        return None
    if d <= 0 or p <= 0:
        return None
    return 100.0 * (p / d - 1.0)


def dex_pool_skip_reason(dex_usd, pool_usd, diff_pct) -> str:
    return (f"DexScreener is {abs(float(diff_pct)):.0f}% off the on-chain pool price "
            f"(dex={float(dex_usd):.6g}, pool={float(pool_usd):.6g})")


def _pool_to_usd(spec, price_in_quote, sol_usd):
    if spec["quote"] == WSOL:
        return price_in_quote * sol_usd if sol_usd else None
    return price_in_quote if spec["quote"] in USD_MINTS else None


async def read_pool_usd(net, mint, *, pair=None, sol_usd=None) -> tuple[float | None, str | None]:
    """One-shot on-chain pool price in USD. Does not touch FastPrice.specs.
    Returns (usd, None) or (None, error)."""
    from agents.scanner import pick_pair
    import fields
    if pair is None:
        try:
            pairs = await net.dex_tokens([mint])
            pair = pick_pair(pairs, mint)
        except Exception as e:
            return None, f"dex pair {type(e).__name__}"
    addr = fields.get(pair, "pairAddress") if pair else None
    if pair and (fields.get(pair, "dexId") or "") == "pumpfun":
        addr = pp.curve_address(mint)
    if not addr:
        return None, "no pool"
    try:
        acc = (await net.rpc("getAccountInfo", [addr, {"encoding": "base64", "commitment": "confirmed"}],
                             timeout=RPC_TIMEOUT_S) or {}).get("value")
        if not acc:
            return None, "pool account missing"
        spec = pp.describe(addr, acc["owner"], pp.b64(acc), mint)
        need = pp.needs_decimals(spec)
        if need:
            ms = (await net.rpc("getMultipleAccounts", [need, {"encoding": "base64"}], timeout=RPC_TIMEOUT_S) or {}).get("value") or []
            for m_, a in zip(need, ms):
                spec["dec_a" if m_ == spec.get("mint_a") else "dec_b"] = pp.mint_decimals(pp.b64(a) if a else None)
        if spec["kind"] != "pump_curve":
            spec["dec_mint"] = spec.get("dec_a") if spec["a_is_mint"] else spec.get("dec_b")
        keys = spec["accounts"]
        vals = (await net.rpc("getMultipleAccounts", [keys, {"encoding": "base64", "commitment": "confirmed"}],
                              timeout=RPC_TIMEOUT_S) or {}).get("value") or []
        if len(vals) != len(keys):
            return None, "pool accounts missing"
        price = pp.price_in_quote(spec, [pp.b64(v) for v in vals])
    except pp.Unsupported as e:
        return None, f"unsupported: {e}"
    except Exception as e:
        return None, f"rpc {type(e).__name__}"
    usd = _pool_to_usd(spec, price, sol_usd)
    if not usd:
        return None, "no SOL/USD"
    return float(usd), None


async def check_buy_dex_pool(c, mint, dex_usd, *, sol_usd=None, pair=None, max_pct=None) -> dict:
    """Pre-buy gate. blocked=True only when both prices are known and |diff| > max_pct.
    Unreadable pool, missing dex price, or max_pct<=0 (off): blocked=False (current buy path continues)."""
    if max_pct is None:
        try:
            max_pct = float(c.s["gmgn.max_dex_pool_diff_pct"] or 0)
        except Exception:
            max_pct = 30.0
    max_pct = float(max_pct)
    out = {"blocked": False, "reason": None, "dex_usd": dex_usd, "pool_usd": None, "diff_pct": None, "error": None, "max_pct": max_pct}
    if max_pct <= 0:
        out["error"] = "off"
        return out
    try:
        dex = float(dex_usd or 0)
    except (TypeError, ValueError):
        dex = 0.0
    if dex <= 0:
        out["error"] = "no dex price"
        return out
    pool, err = await read_pool_usd(c.net, mint, pair=pair, sol_usd=sol_usd)
    out["pool_usd"] = pool
    if err or not pool:
        out["error"] = err or "no pool price"
        return out
    diff = dex_pool_diff_pct(dex, pool)
    out["diff_pct"] = diff
    if diff is not None and abs(diff) > max_pct + 1e-9:
        out["blocked"] = True
        out["reason"] = dex_pool_skip_reason(dex, pool, diff)
    return out
